"""Narration intake (ElevenLabs generation stays external), alignment, measurements, lock, replacement.

- import: drag-drop or watched folder; waits until the file stops growing; records take id, script version,
  original checksum, duration and ONLY user-provided generation settings.
- align: local ASR word timings vs the clean script → missing / extra / substituted words, per-section timing.
  ASR mismatches are review candidates, not definitive errors. It cannot certify emotional delivery.
- lock: a read-only copy is what the edit uses; original bytes are verified by hash on every check.
- replace: a new take supersedes the old one and marks dependent edits/captions/previews stale.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import stat
import threading
import time
from pathlib import Path

from .jobs import JobFailed, enqueue, handler
from .util import new_id, now, run, sha256_file

PARTIAL_SUFFIXES = (".crdownload", ".part", ".download", ".tmp", ".partial")


def import_take(ws, project_id: str, path: str | Path, *, script_version_id: str | None = None,
                generation_settings: dict | None = None, synthetic_standin: bool = False,
                thread_id: str | None = None, origin: str = "ui", deliverable_id: str | None = None) -> dict:
    from .assets import import_file
    from .projects import get_deliverable
    from .scripts_eleven import get_script
    deliverable_id = get_deliverable(ws, deliverable_id, project_id)["id"]
    src = Path(path).expanduser()
    if src.suffix.lower() in PARTIAL_SUFFIXES:
        raise JobFailed("file is still downloading", hint="Wait for the download to finish.")
    script = get_script(ws, script_version_id) if script_version_id else (
        get_script(ws, project_id=project_id, deliverable_id=deliverable_id) or get_script(ws, project_id=project_id))
    a = import_file(ws, src, project_id=project_id, role="narration", copy=True,
                    label=f"narration take — {src.stem}",
                    license={"license": "user-generated (ElevenLabs, user's own account)", "evidence": "user import"}
                    if not synthetic_standin else {"license": "local TTS stand-in (espeak-ng output)",
                                                   "evidence": "generated locally for testing"})
    dup = ws.db.one("SELECT * FROM narration_takes WHERE project_id=? AND asset_id=?", (project_id, a["id"]))
    if dup:
        # Same bytes imported again: audio is unchanged, but the user may be correcting its labels.
        fix = {}
        if synthetic_standin and not dup["is_synthetic_standin"]:
            fix["is_synthetic_standin"] = 1
        if generation_settings:
            fix["generation_settings_json"] = {**json.loads(dup["generation_settings_json"]), **generation_settings}
        if fix:
            ws.db.update("narration_takes", {"id": dup["id"]}, fix)
            ws.remember(kind="fact", subject=f"Narration take {dup['take_no']} labels corrected",
                        content=f"Metadata only (audio untouched): {', '.join(fix)}", project_id=project_id,
                        thread_id=thread_id, ref_type="take", ref_id=dup["id"], provenance=origin)
        return get_take(ws, dup["id"]) | {"duplicate": True, "labels_updated": sorted(fix)}
    n = ws.db.one("SELECT COALESCE(MAX(take_no),0)+1 n FROM narration_takes WHERE project_id=?", (project_id,))["n"]
    tid = new_id("take")
    ws.db.insert("narration_takes", {
        "id": tid, "project_id": project_id, "deliverable_id": deliverable_id, "asset_id": a["id"], "script_version_id": script["id"] if script else None,
        "take_no": n, "original_sha256": a["sha256"], "duration": a["duration"] or 0,
        "generation_settings_json": generation_settings or {}, "is_synthetic_standin": int(synthetic_standin),
        "status": "imported", "created_at": now()})
    ws.remember(kind="fact", subject=f"Narration take {n} imported" + (" (local TTS stand-in)" if synthetic_standin else ""),
                content=f"{src.name}, {a['duration'] or 0:.1f}s, script v{script['version'] if script else '?'}; "
                        f"generation settings: {json.dumps(generation_settings or {}) or 'not provided'}",
                project_id=project_id, thread_id=thread_id, ref_type="take", ref_id=tid, provenance=origin)
    ws.close_task(project_id, "Generate narration in ElevenLabs")
    ws.automation(project_id, "narration_generation", "handoff", "ElevenLabs generation is manual by design")
    ws.automation(project_id, "narration_import", "automated" if origin in ("watch", "chat", "mcp") else "handoff")
    job = enqueue(ws, "narration_align", {"take_id": tid}, project_id=project_id, thread_id=thread_id, origin=origin)
    return get_take(ws, tid) | {"align_job": job["id"]}


def get_take(ws, take_id: str) -> dict:
    t = ws.db.one("SELECT * FROM narration_takes WHERE id=?", (take_id,))
    if not t:
        raise KeyError(take_id)
    for k in ("generation_settings", "alignment", "metrics"):
        t[k] = json.loads(t.pop(f"{k}_json"))
    return t


def measure(path: str | Path) -> dict:
    from .audio_catalog import loudness
    out = run(["ffmpeg", "-nostdin", "-v", "info", "-i", str(path), "-af",
               "silencedetect=noise=-45dB:d=0.6,astats=metadata=0:reset=0", "-f", "null", "-"],
              timeout=900, check=False).stdout
    sil = [(float(a), float(b)) for a, b in re.findall(r"silence_start: ([\d.]+)[\s\S]*?silence_end: ([\d.]+)", out)]
    peak = re.findall(r"Peak level dB: (-?[\d.]+|-inf)", out)
    clipped = re.findall(r"Number of (?:clipped|Peak count)[^:]*: (\d+)", out)
    m = {"silences": sil[:50], "long_silences": [s for s in sil if s[1] - s[0] > 1.5],
         "peak_db": float(peak[-1]) if peak and peak[-1] != "-inf" else None,
         "clip_risk": bool(peak and peak[-1] != "-inf" and float(peak[-1]) > -0.3)}
    m.update(loudness(path))
    return m


def align_words(script_words: list[str], heard: list[dict]) -> dict:
    """difflib alignment of normalized script words vs ASR words (with times)."""
    norm = lambda w: re.sub(r"[^a-z0-9']", "", w.lower())
    hw = [norm(w["word"]) for w in heard]
    sm = difflib.SequenceMatcher(a=script_words, b=hw, autojunk=False)
    mapping: dict[int, int] = {}
    issues = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                mapping[i1 + k] = j1 + k
        elif tag == "replace":
            issues.append({"type": "substituted", "script": " ".join(script_words[i1:i2]),
                           "heard": " ".join(hw[j1:j2]), "t": heard[j1]["start"] if j1 < len(heard) else None})
            for k in range(min(i2 - i1, j2 - j1)):
                mapping[i1 + k] = j1 + k
        elif tag == "delete":
            issues.append({"type": "missing", "script": " ".join(script_words[i1:i2]),
                           "t": heard[j1 - 1]["end"] if 0 < j1 <= len(heard) else None})
        elif tag == "insert":
            issues.append({"type": "extra", "heard": " ".join(hw[j1:j2]), "t": heard[j1]["start"]})
    return {"ratio": round(sm.ratio(), 3), "mapping": mapping, "issues": issues}


@handler("narration_align")
def job_align(ctx):
    from .analysis import transcribe_asset
    from .scripts_eleven import TAG_RE, get_script, words
    ws = ctx.ws
    t = get_take(ws, ctx.params["take_id"])
    pid = t["project_id"]
    a = ws.db.one("SELECT * FROM media_assets WHERE id=?", (t["asset_id"],))
    tr = transcribe_asset(ws, t["asset_id"], ctx, project_id=pid)
    heard = ws.db.all("SELECT word, start, end FROM word_timings WHERE asset_id=? ORDER BY idx", (t["asset_id"],))
    ctx.progress(0.7, "aligning narration to the script", stage="align")
    script = get_script(ws, t["script_version_id"]) if t["script_version_id"] else None
    result = {"transcript": tr, "words_heard": len(heard)}
    sections_timing = []
    if script:
        sw, owners = [], []
        for s in script["doc"]["sections"]:
            ws_ = words(s["clean"])
            sw += ws_
            owners += [s["id"]] * len(ws_)
        al = align_words(sw, heard)
        for sec in script["doc"]["sections"]:
            idx = [i for i, o in enumerate(owners) if o == sec["id"] and i in al["mapping"]]
            if idx:
                sections_timing.append({"section_id": sec["id"], "label": sec["label"],
                                        "start": heard[al["mapping"][idx[0]]]["start"],
                                        "end": heard[al["mapping"][idx[-1]]]["end"]})
        # tags read aloud: a tag word heard right where the tag sits in the script
        tag_words = {t_.lower() for s in script["doc"]["sections"] for t_ in TAG_RE.findall(s["tagged"])}
        spoken_tags = sorted({w["word"].strip(".,!?").lower() for w in heard} & {x for tw in tag_words for x in tw.split()}
                             - set(sw))
        result.update({"match_ratio": al["ratio"], "issues": al["issues"][:60], "sections": sections_timing,
                       "possible_tags_read_aloud": spoken_tags,
                       "note": "ASR differences are candidates to review, not definitive errors; ASR cannot judge "
                               "emotional delivery."})
    ctx.progress(0.85, "measuring silence, peaks, loudness", stage="measure")
    m = measure(a["current_path"])
    dur = t["duration"] or 0
    if heard and dur:
        speech = (heard[-1]["end"] - heard[0]["start"]) or dur
        m["wpm"] = round(len(heard) / speech * 60, 1)
        m["leading_silence"] = round(heard[0]["start"], 2)
        m["trailing_silence"] = round(dur - heard[-1]["end"], 2)
    ws.db.update("narration_takes", {"id": t["id"]}, {"status": "aligned" if t["status"] == "imported" else t["status"],
                                                      "alignment_json": result, "metrics_json": m})
    from .projects import set_stage, write_manifest
    set_stage(ws, pid, "narration")
    if t["status"] != "locked":
        ws.add_task(pid, f"Review narration take {t['take_no']} and lock it", kind="question")
    ws.automation(pid, "narration_alignment", "automated")
    write_manifest(ws, pid)
    n_issues = len(result.get("issues", []))
    return {**result, "metrics": m, "take_id": t["id"],
            "summary": f"take {t['take_no']}: {result.get('match_ratio', 0):.0%} word match, {n_issues} review item(s), "
                       f"{m.get('integrated_lufs')} LUFS, peak {m.get('peak_db')} dB" +
                       (f", tags possibly read aloud: {', '.join(result['possible_tags_read_aloud'])}"
                        if result.get("possible_tags_read_aloud") else "")}


def lock_take(ws, take_id: str, *, origin: str = "user", thread_id: str | None = None) -> dict:
    from .projects import get_project, write_manifest
    t = get_take(ws, take_id)
    if t["status"] == "locked":
        return verify_lock(ws, take_id)
    a = ws.db.one("SELECT * FROM media_assets WHERE id=?", (t["asset_id"],))
    if sha256_file(a["current_path"]) != t["original_sha256"]:
        raise JobFailed("the narration file changed since import; re-import it before locking")
    d = Path(get_project(ws, t["project_id"])["root_dir"]) / "voiceover" / "locked"
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"take{t['take_no']:02d}_{t['original_sha256'][:10]}{Path(a['current_path']).suffix}"
    if not dest.exists():
        shutil.copy2(a["current_path"], dest)
    os.chmod(dest, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    # previous locked take for this project is replaced
    for old in ws.db.all("SELECT id FROM narration_takes WHERE project_id=? AND deliverable_id IS ? AND "
                         "status='locked' AND id!=?", (t["project_id"], t["deliverable_id"], take_id)):
        replace_take(ws, old["id"], reason=f"replaced by take {t['take_no']}")
    ws.db.update("narration_takes", {"id": take_id}, {"status": "locked", "locked_path": str(dest), "locked_at": now()})
    ws.remember(kind="decision", subject=f"Narration take {t['take_no']} locked",
                content="Original bytes, timing, order, gain and track contents are preserved; edits work around it.",
                project_id=t["project_id"], thread_id=thread_id, ref_type="take", ref_id=take_id,
                data={"sha256": t["original_sha256"], "locked_path": str(dest)}, provenance=origin)
    ws.close_task(t["project_id"], f"Review narration take {t['take_no']}")
    write_manifest(ws, t["project_id"])
    return verify_lock(ws, take_id)


def verify_lock(ws, take_id: str) -> dict:
    t = get_take(ws, take_id)
    ok = bool(t["locked_path"]) and Path(t["locked_path"]).exists() and sha256_file(t["locked_path"]) == t["original_sha256"]
    return {"take_id": take_id, "take_no": t["take_no"], "locked": t["status"] == "locked", "bytes_unchanged": ok,
            "locked_path": t["locked_path"], "sha256": t["original_sha256"], "duration": t["duration"]}


def locked_take(ws, project_id: str, deliverable_id: str | None = None) -> dict | None:
    if deliverable_id:
        r = ws.db.one("SELECT id FROM narration_takes WHERE deliverable_id=? AND status='locked' ORDER BY locked_at "
                      "DESC LIMIT 1", (deliverable_id,))
    else:
        r = ws.db.one("SELECT id FROM narration_takes WHERE project_id=? AND status='locked' ORDER BY locked_at DESC "
                      "LIMIT 1", (project_id,))
    return get_take(ws, r["id"]) if r else None


def replace_take(ws, take_id: str, reason: str) -> list[str]:
    t = get_take(ws, take_id)
    ws.db.update("narration_takes", {"id": take_id}, {"status": "replaced"})
    stale = [r["id"] for r in ws.db.all("SELECT id FROM artifacts WHERE project_id=? AND depends_json LIKE ?",
                                        (t["project_id"], f'%"take:{take_id}"%'))]
    ws.db.x("UPDATE artifacts SET stale=1, stale_reason=? WHERE project_id=? AND depends_json LIKE ?",
            (f"narration {reason}", t["project_id"], f'%"take:{take_id}"%'))
    ws.remember(kind="decision", subject=f"Narration take {t['take_no']} replaced", content=reason,
                project_id=t["project_id"], ref_type="take", ref_id=take_id, data={"stale_artifacts": stale},
                provenance="system")
    return stale


# ---------------------------------------------------------------------- watched folder

class NarrationWatcher:
    """Polls configured folders. A new audio file is imported once its size is stable for two polls.
    Exactly one project waiting for narration → import there. Several → ask one concise question."""

    def __init__(self, ws, interval: float = 2.0):
        self.ws = ws
        self.interval = interval
        self._seen: dict[str, tuple[int, int]] = {}
        self._stop = threading.Event()
        self._started = time.time()

    def folders(self) -> list[Path]:
        return [Path(f).expanduser() for f in self.ws.cfg.get("watch_folders", []) if Path(f).expanduser().exists()]

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="narration-watch").start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.poll()
            except Exception:  # noqa: BLE001
                pass

    def poll(self) -> list[dict]:
        from .probe import AUDIO_EXT
        actions = []
        for folder in self.folders():
            for f in folder.iterdir():
                if f.suffix.lower() not in AUDIO_EXT or f.name.startswith("."):
                    continue
                st = f.stat()
                if st.st_mtime < self._started - 5:
                    continue          # only files that arrive while the app runs
                key = str(f)
                prev = self._seen.get(key)
                cur = (st.st_size, int(st.st_mtime))
                if prev is None or prev[0] != cur[0]:
                    self._seen[key] = (cur[0], 0)
                    continue
                if prev[1] == -1:
                    continue          # handled
                self._seen[key] = (cur[0], -1)
                actions.append(self.route(f))
        return actions

    def route(self, f: Path) -> dict:
        ws = self.ws
        waiting = ws.db.all("SELECT DISTINCT p.id, p.title, p.slug FROM projects p JOIN tasks t ON t.project_id=p.id "
                            "WHERE t.status='open' AND t.title LIKE 'Generate narration%' AND p.status='active'")
        named = [p for p in waiting if p["slug"] in f.stem.lower().replace("_", "-")]
        target = named[0] if len(named) == 1 else (waiting[0] if len(waiting) == 1 else None)
        if target:
            th = ws.db.one("SELECT id FROM threads WHERE project_id=? AND kind='main'", (target["id"],))
            take = import_take(ws, target["id"], f, thread_id=th["id"] if th else None, origin="watch")
            if th:
                from .chat.threads import add_message
                add_message(ws, th["id"], "system", f"Imported narration **{f.name}** as take {take['take_no']} "
                            f"and started alignment.", meta={"take_id": take["id"], "job_id": take.get("align_job")})
            return {"imported": take["id"], "project": target["id"]}
        from .chat.threads import add_message
        choices = waiting or [dict(p) for p in ws.db.all("SELECT id, title FROM projects WHERE status='active' "
                                                         "ORDER BY updated_at DESC LIMIT 4")]
        add_message(ws, "th_channel", "assistant",
                    f"New audio **{f.name}** arrived. Which project is it narration for?",
                    meta={"question": "narration_match", "file": str(f),
                          "choices": [{"project_id": p["id"], "title": p["title"]} for p in choices]})
        return {"question": str(f), "choices": [p["id"] for p in choices]}


def script_word_timings(ws, take: dict) -> tuple[list[dict], str]:
    """Caption words = the APPROVED SCRIPT's words (spelling, names, numbers as written), timed by the aligned ASR
    words. Script words the recognizer missed get interpolated timings between their aligned neighbours.
    Falls back to ASR words only when the take has no script."""
    import re as _re
    from .scripts_eleven import get_script
    heard = ws.db.all("SELECT word, start, end FROM word_timings WHERE asset_id=? ORDER BY idx", (take["asset_id"],))
    script = get_script(ws, take["script_version_id"]) if take.get("script_version_id") else None
    if not script or not heard:
        return [{"w": h["word"], "start": h["start"], "end": h["end"], "src": "asr"} for h in heard], "asr"
    tokens = [t for t in _re.split(r"\s+", script["clean_text"]) if _re.search(r"[A-Za-z0-9]", t)]
    norm = [_re.sub(r"[^a-z0-9']", "", t.lower()) for t in tokens]
    al = align_words(norm, heard)
    m = al["mapping"]
    out = []
    for i, tok in enumerate(tokens):
        if i in m:
            h = heard[m[i]]
            out.append({"w": tok, "start": h["start"], "end": h["end"], "src": "aligned"})
        else:
            out.append({"w": tok, "start": None, "end": None, "src": "interpolated"})
    # interpolate runs of unaligned words between known neighbours
    i = 0
    while i < len(out):
        if out[i]["start"] is not None:
            i += 1
            continue
        j = i
        while j < len(out) and out[j]["start"] is None:
            j += 1
        left = out[i - 1]["end"] if i > 0 else (heard[0]["start"] if heard else 0.0)
        right = out[j]["start"] if j < len(out) else (heard[-1]["end"] if heard else left + 0.3 * (j - i))
        span = max(0.12 * (j - i), right - left)
        step = span / (j - i)
        for k in range(i, j):
            out[k]["start"] = round(left + step * (k - i), 3)
            out[k]["end"] = round(left + step * (k - i + 1), 3)
        i = j
    return out, "script"
