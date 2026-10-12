"""FFmpeg renderer for the canonical edit plan (review previews and the no-Premiere export path).

Two cached stages so targeted revisions only redo what changed:
  picture : V1 clips (trim → aspect-preserving fill crop → restrained zoom) + burned kinetic captions
  mix     : A1 locked narration (unprocessed) + A2 game audio + A3 music (sidechain-ducked under VO) + A4 SFX
Final = mux(picture, mix). The narration file is never processed: it is mixed at unity gain at its plan position.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from .jobs import JobFailed, handler
from .timecode import Rate
from .util import new_id, now, run, sha256_file

RENDER_VERSION = "render-1"


def _font() -> tuple[str, str | None]:
    """Preferred caption font if installed, else a bold fallback. Returns (family, fontsdir)."""
    try:
        out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=10).stdout
    except OSError:
        out = ""
    for fam in ("Montserrat ExtraBold", "Montserrat Black", "Montserrat", "Arial Black", "Impact", "DejaVu Sans"):
        if fam.lower() in out.lower():
            return fam, None
    return "DejaVu Sans", None


def _ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def kinetic_ass(plan: dict, dest: Path) -> dict:
    """Word-by-word captions: each cue's words appear as they are spoken; the active word pops slightly.
    Yellow fill, thick black outline, bottom-center at the preset's vertical position."""
    seq = plan["sequence"]
    W, H = seq["width"], seq["height"]
    rate = Rate(**seq["fps"])
    st = plan["captions"]["style"]
    fam, _ = _font()
    size = int(st.get("size_px", 92 if H > W else 64))
    outline = int(st.get("outline_px", 8))
    margin_v = int(H * (1 - float(st.get("position_y", 0.68))))
    head = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0",
            "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding",
            f"Style: Kinetic,{fam},{size},{_ass_color(st.get('color', '#FFE600'))},&H000000FF,"
            f"{_ass_color(st.get('outline', '#000000'))},&H00000000,-1,0,0,0,100,100,0,0,1,{outline},0,2,"
            f"{int(W * 0.06)},{int(W * 0.06)},{margin_v},1", "", "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    def ts(frames):
        s = Fraction(frames) / rate.frac
        cs = int(round(float(s) * 100))
        return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"

    lines = []
    for cue in plan["captions"]["cues"]:
        ws_ = cue["words"]
        for i, w in enumerate(ws_):
            start = w["start"] if i else cue["start"]
            end = ws_[i + 1]["start"] if i + 1 < len(ws_) else cue["end"]
            if end <= start:
                continue
            parts = []
            for j, x in enumerate(ws_):
                word = x["w"].upper().replace("{", "").replace("}", "")
                if j < i:
                    parts.append(word)
                elif j == i:
                    parts.append(r"{\fscx112\fscy112}" + word + r"{\fscx100\fscy100}")
                else:
                    parts.append(r"{\alpha&HFF&}" + word + r"{\alpha&H00&}")
            lines.append(f"Dialogue: 0,{ts(start)},{ts(end)},Kinetic,,0,0,0,,{' '.join(parts)}")
    dest.write_text("\n".join(head + lines) + "\n")
    return {"path": str(dest), "font": fam, "events": len(lines), "style": "kinetic_word yellow/black outline"}


def _segment(clip, a, seq, rate: Rate, out: Path):
    sr = Rate(**clip["src_rate"])
    in_s = sr.frames_to_seconds(clip["src_in"])
    n = clip["dst_out"] - clip["dst_in"]
    W, H = seq["width"], seq["height"]
    tr = clip["transform"]
    z0, z1 = tr["zoom"]["from"], tr["zoom"]["to"]
    if tr["mode"] == "fill":
        s = tr["scale"]
        sw, sh = int(round(a["width"] * s / 2) * 2), int(round(a["height"] * s / 2) * 2)
        fx, fy = tr.get("focus_x", 0.5), tr.get("focus_y", 0.5)
        frame = (f"scale={max(sw, W)}:{max(sh, H)}:flags=lanczos,"
                 f"crop={W}:{H}:(iw-{W})*{fx}:(ih-{H})*{fy}")
    else:
        frame = (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
                 f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black")
    zoom = (f",zoompan=z='{z0}+({z1}-{z0})*on/{max(1, n - 1)}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={W}x{H}:fps={rate.num}/{rate.den}") if abs(z1 - z0) > 1e-6 or z0 != 1.0 else ""
    vf = f"fps={rate.num}/{rate.den},{frame}{zoom},setsar=1,format=yuv420p"
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{in_s:.6f}", "-i", a["current_path"], "-an",
         "-vf", vf, "-frames:v", str(n), "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
         "-r", f"{rate.num}/{rate.den}", str(out)], timeout=1800)


def _black(seq, rate, n, out: Path):
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
         f"color=c=black:s={seq['width']}x{seq['height']}:r={rate.num}/{rate.den}", "-frames:v", str(n),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", str(out)], timeout=600)


def render_picture(ws, plan, work: Path, ctx=None) -> dict:
    from .assets import get_asset
    from .editplan import picture_key
    seq = plan["sequence"]
    rate = Rate(**seq["fps"])
    key = picture_key(plan) + RENDER_VERSION
    cache = Path(ws.cfg.cache_dir) / "render" / f"picture-{key}.mp4"
    meta_p = cache.with_suffix(".json")
    if cache.exists() and meta_p.exists():
        return json.loads(meta_p.read_text()) | {"cached": True}
    cache.parent.mkdir(parents=True, exist_ok=True)
    segs = []
    cursor = 0
    clips = sorted([c for c in plan["clips"] if c["track"] == "V1"], key=lambda c: c["dst_in"])
    for i, c in enumerate(clips):
        if c["dst_in"] > cursor:
            p = work / f"gap{i:03d}.mp4"
            _black(seq, rate, c["dst_in"] - cursor, p)
            segs.append(p)
        p = work / f"{c['id']}.mp4"
        _segment(c, get_asset(ws, c["asset_id"]), seq, rate, p)
        segs.append(p)
        cursor = c["dst_out"]
        if ctx:
            ctx.progress(0.05 + 0.45 * (i + 1) / len(clips), f"picture: {c['id']} ({i + 1}/{len(clips)})", stage="picture")
    if cursor < seq["duration_frames"]:
        p = work / "tail.mp4"
        _black(seq, rate, seq["duration_frames"] - cursor, p)
        segs.append(p)
    lst = work / "concat.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in segs))
    raw = work / "picture_raw.mp4"
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
         str(raw)], timeout=1800)
    cap = None
    if plan["captions"]["cues"] and plan["captions"]["style"].get("style") == "kinetic_word":
        cap = kinetic_ass(plan, work / "captions.ass")
        if ctx:
            ctx.progress(0.55, "burning kinetic captions", stage="captions")
        ass = str(work / "captions.ass").replace("\\", "/").replace(":", r"\:")
        run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(raw), "-vf", f"subtitles='{ass}'",
             "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", str(cache)], timeout=3600)
        shutil.copy2(work / "captions.ass", cache.with_suffix(".ass"))
    else:
        shutil.copy2(raw, cache)
    meta = {"path": str(cache), "key": key, "segments": len(segs), "captions": cap, "cached": False}
    meta_p.write_text(json.dumps(meta))
    return meta


def render_mix(ws, plan, work: Path, ctx=None) -> dict:
    from .assets import get_asset
    from .editplan import mix_key
    seq = plan["sequence"]
    rate = Rate(**seq["fps"])
    total = rate.frames_to_seconds(seq["duration_frames"])
    key = mix_key(plan) + RENDER_VERSION
    cache = Path(ws.cfg.cache_dir) / "render" / f"mix-{key}.wav"
    meta_p = cache.with_suffix(".json")
    if cache.exists() and meta_p.exists():
        return json.loads(meta_p.read_text()) | {"cached": True}
    cache.parent.mkdir(parents=True, exist_ok=True)
    inputs: list[list[str]] = []
    fg: list[str] = []
    beds, fx = [], []
    vo_label = None
    if plan.get("narration"):
        n = plan["narration"]
        inputs.append(["-i", n["locked_path"]])
        idx = len(inputs) - 1
        # unity gain, original position: format conversion only (mono→stereo duplicate, resample)
        fg.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[vo][vokey]")
        vo_label = "vo"
    for c in plan["clips"]:
        a = get_asset(ws, c["asset_id"])
        if not (c["audio"].get("use_source_audio") and a["audio_streams"]):
            continue
        sr = Rate(**c["src_rate"])
        in_s = sr.frames_to_seconds(c["src_in"])
        dur = rate.frames_to_seconds(c["dst_out"] - c["dst_in"])
        inputs.append(["-ss", f"{in_s:.6f}", "-t", f"{dur:.6f}", "-i", a["current_path"]])
        idx = len(inputs) - 1
        d_ms = int(round(rate.frames_to_seconds(c["dst_in"]) * 1000))
        fg.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,afade=t=in:d=0.02,"
                  f"afade=t=out:st={max(0, dur - 0.03):.3f}:d=0.03,volume={c['audio']['gain_db']}dB,"
                  f"adelay={d_ms}|{d_ms}[g{idx}]")
        beds.append(f"[g{idx}]")
    for m in plan["music"]:
        a = get_asset(ws, m["asset_id"])
        dur = rate.frames_to_seconds(m["dst_out"] - m["dst_in"])
        inputs.append(["-ss", f"{m.get('src_in_s', 0):.3f}", "-t", f"{dur:.3f}", "-i", a["current_path"]])
        idx = len(inputs) - 1
        d_ms = int(round(rate.frames_to_seconds(m["dst_in"]) * 1000))
        fg.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,volume={m['gain_db']}dB,"
                  f"afade=t=in:d={m.get('fade_in_s', 0.5)},afade=t=out:st={max(0, dur - m.get('fade_out_s', 1.0)):.3f}:"
                  f"d={m.get('fade_out_s', 1.0)},adelay={d_ms}|{d_ms}[m{idx}]")
        beds.append(f"[m{idx}]")
    for s in plan["sfx"]:
        a = get_asset(ws, s["asset_id"])
        inputs.append(["-i", a["current_path"]])
        idx = len(inputs) - 1
        start = rate.frames_to_seconds(s["at_frame"]) - float(s.get("hit_offset_s") or 0)
        trim = max(0.0, -start)
        d_ms = int(round(max(0.0, start) * 1000))
        fg.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=start={trim:.3f},asetpts=PTS-STARTPTS,"
                  f"volume={s['gain_db']}dB,adelay={d_ms}|{d_ms}[s{idx}]")
        fx.append(f"[s{idx}]")
    mix_in = []
    if vo_label:
        mix_in.append("[vo]")
    if beds:
        fg.append(f"{''.join(beds)}amix=inputs={len(beds)}:normalize=0:duration=longest[bed]")
        if vo_label:
            fg.append("[bed][vokey]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=350[bedd]")
            mix_in.append("[bedd]")
        else:
            mix_in.append("[bed]")
    elif vo_label:
        fg.append("[vokey]anullsink")
    mix_in += fx
    if not mix_in:
        inputs.append(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
        mix_in = [f"[{len(inputs) - 1}:a]"]
    fg.append(f"{''.join(mix_in)}amix=inputs={len(mix_in)}:normalize=0:duration=longest,"
              f"apad,atrim=end={total:.6f}[out]")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    for i in inputs:
        cmd += i
    cmd += ["-filter_complex", ";".join(fg), "-map", "[out]", "-ar", "48000", "-c:a", "pcm_s24le", str(cache)]
    if ctx:
        ctx.progress(0.7, f"mixing {len(inputs)} audio inputs", stage="mix")
    run(cmd, timeout=3600)
    meta = {"path": str(cache), "key": key, "inputs": len(inputs), "ducking": bool(vo_label and beds), "cached": False}
    meta_p.write_text(json.dumps(meta))
    return meta


@handler("render_preview")
def job_render(ctx):
    from .editplan import get_plan, validate
    from .projects import get_project, set_stage
    ws = ctx.ws
    r = ws.db.one("SELECT * FROM edit_plan_versions WHERE id=?", (ctx.params["plan_id"],))
    plan = json.loads(r["plan_json"])
    val = validate(ws, plan)
    if not val["valid"]:
        raise JobFailed("edit plan is invalid", hint="; ".join(val["errors"][:5]))
    kind = ctx.params.get("kind", "preview")
    pid = r["project_id"]
    root = Path(get_project(ws, pid)["root_dir"])
    work = Path(ws.cfg.cache_dir) / "render" / f"work-{ctx.id}"
    work.mkdir(parents=True, exist_ok=True)
    pic = render_picture(ws, plan, work, ctx)
    mix = render_mix(ws, plan, work, ctx)
    folder = root / ("previews" if kind == "preview" else "exports")
    name = f"{plan['sequence']['name'][:40].replace('/', '-')}_v{r['version']:02d}_{kind}.mp4".replace(" ", "_")
    out = folder / name
    ctx.progress(0.9, "muxing", stage="mux")
    abr = "192k" if kind == "preview" else "320k"
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", pic["path"], "-i", mix["path"], "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", abr, "-movflags", "+faststart", str(out)], timeout=1800)
    from .probe import summarize
    info = summarize(out)
    eid = new_id("exp")
    ws.db.insert("exports", {"id": eid, "project_id": pid, "plan_version_id": r["id"], "deliverable_id": r["deliverable_id"],
                             "kind": kind, "path": str(out), "sha256": sha256_file(out), "probe_json": info,
                             "status": "rendered", "created_at": now()})
    ws.db.x("INSERT OR REPLACE INTO artifacts (id, project_id, kind, cache_key, path, depends_json, stale, job_id, created_at) "
            "VALUES (?,?,?,?,?,?,0,?,?)", (new_id("art"), pid, kind, f"{pic['key']}:{mix['key']}", str(out),
                                           json.dumps([f"plan:{r['id']}"] + ([f"take:{plan['narration']['take_id']}"]
                                                                              if plan.get("narration") else [])),
                                           ctx.id, now()))
    shutil.rmtree(work, ignore_errors=True)
    set_stage(ws, pid, "review" if kind == "preview" else "export")
    ws.automation(pid, "render_" + kind, "automated")
    reused = [s for s, m in (("picture", pic), ("mix", mix)) if m.get("cached")]
    from .jobs import enqueue
    qc = enqueue(ws, "qc", {"export_id": eid}, project_id=pid, thread_id=ctx.job["thread_id"], origin="system")
    return {"export_id": eid, "path": str(out), "duration": info["duration"], "width": info["width"],
            "height": info["height"], "fps": f"{info['fps_num']}/{info['fps_den']}", "reused_stages": reused,
            "captions": pic.get("captions"), "ducking": mix.get("ducking"), "qc_job": qc["id"],
            "summary": f"{kind} v{r['version']}: {info['width']}x{info['height']} {info['duration']:.2f}s"
                       + (f" (reused {', '.join(reused)})" if reused else "") + " — QC started",
            "_outputs": [str(out)]}


@handler("audio_preview")
def job_audio_preview(ctx):
    """Short preview: the asset placed against the locked narration at `at` seconds (hit-aligned for SFX)."""
    from .assets import get_asset
    from .audio_catalog import analyze_audio_asset, waveform_png, analyze_signal
    from .narration import locked_take
    from .projects import get_project
    ws, p = ctx.ws, ctx.params
    pid = ctx.job["project_id"]
    a = get_asset(ws, p["asset_id"])
    an = analyze_audio_asset(ws, a["id"])
    take = locked_take(ws, pid)
    dur = float(p.get("duration", 12))
    at = float(p.get("at", 0))
    start = max(0.0, at - 2.0)
    out = Path(get_project(ws, pid)["root_dir"]) / "previews" / f"audio_{a['id']}_{int(at * 1000)}.wav"
    hit = an["hit_s"] if p.get("align_hit", True) else 0.0
    offset = max(0.0, at - start - hit)
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    fg = []
    if take:
        cmd += ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", take["locked_path"]]
        fg.append("[0:a]aresample=48000,aformat=channel_layouts=stereo[vo]")
    cmd += ["-i", a["current_path"]]
    ai = 1 if take else 0
    d_ms = int(offset * 1000)
    fg.append(f"[{ai}:a]aresample=48000,aformat=channel_layouts=stereo,volume={p.get('gain_db', -12)}dB,"
              f"adelay={d_ms}|{d_ms}[x]")
    fg.append(("[vo][x]amix=inputs=2:normalize=0:duration=first" if take else "[x]anull") + f",atrim=end={dur}[out]")
    cmd += ["-filter_complex", ";".join(fg), "-map", "[out]", str(out)]
    run(cmd, timeout=300)
    sig = analyze_signal(out)
    png = waveform_png(sig["waveform"], out.with_suffix(".png"), (at - start) / dur)
    return {"path": str(out), "waveform": str(png), "against_narration": bool(take), "hit_s": hit,
            "placed_at": at, "summary": f"preview of {a['title']} at {at:.1f}s " +
                                        ("against narration" if take else "(no locked narration yet — isolated)"),
            "_outputs": [str(out)]}
