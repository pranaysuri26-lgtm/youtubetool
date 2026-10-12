"""ffprobe wrapper producing the canonical media facts stored on every asset."""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from .timecode import Rate, snap
from .util import ToolError, run

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mxf", ".ts"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".aif", ".aiff"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def ffprobe(path: str | Path) -> dict:
    try:
        cp = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                 timeout=120)
    except ToolError as e:
        raise ToolError(f"ffprobe could not read {Path(path).name}", hint="The file may be incomplete or not media.",
                        output=e.output) from e
    return json.loads(cp.stdout[cp.stdout.find("{"):])


def summarize(path: str | Path, info: dict | None = None) -> dict:
    """Return the asset fields: kind, duration, rational fps, time base, VFR flag, codecs, dims, audio streams."""
    info = info or ffprobe(path)
    streams = info.get("streams", [])
    fmt = info.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    ext = Path(path).suffix.lower()
    out: dict = {
        "duration": float(fmt["duration"]) if fmt.get("duration") not in (None, "N/A") else None,
        "audio_streams": [{
            "index": a.get("index"), "codec": a.get("codec_name"), "channels": a.get("channels"),
            "layout": a.get("channel_layout"), "sample_rate": int(a.get("sample_rate", 0) or 0),
            "language": (a.get("tags") or {}).get("language"),
        } for a in audio],
        "acodec": audio[0].get("codec_name") if audio else None,
        "fps_num": None, "fps_den": None, "time_base": None, "vfr": False,
        "vcodec": None, "width": None, "height": None,
    }
    is_still = ext in IMAGE_EXT or (ext not in VIDEO_EXT and v is not None
                                    and v.get("codec_name") in ("png", "mjpeg", "bmp", "webp", "tiff"))
    if v is not None and not is_still:
        r = _frac(v.get("r_frame_rate")); a = _frac(v.get("avg_frame_rate"))
        rate = Rate.parse(snap(a or r)) if (a or r) else None
        out.update({
            "kind": "video", "vcodec": v.get("codec_name"), "width": v.get("width"), "height": v.get("height"),
            "time_base": v.get("time_base"),
            "fps_num": rate.num if rate else None, "fps_den": rate.den if rate else None,
            # r_frame_rate is the container's base rate; a material gap from the average indicates VFR.
            "vfr": bool(r and a and abs(float(r - a)) / float(r) > 0.01),
            "sar": v.get("sample_aspect_ratio"), "rotation": _rotation(v),
            "pix_fmt": v.get("pix_fmt"), "field_order": v.get("field_order"),
        })
        if out["duration"] is None and v.get("duration"):
            out["duration"] = float(v["duration"])
    elif v is not None:
        out.update({"kind": "image", "vcodec": v.get("codec_name"), "width": v.get("width"), "height": v.get("height")})
    elif audio:
        out["kind"] = "audio"
    else:
        raise ToolError(f"{Path(path).name} has no audio or video streams", hint="Not a media file.")
    return out


def _frac(s) -> Fraction | None:
    if not s or s in ("0/0", "N/A"):
        return None
    try:
        n, d = str(s).split("/") if "/" in str(s) else (s, 1)
        return Fraction(int(n), int(d)) if int(d) and int(n) else None
    except (ValueError, ZeroDivisionError):
        return None


def _rotation(v: dict) -> int:
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            return int(sd["rotation"])
    return int((v.get("tags") or {}).get("rotate", 0) or 0)


def kind_for(path: str | Path) -> str | None:
    e = Path(path).suffix.lower()
    if e in VIDEO_EXT:
        return "video"
    if e in AUDIO_EXT:
        return "audio"
    if e in IMAGE_EXT:
        return "image"
    return None
