"""Channel-wide music/SFX catalog monitoring.

Configured folders are rescanned periodically. New files are indexed automatically (measurements, heuristic type,
license evidence when a provenance file exists) and announced with a non-disruptive notification. A folder that is
missing (disconnected drive) marks its assets OFFLINE; a file that is gone while its folder is present is REMOVED.
Indexing never places audio on a timeline — policy is suggest_only_until_selected.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from . import probe
from .util import new_id, now, sha256_file


def add_folder(ws, path: str, library: str | None = None, role: str | None = None) -> dict:
    p = str(Path(path).expanduser().resolve())
    if not ws.db.one("SELECT 1 FROM catalog_folders WHERE path=?", (p,)):
        ws.db.insert("catalog_folders", {"id": new_id("cf"), "path": p, "library": library or Path(p).name, "role": role})
    return scan_folder(ws, p)


def notify(ws, kind, title, body, ref_id=None, project_id=None):
    n = {"id": new_id("ntf"), "kind": kind, "title": title, "body": body, "ref_id": ref_id, "project_id": project_id,
         "created_at": now()}
    ws.db.insert("notifications", n)
    ws.emit("notification", n, project_id=project_id, ref_id=ref_id)
    return n


def scan_folder(ws, path: str) -> dict:
    from .assets import import_file
    from .audio_catalog import analyze_audio_asset
    f = ws.db.one("SELECT * FROM catalog_folders WHERE path=?", (path,))
    root = Path(path)
    known = ws.db.all("SELECT id, title, current_path, size, mtime, catalog_state, availability FROM media_assets "
                      "WHERE kind='audio' AND library IS NOT NULL AND current_path LIKE ?", (path.rstrip("/") + "/%",))
    res = {"added": [], "removed": [], "offline": [], "changed": []}
    if not root.exists():
        if f["available"]:
            ws.db.update("catalog_folders", {"id": f["id"]}, {"available": 0})
            for a in known:
                ws.db.update("media_assets", {"id": a["id"]}, {"availability": "offline", "catalog_state": "offline"})
            notify(ws, "catalog.offline", f"{f['library']} unavailable",
                   f"{len(known)} catalog item(s) are on a disconnected drive/folder ({path}). They stay in the catalog "
                   "and searchable; placement is blocked until the drive is back.")
        res["offline"] = [a["id"] for a in known]
        return res
    if not f["available"]:
        ws.db.update("catalog_folders", {"id": f["id"]}, {"available": 1})
    prov = {}
    pj = root / "provenance.json"
    if pj.exists():
        for e in json.loads(pj.read_text()):
            prov[e["sha256"]] = e
    present = set()
    by_path = {a["current_path"]: a for a in known}
    for dirpath, _, files in os.walk(root):
        for name in sorted(files):
            fp = Path(dirpath) / name
            if probe.kind_for(fp) != "audio" or name.startswith("."):
                continue
            present.add(str(fp))
            st = fp.stat()
            k = by_path.get(str(fp))
            if k and k["size"] == st.st_size and abs(k["mtime"] - st.st_mtime) < 1e-3:
                if k["availability"] != "online" or k["catalog_state"] != "present":
                    ws.db.update("media_assets", {"id": k["id"]}, {"availability": "online", "catalog_state": "present"})
                continue
            h = sha256_file(fp)
            lic = None
            if h in prov:
                e = prov[h]
                lic = {"license": e["license"], "author": e.get("author"), "source": e.get("source"),
                       "evidence": f"{pj}", "attribution": f"\"{e['title']}\" by {e.get('author')} — {e['license']}"}
            role = f["role"] or ("music" if "music" in str(fp.parent).lower() else "sfx")
            if k:
                ws.db.update("media_assets", {"id": k["id"]}, {"catalog_state": "removed", "availability": "offline"})
                res["changed"].append(k["id"])
            a = import_file(ws, fp, project_id=None, role=role, library=f["library"], tags=[role], license=lic)
            ws.db.update("media_assets", {"id": a["id"]}, {"catalog_state": "present"})
            an = analyze_audio_asset(ws, a["id"])
            labels = ", ".join(f"{l['label'].replace('_', ' ')} {l['confidence']:.0%}" for l in an["classification"]["labels"])
            if not a.get("duplicate"):
                res["added"].append(a["id"])
                notify(ws, "catalog.added", f"New {role}: {a['title']}",
                       f"{role} · {labels} · {a['duration']:.1f}s · {(lic or {}).get('license', 'license-unverified')} · "
                       "available in every project (suggestion only — not placed anywhere)", ref_id=a["id"])
    for a in known:
        if a["current_path"] not in present and a["catalog_state"] != "removed":
            ws.db.update("media_assets", {"id": a["id"]}, {"catalog_state": "removed", "availability": "offline"})
            res["removed"].append(a["id"])
            notify(ws, "catalog.removed", f"Removed: {a['title']}", f"The file is gone from {path} (the folder is "
                   "present, so this is a removal, not a disconnected drive). History and decisions are kept.", ref_id=a["id"])
    ws.db.update("catalog_folders", {"id": f["id"]}, {"last_scan_at": now()})
    return res


def scan_all(ws) -> dict:
    out = {}
    for f in ws.db.all("SELECT path FROM catalog_folders"):
        try:
            out[f["path"]] = scan_folder(ws, f["path"])
        except Exception as e:  # noqa: BLE001
            out[f["path"]] = {"error": str(e)}
    return out


class CatalogWatcher:
    def __init__(self, ws, interval: float = 10.0):
        self.ws, self.interval = ws, interval
        self._stop = threading.Event()

    def start(self):
        def loop():
            while not self._stop.wait(self.interval):
                scan_all(self.ws)
        threading.Thread(target=loop, daemon=True, name="catalog-watch").start()

    def stop(self):
        self._stop.set()
