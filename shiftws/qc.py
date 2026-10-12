"""Independent QC of a rendered file. Every flag is a candidate for review, not proof of a mistake.
Sampling cannot certify a whole export, so boundary-dense checks and a full local decode are included."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .jobs import JobFailed, handler
from .timecode import Rate
from .util import run, sha256_file


def _decode_errors(path) -> list[str]:
    out = run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "null", "-"], timeout=3600, check=False).stdout
    return [l for l in out.splitlines() if l.strip()][:20]


def _black(path) -> list[tuple[float, float]]:
    out = run(["ffmpeg", "-nostdin", "-v", "info", "-i", str(path), "-vf", "blackdetect=d=0.2:pic_th=0.95:pix_th=0.08",
               "-an", "-f", "null", "-"], timeout=3600, check=False).stdout
    return [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", out)]


def _silence(path) -> list[tuple[float, float]]:
    out = run(["ffmpeg", "-nostdin", "-v", "info", "-i", str(path), "-af", "silencedetect=noise=-50dB:d=1.5", "-vn",
               "-f", "null", "-"], timeout=3600, check=False).stdout
    return [(float(a), float(b)) for a, b in re.findall(r"silence_start: ([\d.]+)[\s\S]*?silence_end: ([\d.]+)", out)]


def _bars_at(path, t: float) -> dict:
    """cropdetect on a few frames around t; reports how much of the frame is uniform dark border."""
    out = run(["ffmpeg", "-nostdin", "-v", "info", "-ss", f"{max(0, t):.3f}", "-i", str(path), "-frames:v", "3",
               "-vf", "cropdetect=limit=0.07:round=2:reset=1", "-f", "null", "-"], timeout=120, check=False).stdout
    m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", out)
    if not m:
        return {}
    w, h, x, y = map(int, m[-1])
    return {"crop_w": w, "crop_h": h, "x": x, "y": y}


def _speech_loudness(path, windows, stream="a") -> float | None:
    """Momentary loudness inside speech windows (VO audibility check)."""
    vals = []
    for s, e in windows[:12]:
        out = run(["ffmpeg", "-nostdin", "-v", "info", "-ss", f"{s:.3f}", "-t", f"{max(0.4, e - s):.3f}", "-i", str(path),
                   "-vn", "-af", "ebur128=framelog=quiet", "-f", "null", "-"], timeout=120, check=False).stdout
        m = re.search(r"I:\s+(-?[\d.]+) LUFS", out[out.rfind("Summary"):] if "Summary" in out else out)
        if m:
            vals.append(float(m.group(1)))
    return round(sum(vals) / len(vals), 1) if vals else None


@handler("qc")
def job_qc(ctx):
    from .audio_catalog import loudness
    from .editplan import validate
    from .probe import summarize
    from .projects import pref
    ws = ctx.ws
    e = ws.db.one("SELECT * FROM exports WHERE id=?", (ctx.params["export_id"],))
    if not e or not Path(e["path"]).exists():
        raise JobFailed("export file not found", hint="Re-render the preview.")
    path = e["path"]
    plan_row = ws.db.one("SELECT * FROM edit_plan_versions WHERE id=?", (e["plan_version_id"],))
    plan = json.loads(plan_row["plan_json"])
    seq = plan["sequence"]
    rate = Rate(**seq["fps"])
    flags, checks = [], {}

    def flag(sev, item, detail, at=None):
        flags.append({"severity": sev, "item": item, "detail": detail, "at": at})

    ctx.progress(0.05, "probing and fully decoding the file", stage="probe")
    info = summarize(path)
    checks["probe"] = {k: info.get(k) for k in ("duration", "width", "height", "fps_num", "fps_den", "vcodec", "acodec",
                                                "audio_streams")}
    if sha256_file(path) != e["sha256"]:
        flag("error", "file changed", "export bytes differ from what was rendered")
    errs = _decode_errors(path)
    checks["decode_errors"] = errs
    if errs:
        flag("error", "decode", f"{len(errs)} decoder error line(s)")
    exp_dur = rate.frames_to_seconds(seq["duration_frames"])
    if info["duration"] is None or abs(info["duration"] - exp_dur) > 2 / float(rate.frac):
        flag("warn", "duration", f"file {info['duration']}s vs plan {exp_dur:.3f}s")
    if (info["width"], info["height"]) != (seq["width"], seq["height"]):
        flag("error", "dimensions", f"{info['width']}x{info['height']} vs plan {seq['width']}x{seq['height']}")
    if (info["fps_num"], info["fps_den"]) != (rate.num, rate.den):
        flag("warn", "frame rate", f"{info['fps_num']}/{info['fps_den']} vs plan {rate}")
    a = info["audio_streams"]
    if len(a) != 1 or a[0]["channels"] != 2:
        flag("warn", "audio layout", f"{len(a)} stream(s), channels {[x['channels'] for x in a]}")
    ctx.progress(0.3, "loudness / true peak", stage="loudness")
    targets = pref(ws, plan["project_id"], "targets") or {}
    loud = loudness(path)
    checks["loudness"] = loud | {"targets": targets}
    if loud["integrated_lufs"] is not None and targets.get("integrated_lufs") is not None:
        d = loud["integrated_lufs"] - targets["integrated_lufs"]
        if abs(d) > targets.get("tolerance_lu", 2.0):
            flag("warn", "loudness", f"{loud['integrated_lufs']} LUFS vs preset target {targets['integrated_lufs']} "
                 f"({'+' if d > 0 else ''}{d:.1f} LU). With narration locked, adjust music/SFX/game audio — or accept.")
    if loud["true_peak_db"] is not None and targets.get("true_peak_db") is not None and loud["true_peak_db"] > targets["true_peak_db"]:
        flag("warn", "true peak", f"{loud['true_peak_db']} dBTP above {targets['true_peak_db']} — lower competing tracks; "
             "VO is not processed automatically")
    ctx.progress(0.45, "silence and black frames", stage="picture")
    sil = _silence(path)
    checks["silence"] = sil
    for s, t in sil:
        flag("info", "silence", f"{t - s:.1f}s of near-silence — intentional pause?", at=s)
    blk = _black(path)
    checks["black"] = blk
    for s, t in blk:
        flag("warn" if (t - s) > 0.5 else "info", "black frames", f"{t - s:.2f}s black (dark gameplay is not a defect "
             "unless the frame is uniformly black)", at=s)
    # boundary-dense bar checks: every cut ±1 frame plus mid-clip
    ctx.progress(0.6, "checking for bars around every cut", stage="bars")
    bar_hits = []
    for c in plan["clips"]:
        for f in (c["dst_in"] + 1, (c["dst_in"] + c["dst_out"]) // 2, c["dst_out"] - 2):
            t = rate.frames_to_seconds(f)
            cd = _bars_at(path, t)
            if cd and (cd["crop_w"] < seq["width"] * 0.97 or cd["crop_h"] < seq["height"] * 0.97):
                bar_hits.append({"clip": c["id"], "t": round(t, 2), **cd})
    checks["bars_samples"] = len(plan["clips"]) * 3
    for b in bar_hits[:10]:
        flag("warn", "bars", f"{b['clip']} @ {b['t']}s: active area {b['crop_w']}x{b['crop_h']} (dark scene or real bars?)",
             at=b["t"])
    # captions
    ctx.progress(0.75, "captions", stage="captions")
    cues = plan["captions"]["cues"]
    cap = {"cues": len(cues)}
    if cues:
        from .scripts_eleven import get_script, words
        sc = ws.db.one("SELECT clean_text FROM script_versions WHERE id=?", (plan.get("script_id"),)) if plan.get("script_id") else None
        vocab = set(words(sc["clean_text"])) if sc else set()
        unknown = sorted({w["w"].lower().strip(".,!?;:'\"") for c in cues for w in c["words"]} - vocab - {""}) if vocab else []
        short = [c["id"] for c in cues if rate.frames_to_seconds(c["end"] - c["start"]) < 0.2]
        st = plan["captions"]["style"]
        y = float(st.get("position_y", 0.68))
        cap.update({"words_not_in_script": unknown[:20], "too_short": short,
                    "safe_area": 0.1 <= y <= 0.9, "style": st.get("style"), "kinetic": st.get("style") == "kinetic_word"})
        if unknown:
            flag("info", "caption spelling", f"{len(unknown)} caption word(s) differ from the script (ASR spelling): "
                 f"{', '.join(unknown[:8])}")
        if short:
            flag("info", "caption timing", f"{len(short)} cue(s) shorter than 0.2s")
        if not cap["safe_area"]:
            flag("warn", "caption safe margin", f"vertical position {y} outside the 10–90% safe area")
    checks["captions"] = cap
    # narration integrity + audibility
    if plan.get("narration"):
        n = plan["narration"]
        from .narration import verify_lock
        v = verify_lock(ws, n["take_id"])
        checks["narration_lock"] = v
        if not v["bytes_unchanged"]:
            flag("error", "narration", "locked narration file changed")
        val = validate(ws, plan)
        if any("narration" in x for x in val["errors"]):
            flag("error", "narration", "; ".join(x for x in val["errors"] if "narration" in x))
        segs = ws.db.all("SELECT start, end FROM transcript_segments WHERE asset_id=? ORDER BY start", (n["asset_id"],))
        windows = [(s["start"], s["end"]) for s in segs]
        mix_l = _speech_loudness(path, windows)
        vo_l = _speech_loudness(n["locked_path"], windows)
        checks["narration_audibility"] = {"mix_lufs_in_speech": mix_l, "vo_alone_lufs": vo_l,
                                          "note": "VO-alone vs full mix inside speech windows"}
        if mix_l is not None and vo_l is not None and mix_l - vo_l > 3.0:
            flag("warn", "narration audibility", f"mix is {mix_l - vo_l:.1f} LU louder than VO in speech — beds may mask VO")
    # end behavior
    tail_black = [b for b in blk if b[1] >= (info["duration"] or 0) - 0.1]
    checks["end"] = {"ends_black": bool(tail_black)}
    if tail_black:
        flag("info", "end frame", "file ends on black — intended end card?")
    passed = not any(f["severity"] == "error" for f in flags)
    status = "qc_passed" if passed and not any(f["severity"] == "warn" for f in flags) else ("qc_flagged" if passed else "qc_failed")
    qc = {"status": status, "flags": flags, "checks": checks,
          "note": "Automated flags are review candidates. Sampled checks cannot certify every frame; the full decode "
                  "check covers stream integrity."}
    ws.db.update("exports", {"id": e["id"]}, {"qc_json": qc, "status": status})
    from .projects import write_manifest
    write_manifest(ws, plan["project_id"])
    ws.automation(plan["project_id"], "qc", "automated")
    sev = {s: sum(1 for f in flags if f["severity"] == s) for s in ("error", "warn", "info")}
    return {"export_id": e["id"], **qc,
            "summary": f"QC {status}: {sev['error']} error, {sev['warn']} warning, {sev['info']} info — "
                       f"{loud['integrated_lufs']} LUFS / {loud['true_peak_db']} dBTP"}
