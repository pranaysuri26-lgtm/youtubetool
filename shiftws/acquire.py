"""Acquisition: yt-dlp downloads (project-specific folders) and local imports, both as resumable jobs."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .jobs import JobFailed, handler
from .util import Cancelled, ToolError, redact, run, which

YTDLP_VERSION = "2026.8.19"

_UNRECOVERABLE = [
    (r"Unsupported URL", "This site isn't supported by yt-dlp. Download the file yourself and drop it in."),
    (r"Private video|members-only|This video is private", "The video is private or members-only. "
     "If you're authorized, enable browser cookies for this download in Connections."),
    (r"Sign in to confirm|login required|cookies", "The site requires a signed-in session. "
     "Use 'cookies from browser' for this download (cookies stay local and are never logged)."),
    (r"HTTP Error 404|Not Found", "The URL returned 404 — check the link."),
    (r"Video unavailable|has been removed", "The video is unavailable at the source."),
    (r"geo.?restrict", "The video is geo-restricted from this network."),
]


def ytdlp_command() -> list[str]:
    exe = Path(sys.executable).with_name("yt-dlp")
    if exe.exists():
        return [str(exe)]
    if which("yt-dlp"):
        return [which("yt-dlp")]
    return [sys.executable, "-m", "yt_dlp"]


@handler("download")
def job_download(ctx):
    """params: url, project_id, start/end (optional seconds range), format (optional), cookies_from_browser (opt)."""
    from .assets import import_file
    from .projects import get_project
    ws, p = ctx.ws, ctx.params
    pid = ctx.job["project_id"]
    url = p["url"]
    dest = Path(get_project(ws, pid)["root_dir"]) / "source-media" / "downloads" if pid else \
        Path(ws.cfg.cache_dir) / "downloads"
    dest.mkdir(parents=True, exist_ok=True)
    # Same URL + range already downloaded and still on disk → reuse, no network.
    existing = ws.db.one("SELECT asset_id FROM source_references WHERE url=? AND requested_range IS ? AND asset_id IN "
                         "(SELECT id FROM media_assets WHERE availability='online')",
                         (url, _range(p)))
    if existing:
        a = import_file_link(ws, existing["asset_id"], pid)
        return {"asset_id": a["id"], "path": a["current_path"], "reused": True,
                "summary": f"already downloaded: {a['title']} (no new download)"}
    tmpl = str(dest / "%(title).80B [%(id)s].%(ext)s")
    cmd = ytdlp_command() + ["--newline", "--no-playlist", "--continue", "--no-overwrites", "--restrict-filenames",
                             "--write-info-json", "--write-subs", "--sub-langs", "en.*,en", "--convert-subs", "srt",
                             "--no-mtime", "--print", "after_move:SHIFTWS_FILE=%(filepath)s",
                             "--progress-template", "download:SHIFTWS_PROG=%(progress._percent_str)s %(progress._eta_str)s",
                             "-o", tmpl, "--retries", "3", "--fragment-retries", "3"]
    if p.get("format"):
        cmd += ["-f", p["format"]]
    else:
        cmd += ["-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4"]
    if p.get("start") is not None or p.get("end") is not None:
        s = p.get("start") or 0
        e = p.get("end") or "inf"
        cmd += ["--download-sections", f"*{s}-{e}", "--force-keyframes-at-cuts"]
    if p.get("cookies_from_browser"):
        cmd += ["--cookies-from-browser", p["cookies_from_browser"]]
    cmd.append(url)
    final = {"path": None}
    lines: list[str] = []

    def on_line(line: str):
        lines.append(redact(line))
        m = re.search(r"SHIFTWS_PROG=\s*([\d.]+)%", line)
        if m:
            ctx.progress(float(m.group(1)) / 100 * 0.9, f"downloading {m.group(1)}%", stage="download")
        if line.startswith("SHIFTWS_FILE="):
            final["path"] = line.split("=", 1)[1].strip()

    try:
        run(cmd, timeout=6 * 3600, cancel=ctx.cancelled, on_line=on_line)
    except Cancelled:
        _cleanup_partials(dest)
        raise
    except ToolError as e:
        text = "\n".join(lines[-30:]) + (e.output or "")
        for pat, hint in _UNRECOVERABLE:
            if re.search(pat, text, re.I):
                raise JobFailed(f"download failed: {pat.split('|')[0]}", hint=hint, retryable=False) from e
        raise JobFailed("download failed", hint=text[-600:], retryable=True) from e
    path = final["path"]
    if not path or not Path(path).exists():
        cands = sorted(dest.glob("*.mp4"), key=lambda x: x.stat().st_mtime)
        if not cands:
            raise JobFailed("yt-dlp finished but no media file was found", hint="\n".join(lines[-10:]))
        path = str(cands[-1])
    info = {}
    ij = Path(path).with_suffix(".info.json")
    if ij.exists():
        info = json.loads(ij.read_text())
    ctx.progress(0.95, "registering download", stage="import")
    a = import_file(ws, path, project_id=pid, role="download", source_url=url,
                    source_title=info.get("title"), extractor=info.get("extractor_key"),
                    source_offset=float(p.get("start") or 0), requested_range=_range(p),
                    metadata={k: info.get(k) for k in ("id", "title", "uploader", "channel", "upload_date",
                                                       "duration", "webpage_url", "license", "extractor")
                              } | {"acquired_with": f"yt-dlp {YTDLP_VERSION}"},
                    license={"license": info["license"], "source": url, "evidence": "yt-dlp metadata"}
                    if info.get("license") else None)
    subs = sorted(Path(path).parent.glob(Path(path).stem + "*.srt"))
    ws.automation(pid, "download", "automated", url)
    if pid:
        ws.close_task(pid, "Import or download footage")
    return {"asset_id": a["id"], "path": a["current_path"], "duplicate": a.get("duplicate", False),
            "subtitles": [str(s) for s in subs], "title": info.get("title"),
            "summary": f"downloaded {a['title']} ({a['duration'] or 0:.0f}s)" +
                       (" — identical file already in catalog" if a.get("duplicate") else ""),
            "_outputs": [a["current_path"]]}


def import_file_link(ws, asset_id, pid):
    from .assets import get_asset
    from .util import now
    if pid:
        ws.db.x("INSERT OR IGNORE INTO project_assets (project_id, asset_id, role, label, added_at) VALUES "
                "(?,?,?,?,?)", (pid, asset_id, "download", None, now()))
    return get_asset(ws, asset_id)


def _range(p):
    if p.get("start") is None and p.get("end") is None:
        return None
    return f"{p.get('start') or 0}-{p.get('end') or 'end'}"


def _cleanup_partials(dest: Path):
    for f in dest.glob("*.part"):
        try:
            f.unlink()
        except OSError:
            pass
    for f in dest.glob("*.ytdl"):
        try:
            f.unlink()
        except OSError:
            pass


@handler("import_media")
def job_import(ctx):
    """Import local files/folders by reference (default) or copy. Dedupes by content hash; probes every input."""
    from .assets import import_file
    from . import probe
    ws, p = ctx.ws, ctx.params
    pid = ctx.job["project_id"]
    files = []
    for item in p["paths"]:
        q = Path(item).expanduser()
        if q.is_dir():
            files += [f for f in sorted(q.rglob("*")) if f.is_file() and probe.kind_for(f)]
        elif q.exists():
            files.append(q)
        else:
            raise JobFailed(f"{q} not found", hint="Check the path or reconnect the drive.")
    done = ctx.checkpoint.get("done", {})
    for i, f in enumerate(files):
        if str(f) in done:
            continue
        kind = probe.kind_for(f)
        role = p.get("role") or {"video": "footage", "image": "screenshot", "audio": "sfx"}.get(kind, "footage")
        lic = (p.get("licenses") or {}).get(f.name)
        a = import_file(ws, f, project_id=pid, role=role, copy=p.get("copy", False), license=lic,
                        tags=p.get("tags"))
        done[str(f)] = {"asset_id": a["id"], "duplicate": a.get("duplicate", False)}
        ctx.save_checkpoint(done=done)
        ctx.progress((i + 1) / len(files), f"imported {f.name}", stage="import")
    if pid and files:
        ws.close_task(pid, "Import or download footage")
        ws.automation(pid, "import_probe", "automated", f"{len(files)} files")
    dups = sum(1 for v in done.values() if v["duplicate"])
    return {"imported": done, "summary": f"imported {len(done)} file(s)" + (f", {dups} duplicate(s) linked" if dups else "")}
