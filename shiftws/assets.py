"""Asset catalog: import by reference or copy, sha256 identity, probing, provenance, licenses, relinking."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import probe
from .util import new_id, now, sha256_file


def import_file(ws, path: str | Path, *, project_id: str | None, role: str, copy: bool = False,
                label: str | None = None, source_url: str | None = None, source_title: str | None = None,
                license: dict | None = None, library: str | None = None, tags: list[str] | None = None,
                source_offset: float = 0.0, requested_range: str | None = None, extractor: str | None = None,
                metadata: dict | None = None) -> dict:
    """Register a media file. Identity is the content hash: importing the same bytes twice links the
    existing asset instead of duplicating it. Originals are never modified."""
    src = Path(path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"{src} does not exist")
    digest = sha256_file(src)
    existing = ws.db.one("SELECT * FROM media_assets WHERE sha256=?", (digest,))
    duplicate = existing is not None
    if existing:
        asset_id = existing["id"]
        if existing["availability"] != "online" or not Path(existing["current_path"]).exists():
            ws.db.update("media_assets", {"id": asset_id}, {"current_path": str(src), "availability": "online",
                                                            "updated_at": now()})
    else:
        stored = src
        if copy and project_id:
            from .projects import get_project
            folder = {"narration": "voiceover", "music": "music", "sfx": "sfx", "download": "source-media"}.get(
                role, "source-media")
            dest_dir = Path(get_project(ws, project_id)["root_dir"]) / folder
            stored = dest_dir / src.name
            if stored.exists() and sha256_file(stored) != digest:
                stored = dest_dir / f"{src.stem}-{digest[:8]}{src.suffix}"
            if not stored.exists():
                shutil.copy2(src, stored)
        info = probe.summarize(stored)
        asset_id = new_id("ast")
        t = now()
        ws.db.insert("media_assets", {
            "id": asset_id, "sha256": digest, "kind": info["kind"], "title": label or src.stem,
            "original_path": str(src), "current_path": str(stored), "size": src.stat().st_size,
            "mtime": src.stat().st_mtime, "duration": info.get("duration"), "fps_num": info.get("fps_num"),
            "fps_den": info.get("fps_den"), "time_base": info.get("time_base"), "vfr": info.get("vfr", False),
            "vcodec": info.get("vcodec"), "acodec": info.get("acodec"), "width": info.get("width"),
            "height": info.get("height"), "audio_streams_json": info.get("audio_streams", []),
            "probe_json": {k: v for k, v in info.items() if k not in ("audio_streams",)},
            "tags_json": tags or [], "availability": "online", "library": library, "created_at": t, "updated_at": t})
        ws.db.insert("source_references", {
            "id": new_id("src"), "asset_id": asset_id, "project_id": project_id, "url": source_url,
            "title": source_title or src.name, "extractor": extractor, "acquired_at": t,
            "requested_range": requested_range, "source_offset": source_offset,
            "metadata_json": metadata or {"original_path": str(src)}})
        lic = license or _provenance_license(src, digest) or {}
        ws.db.insert("license_records", {
            "id": new_id("lic"), "asset_id": asset_id, "license": lic.get("license", "unverified"),
            "author": lic.get("author"), "source": lic.get("source"), "attribution_text": lic.get("attribution"),
            "evidence": lic.get("evidence"), "verified": int(bool(lic.get("license") and lic.get("evidence"))),
            "created_at": t})
    if project_id:
        ws.db.x("INSERT OR IGNORE INTO project_assets (project_id, asset_id, role, label, added_at) VALUES (?,?,?,?,?)",
                (project_id, asset_id, role, label, now()))
    a = get_asset(ws, asset_id)
    ws.index("asset", asset_id, a["title"], " ".join([a["title"], role, a["kind"], " ".join(a["tags"]),
                                                       a["license"]["license"] if a["license"] else "",
                                                       library or ""]),
             project_id=project_id, locator={"asset_id": asset_id})
    ws.emit("asset.imported", {"id": asset_id, "title": a["title"], "duplicate": duplicate, "role": role},
            project_id=project_id, ref_id=asset_id)
    a["duplicate"] = duplicate
    return a


def get_asset(ws, asset_id: str) -> dict | None:
    a = ws.db.one("SELECT * FROM media_assets WHERE id=?", (asset_id,))
    if not a:
        return None
    a["audio_streams"] = json.loads(a.pop("audio_streams_json"))
    a["probe"] = json.loads(a.pop("probe_json"))
    a["tags"] = json.loads(a.pop("tags_json"))
    a["license"] = ws.db.one("SELECT * FROM license_records WHERE asset_id=? ORDER BY created_at DESC LIMIT 1",
                             (asset_id,))
    a["source"] = ws.db.one("SELECT * FROM source_references WHERE asset_id=? ORDER BY acquired_at LIMIT 1",
                            (asset_id,))
    a["rate"] = f"{a['fps_num']}/{a['fps_den']}" if a["fps_num"] else None
    return a


def project_assets(ws, project_id: str, role: str | None = None, kind: str | None = None) -> list[dict]:
    sql = "SELECT pa.asset_id, pa.role, pa.label FROM project_assets pa JOIN media_assets a ON a.id=pa.asset_id " \
          "WHERE pa.project_id=?"
    params: list = [project_id]
    if role:
        sql += " AND pa.role=?"; params.append(role)
    if kind:
        sql += " AND a.kind=?"; params.append(kind)
    out = []
    for r in ws.db.all(sql + " ORDER BY pa.added_at", params):
        a = get_asset(ws, r["asset_id"])
        a["role"] = r["role"]
        out.append(a)
    return out


def set_license(ws, asset_id: str, *, license: str, author=None, source=None, attribution=None, evidence=None) -> dict:
    ws.db.insert("license_records", {"id": new_id("lic"), "asset_id": asset_id, "license": license,
                                     "author": author, "source": source, "attribution_text": attribution,
                                     "evidence": evidence, "verified": int(bool(evidence)), "created_at": now()})
    return get_asset(ws, asset_id)


def tag(ws, asset_id: str, tags: list[str]) -> dict:
    a = get_asset(ws, asset_id)
    merged = sorted(set(a["tags"]) | set(tags))
    ws.db.update("media_assets", {"id": asset_id}, {"tags_json": merged, "updated_at": now()})
    return get_asset(ws, asset_id)


def check_availability(ws, project_id: str | None = None) -> list[dict]:
    """Detect disconnected drives / moved files. Returns assets whose file is missing."""
    rows = ws.db.all("SELECT a.id, a.current_path, a.availability FROM media_assets a" +
                     (" JOIN project_assets pa ON pa.asset_id=a.id WHERE pa.project_id=?" if project_id else ""),
                     (project_id,) if project_id else ())
    missing = []
    for r in rows:
        ok = Path(r["current_path"]).exists()
        state = "online" if ok else "offline"
        if state != r["availability"]:
            ws.db.update("media_assets", {"id": r["id"]}, {"availability": state, "updated_at": now()})
            ws.emit("asset.availability", {"id": r["id"], "availability": state}, project_id=project_id, ref_id=r["id"])
            if state == "offline":
                _mark_dependents_stale(ws, r["id"], "source media offline")
        if not ok:
            missing.append(get_asset(ws, r["id"]))
    return missing


def relink_by_hash(ws, search_dirs: list[str | Path]) -> list[dict]:
    """Scan folders for files whose size+hash match offline assets and relink them."""
    offline: dict[int, list[dict]] = {}
    for a in ws.db.all("SELECT id, size, sha256 FROM media_assets WHERE availability='offline'"):
        offline.setdefault(a["size"], []).append(a)
    relinked = []
    for d in search_dirs:
        for root, _, files in os.walk(Path(d).expanduser()):
            for f in files:
                p = Path(root) / f
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                for a in offline.get(size, []):
                    if sha256_file(p) == a["sha256"]:
                        ws.db.update("media_assets", {"id": a["id"]}, {"current_path": str(p),
                                                                       "availability": "online", "updated_at": now()})
                        relinked.append({"asset_id": a["id"], "path": str(p)})
    return relinked


def _mark_dependents_stale(ws, asset_id: str, reason: str) -> None:
    ws.db.x("UPDATE artifacts SET stale=1, stale_reason=? WHERE asset_id=? OR depends_json LIKE ?",
            (reason, asset_id, f'%"asset:{asset_id}"%'))


def check_modified(ws, asset_id: str) -> bool:
    """True if the file on disk no longer matches the stored hash (upstream changed → derived outputs stale)."""
    a = get_asset(ws, asset_id)
    p = Path(a["current_path"])
    if not p.exists():
        return False
    st = p.stat()
    if st.st_size == a["size"] and abs(st.st_mtime - a["mtime"]) < 1e-3:
        return False
    if sha256_file(p) != a["sha256"]:
        _mark_dependents_stale(ws, asset_id, "source file changed on disk")
        return True
    return False


def _provenance_license(src: Path, digest: str) -> dict | None:
    """License evidence from a provenance.json shipped alongside media (same folder or its parent), matched by hash."""
    for d in (src.parent, src.parent.parent):
        pj = d / "provenance.json"
        if not pj.exists():
            continue
        try:
            for e in json.loads(pj.read_text()):
                if e.get("sha256") == digest:
                    return {"license": e["license"], "author": e.get("author"), "source": e.get("source"),
                            "evidence": f"{pj}", "attribution": f"\"{e.get('title')}\" by {e.get('author')} — {e['license']}"}
        except (OSError, ValueError, KeyError):
            continue
    return None
