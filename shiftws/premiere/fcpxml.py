"""Edit plan → Final Cut Pro 7 XML (xmeml v4), which Premiere imports as a new sequence (import_fcp_xml).

One import creates the whole cut with frame-exact positions, so there is no per-clip placement to retry.
Kinetic captions are not expressible in XMEML; they go on V3 as a clearly identified overlay render.
Awaiting Mac validation: Premiere's interpretation of motion scale and audio levels must be read back.
"""
from __future__ import annotations

import math
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

from ..timecode import Rate, rescale


def _rate_xml(r: Rate) -> str:
    ntsc = r.den == 1001
    tb = round(r.frac) if ntsc else int(r.frac)
    return f"<rate><timebase>{tb}</timebase><ntsc>{'TRUE' if ntsc else 'FALSE'}</ntsc></rate>"


def _pathurl(p: str) -> str:
    return "file://localhost" + quote(str(Path(p).resolve()).replace("\\", "/"))


def _db_to_linear(db: float) -> float:
    return round(10 ** (db / 20), 5)


def plan_to_xmeml(ws, plan: dict, sequence_name: str, overlay_path: str | None = None) -> str:
    from ..assets import get_asset
    seq = plan["sequence"]
    r = Rate(**seq["fps"])
    rx = _rate_xml(r)
    files: dict[str, str] = {}

    def file_xml(asset_id: str, path_override: str | None = None) -> str:
        a = get_asset(ws, asset_id)
        fid = f"file-{asset_id}"
        if fid in files:
            return f'<file id="{fid}"/>'
        dur = r.seconds_to_frames(a["duration"] or 0)
        v = (f"<video><samplecharacteristics><width>{a['width']}</width><height>{a['height']}</height>"
             f"</samplecharacteristics></video>") if a["kind"] == "video" else ""
        au = f"<audio><channelcount>{(a['audio_streams'][0]['channels'] if a['audio_streams'] else 2)}</channelcount></audio>" \
            if a["audio_streams"] else ""
        files[fid] = a["current_path"]
        return (f'<file id="{fid}"><name>{escape(Path(path_override or a["current_path"]).name)}</name>'
                f"<pathurl>{_pathurl(path_override or a['current_path'])}</pathurl>{rx}<duration>{dur}</duration>"
                f"<media>{v}{au}</media></file>")

    def clipitem(cid, name, start, end, src_in, src_out, fxml, filters="", sourcetrack=""):
        return (f'<clipitem id="{cid}"><name>{escape(name)}</name><enabled>TRUE</enabled>{rx}'
                f"<start>{start}</start><end>{end}</end><in>{src_in}</in><out>{src_out}</out>{fxml}{sourcetrack}"
                f"{filters}</clipitem>")

    def motion(scale_pct: float) -> str:
        return ("<filter><effect><name>Basic Motion</name><effectid>basic</effectid><effectcategory>motion"
                "</effectcategory><effecttype>motion</effecttype><mediatype>video</mediatype>"
                f"<parameter><parameterid>scale</parameterid><name>Scale</name><value>{scale_pct:.3f}</value></parameter>"
                "<parameter><parameterid>center</parameterid><name>Center</name><value><horiz>0</horiz><vert>0</vert>"
                "</value></parameter></effect></filter>")

    def level(db: float) -> str:
        return ("<filter><effect><name>Audio Levels</name><effectid>audiolevels</effectid><effectcategory>audiolevels"
                "</effectcategory><effecttype>audiolevels</effecttype><mediatype>audio</mediatype>"
                f"<parameter><parameterid>level</parameterid><name>Level</name><value>{_db_to_linear(db)}</value>"
                "</parameter></effect></filter>")

    v1, a1, a2, a3, a4 = [], [], [], [], []
    for c in plan["clips"]:
        a = get_asset(ws, c["asset_id"])
        sr = Rate(**c["src_rate"])
        si = rescale(c["src_in"], sr, r)
        so = si + (c["dst_out"] - c["dst_in"])
        scale = c["transform"]["scale"] * c["transform"]["zoom"]["from"] * 100
        v1.append(clipitem(c["id"], a["title"], c["dst_in"], c["dst_out"], si, so, file_xml(c["asset_id"]), motion(scale)))
        if c["audio"].get("use_source_audio") and a["audio_streams"]:
            a2.append(clipitem(f"{c['id']}-a", a["title"], c["dst_in"], c["dst_out"], si, so, file_xml(c["asset_id"]),
                               level(c["audio"]["gain_db"]), "<sourcetrack><mediatype>audio</mediatype><trackindex>1"
                                                             "</trackindex></sourcetrack>"))
    n = plan.get("narration")
    if n:
        na = get_asset(ws, n["asset_id"])
        nd = r.seconds_to_frames(na["duration"] or 0)
        # A1 uses the read-only locked copy; unity gain; position 0 — the lock is preserved in the XML too.
        a1.append(clipitem("vo", "narration (locked)", n["dst_in"], n["dst_in"] + nd, 0, nd,
                           file_xml(n["asset_id"], n["locked_path"]), level(0.0)))
    for m in plan["music"]:
        ma = get_asset(ws, m["asset_id"])
        si = r.seconds_to_frames(m.get("src_in_s", 0))
        a3.append(clipitem(m["id"], ma["title"], m["dst_in"], m["dst_out"], si, si + m["dst_out"] - m["dst_in"],
                           file_xml(m["asset_id"]), level(m["gain_db"])))
    for s in plan["sfx"]:
        sa = get_asset(ws, s["asset_id"])
        start = s["at_frame"] - r.seconds_to_frames(float(s.get("hit_offset_s") or 0))
        ln = max(1, r.seconds_to_frames(sa["duration"] or 1))
        trim = max(0, -start)
        a4.append(clipitem(s["id"], sa["title"], max(0, start), max(0, start) + ln - trim, trim, ln, file_xml(s["asset_id"]),
                           level(s["gain_db"])))
    v3 = []
    if overlay_path:
        v3.append(f'<clipitem id="captions-overlay"><name>kinetic captions overlay (render)</name><enabled>TRUE</enabled>{rx}'
                  f"<start>0</start><end>{seq['duration_frames']}</end><in>0</in><out>{seq['duration_frames']}</out>"
                  f'<file id="file-captions"><name>{escape(Path(overlay_path).name)}</name><pathurl>{_pathurl(overlay_path)}'
                  f"</pathurl>{rx}<duration>{seq['duration_frames']}</duration><media><video><samplecharacteristics>"
                  f"<width>{seq['width']}</width><height>{seq['height']}</height></samplecharacteristics></video></media>"
                  f"</file></clipitem>")
    track = lambda items: f"<track>{''.join(items)}</track>"  # noqa: E731
    vtracks = track(v1) + ("<track/>" if v3 else "") + (track(v3) if v3 else "")
    atracks = "".join(track(t) for t in (a1, a2, a3, a4))
    markers = "".join(f"<marker><name>{escape(m['name'])}</name><comment>{escape(m.get('comment', ''))}</comment>"
                      f"<in>{m['frame']}</in><out>-1</out></marker>" for m in plan.get("markers", []))
    fmt = (f"<format><samplecharacteristics>{rx}<width>{seq['width']}</width><height>{seq['height']}</height>"
           "<pixelaspectratio>square</pixelaspectratio><fielddominance>none</fielddominance></samplecharacteristics></format>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
            f'<sequence id="shiftws-seq"><name>{escape(sequence_name)}</name><duration>{seq["duration_frames"]}</duration>{rx}'
            f"<media><video>{fmt}{vtracks}</video><audio>{atracks}</audio></media>{markers}</sequence></xmeml>\n")
