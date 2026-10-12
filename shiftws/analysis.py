"""Real video analysis.

Pipeline per asset (each step cached by file hash + settings + tool version):
  1. transcribe once with the configured local backend → transcript_segments + word_timings (+ sidecar .srt)
  2. run claude-real-video (crv) on the local file with that sidecar → scene-aware deduplicated frames
     (crv reuses the sidecar instead of running its own Whisper, so audio is never transcribed twice)
  3. per-scene metrics (luma, black, motion) with FFmpeg/Pillow; flag sparse visual coverage
Visual *descriptions* are not invented here: frames are marked uninspected until a vision-capable model
(Claude Code via MCP, or a configured local model) looks at them and records a description + confidence.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from . import transcribe as tr
from .jobs import JobFailed, handler
from .util import new_id, now, run, which

CRV_VERSION = "0.10.7"


def _asset(ws, asset_id):
    from .assets import get_asset
    a = get_asset(ws, asset_id)
    if not a:
        raise JobFailed(f"asset {asset_id} not found")
    if not Path(a["current_path"]).exists():
        raise JobFailed(f"{a['title']} is offline", hint="Reconnect the drive or relink the file by hash.")
    return a


def _artifact(ws, kind, cache_key, path, *, asset_id=None, project_id=None, job_id=None, depends=()):
    ws.db.x("INSERT OR REPLACE INTO artifacts (id, project_id, asset_id, kind, cache_key, path, depends_json, stale, "
            "job_id, created_at) VALUES (?,?,?,?,?,?,?,0,?,?)",
            (new_id("art"), project_id, asset_id, kind, cache_key, str(path), json.dumps(list(depends)), job_id, now()))


def cached_artifact(ws, kind, cache_key) -> dict | None:
    r = ws.db.one("SELECT * FROM artifacts WHERE kind=? AND cache_key=? AND stale=0", (kind, cache_key))
    if r and Path(r["path"]).exists():
        return r
    return None


def analysis_dir(ws, asset) -> Path:
    d = Path(ws.cfg.cache_dir) / "analysis" / asset["sha256"][:16]
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ transcription

def transcribe_asset(ws, asset_id: str, ctx=None, start: float | None = None, end: float | None = None,
                     project_id: str | None = None) -> dict:
    a = _asset(ws, asset_id)
    if not a["audio_streams"]:
        return {"segments": 0, "words": 0, "note": "no audio stream", "cached": False}
    backend = tr.get_backend(ws.cfg)
    key = f"{a['sha256']}:{backend.name}:{backend.model}:{start}:{end}"
    hit = cached_artifact(ws, "transcript", key)
    if hit:
        n = ws.db.one("SELECT COUNT(*) n FROM transcript_segments WHERE asset_id=?", (asset_id,))["n"]
        return {"segments": n, "cached": True, "srt": hit["path"], "backend": backend.name, "model": backend.model}
    if ctx:
        ctx.progress(0.05, f"decoding audio of {a['title']}", stage="transcribe")
    audio = tr.decode_pcm16k(a["current_path"], start, end)
    if ctx:
        ctx.progress(0.15, f"transcribing {len(audio) / 16000:.0f}s with {backend.name}/{backend.model}")
    res = backend.transcribe(audio)
    off = start or 0.0
    with ws.db.tx():
        ws.db.x("DELETE FROM transcript_segments WHERE asset_id=?", (asset_id,))
        ws.db.x("DELETE FROM word_timings WHERE asset_id=?", (asset_id,))
        for i, s in enumerate(res["segments"]):
            ws.db.insert("transcript_segments", {"id": f"seg_{asset_id[-8:]}_{i}", "asset_id": asset_id, "idx": i,
                                                 "start": s["start"] + off, "end": s["end"] + off, "text": s["text"],
                                                 "backend": backend.name, "model": backend.model})
        for i, w in enumerate(res["words"]):
            ws.db.insert("word_timings", {"asset_id": asset_id, "idx": i, "start": w["start"] + off,
                                          "end": w["end"] + off, "word": w["word"], "confidence": w.get("conf")})
    for i, s in enumerate(res["segments"]):
        ws.index("transcript", f"seg_{asset_id[-8:]}_{i}", a["title"], s["text"], project_id=project_id,
                 locator={"asset_id": asset_id, "t": round(s["start"] + off, 2), "end": round(s["end"] + off, 2)})
    srt = analysis_dir(ws, a) / "transcript.srt"
    srt.write_text(tr.to_srt([{**s, "start": s["start"] + off, "end": s["end"] + off} for s in res["segments"]]))
    _artifact(ws, "transcript", key, srt, asset_id=asset_id, project_id=project_id,
              job_id=ctx.id if ctx else None, depends=[f"asset:{asset_id}"])
    return {"segments": len(res["segments"]), "words": len(res["words"]), "cached": False, "srt": str(srt),
            "backend": backend.name, "model": backend.model}


@handler("transcribe")
def job_transcribe(ctx):
    p = ctx.params
    r = transcribe_asset(ctx.ws, p["asset_id"], ctx, p.get("start"), p.get("end"), ctx.job["project_id"])
    ctx.ws.automation(ctx.job["project_id"], "transcribe", "automated")
    r["summary"] = f"{r.get('segments', 0)} transcript segments" + (" (cached)" if r.get("cached") else "")
    return r


# ------------------------------------------------------------------ crv

def crv_command() -> list[str]:
    exe = Path(sys.executable).with_name("crv")
    if exe.exists():
        return [str(exe)]
    if which("crv"):
        return [which("crv")]
    return [sys.executable, "-m", "claude_real_video"]


def run_crv(ws, asset, srt: str | None, ctx=None, *, start=None, end=None, max_frames=None, scene=0.30,
            frame_width=640, dedup_threshold=None, grid=False, why=None) -> dict:
    settings = {"start": start, "end": end, "max_frames": max_frames, "scene": scene, "w": frame_width,
                "dedup": dedup_threshold, "grid": grid, "why": why}
    key = f"{asset['sha256']}:crv{CRV_VERSION}:{json.dumps(settings, sort_keys=True)}"
    hit = cached_artifact(ws, "crv", key)
    if hit:
        return {"out": hit["path"], "cached": True}
    base = analysis_dir(ws, asset)
    win = f"{start or 0:g}-{end or 'end'}" if (start or end) else "full"
    from .util import hash_obj
    out = base / f"crv-{win}-{hash_obj(settings)[:8]}"
    inp = base / "input"
    inp.mkdir(exist_ok=True)
    # symlink the original so crv finds our sidecar transcript without touching the user's folder
    ext = Path(asset["current_path"]).suffix
    link = inp / f"source{ext}"
    if link.is_symlink() or link.exists():
        link.unlink()
    try:
        link.symlink_to(asset["current_path"])
    except OSError:
        import shutil
        shutil.copy2(asset["current_path"], link)
    side = inp / "source.srt"
    if srt and Path(srt).exists():
        side.write_text(Path(srt).read_text())
    elif side.exists():
        side.unlink()
    cmd = crv_command() + [str(link), "-o", str(out), "--overwrite", "--scene", str(scene),
                           "--frame-width", str(frame_width)]
    if start is not None:
        cmd += ["--from", f"{start:.3f}"]
    if end is not None:
        cmd += ["--to", f"{end:.3f}"]
    if max_frames:
        cmd += ["--max-frames", str(max_frames)]
    if dedup_threshold is not None:
        cmd += ["--dedup-threshold", str(dedup_threshold)]
    if grid:
        cmd += ["--grid"]
    if why:
        cmd += ["--why", why]
    if not (srt and Path(srt).exists()):
        cmd += ["--no-transcribe"]
    if ctx:
        ctx.progress(0.5, "extracting scene-aware frames with claude-real-video", stage="frames")
    lines: list[str] = []
    run(cmd, timeout=3600, cancel=ctx.cancelled if ctx else None, on_line=lines.append,
        env={"CRV_NO_UPDATE_CHECK": "1", "PYTHONUNBUFFERED": "1"})
    if not (out / "frames.json").exists():
        raise JobFailed("claude-real-video produced no frames.json", hint="\n".join(lines[-10:]))
    _artifact(ws, "crv", key, out, asset_id=asset["id"], job_id=ctx.id if ctx else None,
              depends=[f"asset:{asset['id']}"])
    return {"out": str(out), "cached": False}


# ------------------------------------------------------------------ scenes

def _black_intervals(path, start=None, end=None) -> list[tuple[float, float]]:
    cmd = ["ffmpeg", "-nostdin", "-v", "info"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(path)]
    if end:
        cmd += ["-t", str(end - (start or 0))]
    cmd += ["-vf", "blackdetect=d=0.25:pic_th=0.95:pix_th=0.08", "-an", "-f", "null", "-"]
    out = run(cmd, timeout=3600, check=False).stdout
    res = []
    for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+)", out):
        res.append((float(m.group(1)) + (start or 0), float(m.group(2)) + (start or 0)))
    return res


def _frame_metrics(path: Path) -> dict:
    from PIL import Image, ImageStat
    im = Image.open(path).convert("L")
    st = ImageStat.Stat(im)
    w, h = im.size
    # pillarbox/letterbox detection: dark uniform side columns / top rows
    cols = [ImageStat.Stat(im.crop((x, 0, x + 1, h))).mean[0] for x in range(0, w, max(1, w // 64))]
    rows = [ImageStat.Stat(im.crop((0, y, w, y + 1))).mean[0] for y in range(0, h, max(1, h // 64))]
    side = 0
    for c in cols:
        if c < 12:
            side += 1
        else:
            break
    top = 0
    for r in rows:
        if r < 12:
            top += 1
        else:
            break
    return {"luma": round(st.mean[0], 1), "contrast": round(st.stddev[0], 1),
            "pillarbox_frac": round(side / len(cols), 3), "letterbox_frac": round(top / len(rows), 3)}


def build_scenes(ws, asset, crv_out: str, start=None, end=None, project_id=None) -> dict:
    data = json.loads((Path(crv_out) / "frames.json").read_text())
    frames = data["frames"]
    dur = asset["duration"] or 0
    s0 = start or 0.0
    s1 = end or dur
    blacks = _black_intervals(asset["current_path"], start, end)
    rows = []
    for i, f in enumerate(frames):
        t = float(f["timestamp_sec"])
        t_next = float(frames[i + 1]["timestamp_sec"]) if i + 1 < len(frames) else s1
        fp = Path(crv_out) / "frames" / f["file"]
        if not fp.exists():
            fp = next(Path(crv_out).rglob(f["file"]), fp)
        m = _frame_metrics(fp) if fp.exists() else {}
        m["black_overlap"] = round(sum(max(0, min(b1, t_next) - max(b0, t)) for b0, b1 in blacks), 2)
        m["selection_reason"] = f.get("selection_reason")
        m["dark_but_not_black"] = bool(m.get("luma", 99) < 30 and m["black_overlap"] == 0)
        rows.append({"t": t, "t_next": t_next, "frame": str(fp), "metrics": m})
    gaps = [r["t_next"] - r["t"] for r in rows]
    max_gap = max(gaps) if gaps else (s1 - s0)
    with ws.db.tx():
        # replace scenes inside this window only (coarse-to-fine passes keep other windows)
        # inspected scenes (described by a model) are kept across re-analysis passes
        ws.db.x("DELETE FROM scenes WHERE asset_id=? AND start>=? AND start<? AND described_by IS NULL",
                (asset["id"], s0, s1 + 1e-6))
        for i, r in enumerate(rows):
            sid = f"scn_{asset['id'][-8:]}_{int(r['t'] * 1000)}"
            ws.db.x("INSERT OR IGNORE INTO scenes (id, asset_id, idx, start, end, frame_path, description, tags_json, "
                    "confidence, described_by, metrics_json, uncertain) VALUES (?,?,?,?,?,?,'','[]',NULL,NULL,?,1)",
                    (sid, asset["id"], i, r["t"], r["t_next"], r["frame"], json.dumps(r["metrics"])))
    for r in rows:
        sid = f"scn_{asset['id'][-8:]}_{int(r['t'] * 1000)}"
        said = " ".join(x["text"] for x in ws.db.all(
            "SELECT text FROM transcript_segments WHERE asset_id=? AND end>? AND start<?",
            (asset["id"], r["t"], r["t_next"])))
        cur = ws.db.one("SELECT description, tags_json, metrics_json, frame_path FROM scenes WHERE id=?", (sid,)) or {}
        desc = f"{cur.get('description') or ''} {' '.join(json.loads(cur.get('tags_json') or '[]'))}".strip()
        ws.index("scene", sid, f"{asset['title']} @ {r['t']:.1f}s", f"{desc} {said} luma {r['metrics'].get('luma')}",
                 project_id=project_id, locator={"asset_id": asset["id"], "t": round(r["t"], 2),
                                                 "end": round(r["t_next"], 2), "frame": cur.get("frame_path") or r["frame"]})
    coverage = {
        "frames": len(rows), "window": [s0, s1], "max_gap_s": round(max_gap, 2),
        "sparse": bool(max_gap > 8.0 or (len(rows) and (s1 - s0) / len(rows) > 6.0)),
        "black_intervals": blacks,
        "pillarboxed_frames": sum(1 for r in rows if r["metrics"].get("pillarbox_frac", 0) > 0.05),
        "note": "Scene-aware samples, not continuous frame-by-frame observation. Inspect dense frames around "
                "actions and edit boundaries before relying on a moment.",
    }
    return coverage


@handler("analyze_video")
def job_analyze(ctx):
    ws, p = ctx.ws, ctx.params
    a = _asset(ws, p["asset_id"])
    pid = ctx.job["project_id"]
    start, end = p.get("start"), p.get("end")
    result = {"asset_id": a["id"], "title": a["title"]}
    if not ctx.checkpoint.get("transcribed"):
        t = transcribe_asset(ws, a["id"], ctx, start, end, pid) if p.get("transcribe", True) else {}
        ctx.save_checkpoint(transcribed=True, transcript=t)
    result["transcript"] = ctx.checkpoint.get("transcript", {})
    if not ctx.checkpoint.get("crv_out"):
        c = run_crv(ws, a, result["transcript"].get("srt"), ctx, start=start, end=end,
                    max_frames=p.get("max_frames"), scene=p.get("scene", 0.30), frame_width=p.get("frame_width", 640),
                    dedup_threshold=p.get("dedup_threshold"), grid=p.get("grid", False), why=p.get("why"))
        ctx.save_checkpoint(crv_out=c["out"], crv_cached=c["cached"])
    ctx.progress(0.85, "measuring scenes (luma, black frames, bars)", stage="scenes")
    cov = build_scenes(ws, a, ctx.checkpoint["crv_out"], start, end, pid)
    result.update({"crv_out": ctx.checkpoint["crv_out"], "crv_cached": ctx.checkpoint.get("crv_cached"),
                   "coverage": cov})
    if pid:
        ws.close_task(pid, "Analyze")
        ws.automation(pid, "video_analysis", "automated", a["title"])
        if cov["frames"] and not ws.db.one("SELECT 1 FROM scenes WHERE asset_id=? AND described_by IS NOT NULL",
                                           (a["id"],)):
            ws.add_task(pid, f"Describe key frames of {a['title']} (vision model)", kind="todo",
                        detail="Scene frames exist but nothing has inspected them yet.")
    frames = ws.db.all("SELECT frame_path FROM scenes WHERE asset_id=? ORDER BY start LIMIT 6", (a["id"],))
    result["preview_frames"] = [f["frame_path"] for f in frames]
    result["summary"] = (f"{a['title']}: {cov['frames']} scene frames, {result['transcript'].get('segments', 0)} "
                         f"transcript segments" + (", sparse coverage" if cov["sparse"] else "") +
                         (f", {cov['pillarboxed_frames']} pillarboxed frames" if cov["pillarboxed_frames"] else ""))
    result["_outputs"] = [ctx.checkpoint["crv_out"]]
    return result


# ------------------------------------------------------------------ scene inspection (model-provided)

def describe_scene(ws, scene_id: str, description: str, *, tags=None, confidence: float, described_by: str,
                   project_id=None, rubric: dict | None = None) -> dict:
    """Store what a vision-capable model saw in a frame. Confidence is required; low confidence stays uncertain.
    rubric (deep dive): CATEGORY, CHARACTERS/ELEMENTS VISIBLE, WHY NOTABLE, VISUAL QUALITY 1-5,
    STORY/SIGNIFICANCE 1-5, ON-SCREEN TEXT — subjective editorial scores, stored as such."""
    s = ws.db.one("SELECT * FROM scenes WHERE id=?", (scene_id,))
    if not s:
        raise KeyError(scene_id)
    metrics = json.loads(s["metrics_json"])
    if rubric:
        metrics["rubric"] = {**metrics.get("rubric", {}), **rubric}
    ws.db.update("scenes", {"id": scene_id}, {"description": description, "tags_json": tags or [],
                                              "confidence": confidence, "described_by": described_by,
                                              "uncertain": int(confidence < 0.6), "metrics_json": metrics})
    from .reports import mark_reviewed
    mark_reviewed(ws, s["asset_id"], "frame", s["frame_path"] or scene_id, described_by, description[:200])
    a = ws.db.one("SELECT title FROM media_assets WHERE id=?", (s["asset_id"],))
    said = " ".join(x["text"] for x in ws.db.all(
        "SELECT text FROM transcript_segments WHERE asset_id=? AND end>? AND start<?", (s["asset_id"], s["start"],
                                                                                         s["end"])))
    ws.index("scene", scene_id, f"{a['title']} @ {s['start']:.1f}s",
             f"{description} {' '.join(tags or [])} {json.dumps(rubric or {})} {said}", project_id=project_id,
             locator={"asset_id": s["asset_id"], "t": round(s["start"], 2), "end": round(s["end"], 2),
                      "frame": s["frame_path"]})
    return ws.db.one("SELECT * FROM scenes WHERE id=?", (scene_id,))


def find_footage(ws, query: str, project_id: str | None = None, limit: int = 10) -> list[dict]:
    """Search transcripts + inspected scene descriptions. Returns source id, time range, evidence and confidence."""
    hits = ws.search(query, scope="project" if project_id else "all", project_id=project_id,
                     kinds=["scene", "transcript"], limit=limit * 2)
    out = []
    for h in hits:
        loc = h["locator"]
        conf = None
        described = False
        if h["kind"] == "scene":
            s = ws.db.one("SELECT confidence, described_by, uncertain FROM scenes WHERE id=?", (h["ref_id"],))
            if s:
                conf, described = s["confidence"], s["described_by"] is not None
        out.append({"kind": h["kind"], "asset_id": loc.get("asset_id"), "start": loc.get("t"), "end": loc.get("end"),
                    "evidence": h["snippet"], "frame": loc.get("frame"), "confidence": conf,
                    "basis": ("vision description" if described else
                              "transcript match" if h["kind"] == "transcript" else "uninspected frame (metrics only)"),
                    "project": h["project_title"]})
    return out[:limit]
