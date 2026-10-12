"""Music / SFX catalog: measurements, hit points, waveform, heuristic classification (with visible confidence),
license evidence and usage history (approved / rejected / favorite) that persists across projects."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import numpy as np

from . import probe
from .jobs import handler
from .transcribe import decode_pcm16k
from .util import new_id, now, run

TOOL_VERSION = "audio-catalog-1"


def loudness(path: str | Path) -> dict:
    """EBU R128 integrated loudness, LRA and true peak via FFmpeg's ebur128 filter."""
    out = run(["ffmpeg", "-nostdin", "-v", "info", "-i", str(path), "-vn", "-filter_complex",
               "ebur128=peak=true:framelog=quiet", "-f", "null", "-"], timeout=1800, check=False).stdout
    summary = out[out.rfind("Summary:"):] if "Summary:" in out else out
    def grab(label):
        m = re.search(label + r"\s*:\s*(-?[\d.]+|-inf)", summary)
        return None if not m or m.group(1) == "-inf" else float(m.group(1))
    return {"integrated_lufs": grab(r"I"), "lra": grab(r"LRA"), "true_peak_db": grab(r"Peak")}


def analyze_signal(path: str | Path) -> dict:
    a = decode_pcm16k(path)
    sr = 16000
    if len(a) == 0:
        return {}
    peak = float(np.abs(a).max())
    rms = float(np.sqrt(np.mean(a ** 2)))
    hop = int(0.01 * sr)
    n = len(a) // hop
    env = np.abs(a[:n * hop]).reshape(n, hop).max(axis=1) if n else np.array([peak])
    thresh = max(peak * 0.05, 10 ** (-50 / 20))
    above = np.nonzero(env > thresh)[0]
    onset = float(above[0] * hop / sr) if len(above) else 0.0
    # hit point: largest positive jump in short-term energy (the transient), not file time zero
    e = env ** 2
    flux = np.diff(e, prepend=e[0])
    hit = float(int(np.argmax(flux)) * hop / sr) if len(flux) else 0.0
    spec = np.abs(np.fft.rfft(a[: min(len(a), sr * 30)]))
    freqs = np.fft.rfftfreq(min(len(a), sr * 30), 1 / sr)
    tot = float(spec.sum()) or 1.0
    bands = {"low_<250Hz": float(spec[freqs < 250].sum() / tot),
             "mid_250-4kHz": float(spec[(freqs >= 250) & (freqs < 4000)].sum() / tot),
             "high_>4kHz": float(spec[freqs >= 4000].sum() / tot)}
    # waveform peaks (200 buckets) for UI and PNG
    buckets = 200
    step = max(1, len(a) // buckets)
    wf = [round(float(np.abs(a[i:i + step]).max()), 4) for i in range(0, step * buckets, step) if i < len(a)]
    # coarse sections for music: energy changes on 2 s windows
    win = sr * 2
    energies = [float(np.sqrt(np.mean(a[i:i + win] ** 2))) for i in range(0, len(a) - win + 1, win)]
    sections = []
    if len(energies) > 6:
        med = float(np.median(energies)) or 1e-6
        cur = None
        for i, en in enumerate(energies):
            lvl = "high" if en > med * 1.3 else "low" if en < med * 0.7 else "mid"
            if not cur or cur["level"] != lvl:
                cur = {"start": i * 2.0, "end": (i + 1) * 2.0, "level": lvl}
                sections.append(cur)
            else:
                cur["end"] = (i + 1) * 2.0
        sections = [s for s in sections if s["end"] - s["start"] >= 4.0]
    return {"peak_db": round(20 * np.log10(peak), 2) if peak > 0 else None,
            "rms_db": round(20 * np.log10(rms), 2) if rms > 0 else None, "onset_s": round(onset, 3),
            "hit_s": round(hit, 3), "bands": {k: round(v, 3) for k, v in bands.items()}, "waveform": wf,
            "sections": sections, "duration": len(a) / sr,
            "crest_db": round(20 * np.log10(peak / rms), 2) if rms > 0 and peak > 0 else None}


def classify(sig: dict) -> dict:
    """Heuristic labels. Confidence is shown to the user; it is not a trained classifier."""
    d = sig.get("duration", 0)
    crest = sig.get("crest_db") or 0
    low = sig["bands"]["low_<250Hz"] if sig.get("bands") else 0
    mid = sig["bands"]["mid_250-4kHz"] if sig.get("bands") else 0
    labels = []
    if d >= 30:
        labels.append(("music_or_ambience", 0.7))
    elif d <= 4 and crest >= 10:
        labels.append(("impact_or_hit", 0.55 + min(0.3, (crest - 10) / 40)))
    elif d <= 4:
        labels.append(("short_sfx", 0.5))
    else:
        labels.append(("transition_or_riser", 0.35))
    if low > 0.45:
        labels.append(("bass_heavy", round(min(0.9, low), 2)))
    if mid > 0.45:
        labels.append(("audible_midrange", round(min(0.9, mid), 2)))
    return {"labels": [{"label": l, "confidence": round(c, 2)} for l, c in labels],
            "method": "heuristic (duration, crest factor, band energy)"}


def waveform_png(peaks: list[float], dest: Path, hit_frac: float | None = None) -> Path:
    from PIL import Image, ImageDraw
    w, h = 600, 90
    im = Image.new("RGB", (w, h), (24, 24, 28))
    d = ImageDraw.Draw(im)
    mx = max(peaks) or 1
    bw = w / max(1, len(peaks))
    for i, p in enumerate(peaks):
        y = (p / mx) * (h / 2 - 4)
        x = int(i * bw)
        d.line([(x, h / 2 - y), (x, h / 2 + y)], fill=(255, 214, 0), width=max(1, int(bw) - 1))
    if hit_frac is not None:
        x = int(hit_frac * w)
        d.line([(x, 0), (x, h)], fill=(255, 70, 70), width=2)
    im.save(dest)
    return dest


def analyze_audio_asset(ws, asset_id: str, force: bool = False) -> dict:
    from .assets import get_asset
    a = get_asset(ws, asset_id)
    prev = ws.db.one("SELECT * FROM audio_analysis WHERE asset_id=?", (asset_id,))
    if prev and prev["tool_version"] == TOOL_VERSION and not force:
        return _row(prev)
    sig = analyze_signal(a["current_path"])
    loud = loudness(a["current_path"])
    cls = classify(sig)
    wdir = Path(ws.cfg.cache_dir) / "waveforms"
    wdir.mkdir(parents=True, exist_ok=True)
    png = waveform_png(sig["waveform"], wdir / f"{a['sha256'][:16]}.png",
                       sig["hit_s"] / sig["duration"] if sig.get("duration") else None)
    row = {"asset_id": asset_id, "integrated_lufs": loud["integrated_lufs"], "true_peak_db": loud["true_peak_db"],
           "lra": loud["lra"], "peak_db": sig["peak_db"], "rms_db": sig["rms_db"], "hit_s": sig["hit_s"],
           "onset_s": sig["onset_s"], "spectral_json": sig["bands"], "sections_json": sig["sections"],
           "classification_json": cls, "waveform_path": str(png), "waveform_json": sig["waveform"],
           "tool_version": TOOL_VERSION, "analyzed_at": now()}
    ws.db.x("DELETE FROM audio_analysis WHERE asset_id=?", (asset_id,))
    ws.db.insert("audio_analysis", row)
    labels = " ".join(l["label"].replace("_", " ") for l in cls["labels"])
    ws.index("audio", asset_id, a["title"], f"{a['title']} {labels} {' '.join(a['tags'])} {a['library'] or ''} "
             f"{a['license']['license'] if a['license'] else ''}", locator={"asset_id": asset_id, "t": sig["hit_s"]})
    return _row(ws.db.one("SELECT * FROM audio_analysis WHERE asset_id=?", (asset_id,)))


def _row(r):
    r = dict(r)
    for k in ("spectral", "sections", "classification", "waveform"):
        r[k] = json.loads(r.pop(f"{k}_json"))
    return r


@handler("audio_analyze")
def job_audio_analyze(ctx):
    r = analyze_audio_asset(ctx.ws, ctx.params["asset_id"], ctx.params.get("force", False))
    r["summary"] = f"{r['integrated_lufs']} LUFS, hit at {r['hit_s']}s, " + ", ".join(
        f"{l['label']} ({l['confidence']:.0%})" for l in r["classification"]["labels"])
    return r


@handler("index_audio_library")
def job_index_library(ctx):
    """Index a user-selected folder (or attached drive) into the catalog. Originals stay where they are."""
    from .assets import import_file
    ws, p = ctx.ws, ctx.params
    folder = Path(p["folder"]).expanduser()
    if not folder.exists():
        from .jobs import JobFailed
        raise JobFailed(f"{folder} is not available", hint="Is the drive connected?")
    prov = {}
    pj = folder / "provenance.json"
    if pj.exists():
        for e in json.loads(pj.read_text()):
            prov[e["sha256"]] = e
    files = [Path(r) / f for r, _, fs in os.walk(folder) for f in sorted(fs) if probe.kind_for(f) == "audio"]
    done = set(ctx.checkpoint.get("done", []))
    from .util import sha256_file
    for i, f in enumerate(files):
        if str(f) in done:
            continue
        role = p.get("role") or ("music" if "music" in str(f.parent).lower() else "sfx")
        h = sha256_file(f)
        lic = None
        if h in prov:
            e = prov[h]
            lic = {"license": e["license"], "author": e.get("author"), "source": e.get("source"),
                   "evidence": f"{pj} ({e.get('evidence', '')})",
                   "attribution": f"\"{e['title']}\" by {e.get('author')} — {e['license']}"}
        a = import_file(ws, f, project_id=p.get("project_id"), role=role, library=p.get("library", folder.name),
                        tags=[role], license=lic)
        analyze_audio_asset(ws, a["id"])
        done.add(str(f))
        ctx.save_checkpoint(done=sorted(done))
        ctx.progress((i + 1) / len(files), f"indexed {f.name}")
    ws.automation(p.get("project_id"), "audio_library_index", "automated", str(folder))
    return {"files": len(files), "summary": f"indexed {len(files)} audio files from {folder.name}"}


def record_usage(ws, asset_id: str, status: str, *, project_id=None, purpose="", note="", thread_id=None) -> dict:
    """Approve / reject / favorite an audio asset. Also saved as a structured memory so search finds it later."""
    from .assets import get_asset
    a = get_asset(ws, asset_id)
    dec = ws.remember(kind="approval" if status in ("approved", "favorite", "used") else "rejection",
                      subject=f"{'SFX' if 'sfx' in a['tags'] else 'Audio'} {status}: {a['title']}",
                      content=f"{status.capitalize()} {a['title']} for {purpose or 'this project'}. {note}".strip(),
                      project_id=project_id, thread_id=thread_id, ref_type="asset", ref_id=asset_id,
                      data={"purpose": purpose, "status": status, "hit_s": _hit(ws, asset_id)}, provenance="user")
    row = {"id": new_id("use"), "asset_id": asset_id, "project_id": project_id, "status": status, "purpose": purpose,
           "note": note, "decision_id": dec["id"], "created_at": now()}
    ws.db.insert("audio_usage", row)
    return row


def _hit(ws, asset_id):
    r = ws.db.one("SELECT hit_s FROM audio_analysis WHERE asset_id=?", (asset_id,))
    return r["hit_s"] if r else None


DESCRIPTORS = {
    "impact": ["impact_or_hit"], "hit": ["impact_or_hit"], "bass": ["bass_heavy"], "boom": ["bass_heavy"],
    "midrange": ["audible_midrange"], "mid": ["audible_midrange"], "transition": ["transition_or_riser"],
    "whoosh": ["transition_or_riser"], "ambient": ["music_or_ambience"], "underscore": ["music_or_ambience"],
    "music": ["music_or_ambience"], "short": ["impact_or_hit", "short_sfx"],
}


def find_audio(ws, request: str, *, project_id=None, role=None, status=None, limit=8) -> list[dict]:
    """Rank catalog audio for a natural request ('short bass impact with audible midrange').
    Combines measured features, labels, tags, usage history and license state."""
    words = re.findall(r"[a-z]+", request.lower())
    wanted = {l for w in words for l in DESCRIPTORS.get(w, [])}
    sql = ("SELECT a.id FROM media_assets a JOIN audio_analysis aa ON aa.asset_id=a.id WHERE a.kind='audio'")
    params: list = []
    if role:
        sql += " AND a.tags_json LIKE ?"; params.append(f'%"{role}"%')
    out = []
    from .assets import get_asset
    for r in ws.db.all(sql, params):
        a = get_asset(ws, r["id"])
        an = _row(ws.db.one("SELECT * FROM audio_analysis WHERE asset_id=?", (r["id"],)))
        labels = {l["label"]: l["confidence"] for l in an["classification"]["labels"]}
        score = sum(labels.get(l, 0) for l in wanted)
        text = f"{a['title']} {' '.join(a['tags'])}".lower()
        score += 0.3 * sum(1 for w in words if len(w) > 3 and w in text)
        if "short" in words and (a["duration"] or 99) <= 2.5:
            score += 0.3
        uses = ws.db.all("SELECT status, project_id, purpose FROM audio_usage WHERE asset_id=? ORDER BY created_at DESC",
                         (r["id"],))
        if any(u["status"] == "rejected" and (u["project_id"] == project_id or not project_id) for u in uses):
            score -= 1.0
        if any(u["status"] in ("approved", "favorite") for u in uses):
            score += 0.4
        if status and not any(u["status"] == status for u in uses):
            continue
        out.append({"asset_id": a["id"], "title": a["title"], "score": round(score, 3), "duration": a["duration"],
                    "labels": an["classification"]["labels"], "lufs": an["integrated_lufs"], "hit_s": an["hit_s"],
                    "bands": an["spectral"], "waveform": an["waveform_path"], "license": a["license"]["license"],
                    "license_verified": bool(a["license"]["verified"]), "history": uses[:5]})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]
