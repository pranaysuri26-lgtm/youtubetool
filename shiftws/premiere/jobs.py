"""Premiere jobs (lane 'premiere' → serialized across processes: one writer).

premiere_apply: save → find-or-create sequence by deterministic name (idempotent: a timeout never yields a
duplicate) → import plan XML → read back tracks → verify against the plan → save. Each step is a premiere_ops
row with an operation id, preconditions and verified postconditions.
premiere_inspect: read-only repair inspection (tracks, offline media, narration placement).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..jobs import JobFailed, handler
from ..timecode import Rate
from ..util import new_id, now, run
from .adapter import BridgeClient, bridge_command, plan_operations


def _op(ws, pid, plan_id, op, params, pre=None, post=None, seq=None):
    oid = f"op_{plan_id[-6:]}_{op}_{abs(hash(json.dumps(params, sort_keys=True, default=str))) % 10**8}"
    ex = ws.db.one("SELECT * FROM premiere_ops WHERE id=?", (oid,))
    if not ex:
        ws.db.insert("premiere_ops", {"id": oid, "project_id": pid, "plan_version_id": plan_id, "sequence_ref": seq, "op": op,
                                      "params_json": params, "precondition_json": pre or {}, "postcondition_json": post or {},
                                      "status": "pending", "created_at": now(), "updated_at": now()})
    return oid


def _mark(ws, oid, status, result):
    ws.db.x("UPDATE premiere_ops SET status=?, attempts=attempts+1, result_json=?, updated_at=? WHERE id=?",
            (status, json.dumps(result, default=str)[:20000], now(), oid))


def captions_overlay(ws, plan: dict, dest: Path) -> Path:
    """Transparent ProRes 4444 overlay with the kinetic captions (for V3 in Premiere)."""
    from ..render import kinetic_ass
    seq = plan["sequence"]
    r = Rate(**seq["fps"])
    ass = dest.with_suffix(".ass")
    kinetic_ass(plan, ass)
    dur = r.frames_to_seconds(seq["duration_frames"])
    a = str(ass).replace("\\", "/").replace(":", r"\:")
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
         f"color=c=black@0.0:s={seq['width']}x{seq['height']}:r={r.num}/{r.den}:d={dur:.6f},format=rgba",
         "-vf", f"subtitles='{a}':alpha=1", "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
         str(dest)], timeout=3600)
    return dest


def _sequences(cli) -> list[dict]:
    r = cli.call("list_sequences", {})
    d = r["data"]
    if isinstance(d, dict):
        d = d.get("sequences") or d.get("data") or []
    return d if isinstance(d, list) else []


@handler("premiere_apply", lane="premiere")
def job_apply(ctx):
    from ..editplan import validate
    from ..projects import get_project
    from .fcpxml import plan_to_xmeml
    ws = ctx.ws
    row = ws.db.one("SELECT * FROM edit_plan_versions WHERE id=?", (ctx.params["plan_id"],))
    plan = json.loads(row["plan_json"])
    pid = row["project_id"]
    v = validate(ws, plan)
    if not v["valid"]:
        raise JobFailed("edit plan is invalid", hint="; ".join(v["errors"][:5]))
    cmd = bridge_command(ws)
    if not cmd:
        raise JobFailed("Premiere bridge not installed", hint="Connections → Premiere. Only Premiere jobs are blocked.")
    cli = BridgeClient(cmd, timeout=float(ws.cfg.get("premiere.timeout", 90)))
    info = plan_operations(ws, {**row, "plan": plan})
    name = info["sequence_name"]
    root = Path(get_project(ws, pid)["root_dir"]) / "premiere"
    root.mkdir(parents=True, exist_ok=True)
    # 1. connection (read-only; does not launch Premiere)
    oid = _op(ws, pid, row["id"], "connection_check", {})
    c = cli.call("verify_premiere_connection", {"launchIfNeeded": False}, via_catalog=False)
    _mark(ws, oid, "verified" if not c["is_error"] else "failed", c)
    if c["is_error"]:
        raise JobFailed("Premiere is not connected", hint=json.dumps(c["data"])[:400])
    ctx.progress(0.1, "saving project before changes", stage="save")
    oid = _op(ws, pid, row["id"], "save", {"when": "before"})
    _mark(ws, oid, "verified", cli.call("save_project", {}))
    # 2. idempotency: does the target sequence already exist (e.g. a previous attempt timed out after succeeding)?
    existing = [s for s in _sequences(cli) if s.get("name") == name]
    if not existing:
        overlay = None
        if plan["captions"]["cues"] and plan["captions"]["style"].get("style") == "kinetic_word":
            ctx.progress(0.2, "rendering kinetic caption overlay", stage="captions")
            overlay = str(captions_overlay(ws, plan, root / f"captions_overlay_v{row['version']:02d}.mov"))
        xml = root / f"plan_v{row['version']:02d}_{row['id'][-6:]}.xml"
        xml.write_text(plan_to_xmeml(ws, plan, name, overlay))
        oid = _op(ws, pid, row["id"], "import_xml", {"filePath": str(xml)}, post={"sequence_name": name})
        ctx.progress(0.4, "importing the plan as a new sequence", stage="import")
        try:
            r = cli.call("import_fcp_xml", {"filePath": str(xml)})
        except TimeoutError:
            r = {"is_error": True, "data": "timeout"}
        # after ANY outcome, inspect actual state before deciding (the import may have succeeded)
        after = [s for s in _sequences(cli) if s.get("name") == name]
        if len(after) > 1:
            _mark(ws, oid, "failed", {"duplicates": after})
            raise JobFailed("duplicate sequences detected", hint="Remove the extra copy manually; nothing else was changed.")
        if not after:
            _mark(ws, oid, "failed", r)
            raise JobFailed("XML import did not create the sequence", hint=json.dumps(r["data"])[:400], retryable=True)
        _mark(ws, oid, "verified", r)
        existing = after
    seq = existing[0]
    seq_id = seq.get("id") or seq.get("sequenceId")
    ctx.progress(0.7, "reading back the sequence", stage="readback")
    oid = _op(ws, pid, row["id"], "read_tracks", {"sequenceId": seq_id}, seq=seq_id)
    tr = cli.call("list_sequence_tracks", {"sequenceId": seq_id})
    rb = reconcile(plan, tr["data"])
    _mark(ws, oid, "verified" if rb["matches"] else "failed", {"readback": rb})
    oid = _op(ws, pid, row["id"], "save", {"when": "after"}, seq=seq_id)
    _mark(ws, oid, "verified", cli.call("save_project", {}))
    ws.remember(kind="fact", subject=f"Premiere sequence for plan v{row['version']}",
                content=f"Sequence '{name}' ({seq_id}); readback {'matches' if rb['matches'] else 'differs'}: {rb['summary']}",
                project_id=pid, ref_type="sequence", ref_id=seq_id,
                data={**rb, "deliverable_id": row["deliverable_id"], "plan_id": row["id"], "sequence_name": name},
                provenance="premiere")
    ws.automation(pid, "premiere_assembly", "automated")
    if not rb["matches"]:
        raise JobFailed("sequence differs from the plan after import", hint=rb["summary"])
    return {"sequence_id": seq_id, "sequence_name": name, "readback": rb, "deliverable_id": row["deliverable_id"],
            "plan_id": row["id"], "summary": f"Premiere: '{name}' verified"}


def reconcile(plan: dict, tracks_data) -> dict:
    """Compare plan vs Premiere readback: clip counts per track, V1 positions, narration on A1.
    Clips present in Premiere but not in the plan are reported as manual edits — never overwritten."""
    d = tracks_data if isinstance(tracks_data, dict) else {}
    vt = d.get("videoTracks") or d.get("video") or []
    at = d.get("audioTracks") or d.get("audio") or []
    rate = Rate(**plan["sequence"]["fps"])
    v1 = (vt[0].get("clips") if vt and isinstance(vt[0], dict) else []) or []
    a1 = (at[0].get("clips") if at and isinstance(at[0], dict) else []) or []
    expected = sorted(plan["clips"], key=lambda c: c["dst_in"])
    pos_err = []
    for c, got in zip(expected, sorted(v1, key=lambda x: float(x.get("start", x.get("startTime", 0)) or 0))):
        t = float(got.get("start", got.get("startTime", 0)) or 0)
        if abs(t - rate.frames_to_seconds(c["dst_in"])) > 1.5 / float(rate.frac):
            pos_err.append({"clip": c["id"], "expected_s": rate.frames_to_seconds(c["dst_in"]), "got_s": t})
    extra = max(0, len(v1) - len(expected))
    ok = len(v1) == len(expected) and not pos_err and (bool(a1) == bool(plan.get("narration")))
    return {"matches": ok, "v1_expected": len(expected), "v1_found": len(v1), "position_errors": pos_err[:10],
            "manual_extra_clips": extra, "narration_on_A1": bool(a1),
            "summary": f"V1 {len(v1)}/{len(expected)} clips, {len(pos_err)} position error(s), "
                       f"narration on A1: {bool(a1)}, extra clips: {extra}"}


@handler("premiere_inspect", lane="premiere")
def job_inspect(ctx):
    """Repair-mode inspection: actual tracks, mute state, offline media, narration placement. Read-only."""
    ws = ctx.ws
    cmd = bridge_command(ws)
    if not cmd:
        raise JobFailed("Premiere bridge not installed")
    cli = BridgeClient(cmd, timeout=60)
    seq_id = ctx.params["sequence_id"]
    tracks = cli.call("list_sequence_tracks", {"sequenceId": seq_id})["data"]
    offline = cli.call("check_offline_media", {})["data"]
    d = tracks if isinstance(tracks, dict) else {}
    at = d.get("audioTracks") or []
    diag = []
    if at:
        a1 = at[0]
        if a1.get("muted") or a1.get("isMuted"):
            diag.append("A1 (narration) track is muted — routing/mute, not source silence")
        if not a1.get("clips"):
            diag.append("A1 has no clips — narration missing from the sequence")
    if offline and json.dumps(offline).lower().count("offline") > 1:
        diag.append("offline media reported — relink before rendering")
    return {"tracks": tracks, "offline": offline, "diagnosis": diag or ["no structural problem found; render and measure "
                                                                        "the affected interval next"],
            "summary": "; ".join(diag) or "inspection clean (verify by render)"}


@handler("premiere_export", lane="premiere")
def job_export(ctx):
    """Export a verified sequence through Premiere/Media Encoder, then inspect the ACTUAL file (never trust the tool's
    answer alone): wait until the file stops growing, probe it, register it as an export of the right deliverable and
    plan version, and run the same independent QC as FFmpeg renders."""
    import time as _t
    from ..probe import summarize
    from ..util import sha256_file
    ws, p = ctx.ws, ctx.params
    row = ws.db.one("SELECT * FROM edit_plan_versions WHERE id=?", (p["plan_id"],))
    pid = row["project_id"]
    cmd = bridge_command(ws)
    if not cmd:
        raise JobFailed("Premiere bridge not installed", hint="Only Premiere jobs are blocked.")
    cli = BridgeClient(cmd, timeout=float(ws.cfg.get("premiere.timeout", 90)))
    from ..projects import get_project
    out = Path(p.get("output_path") or (Path(get_project(ws, pid)["root_dir"]) / "exports" /
                                         f"premiere_plan_v{row['version']:02d}_{row['id'][-6:]}.mp4"))
    if out.exists():
        raise JobFailed(f"{out.name} already exists", hint="Exports are never overwritten; pass another output_path.")
    args = {"sequenceId": p["sequence_id"], "outputPath": str(out), "allowOverwrite": False}
    if p.get("preset_path"):
        args["presetPath"] = p["preset_path"]
    elif p.get("preset_name"):
        args["presetName"] = p["preset_name"]
    else:
        raise JobFailed("an export preset is required", hint="Pass preset_path (.epr) or preset_name (see get_encoder_presets).")
    oid = _op(ws, pid, row["id"], "export", args, seq=p["sequence_id"])
    ctx.progress(0.1, "asking Premiere / Media Encoder to export", stage="export")
    try:
        r = cli.call("export_sequence", args)
    except TimeoutError:
        r = {"is_error": False, "data": "timeout — checking the file instead"}
    # completion = the file exists and its size is stable for 3 polls (Media Encoder may still be writing)
    last, stable = -1, 0
    deadline = _t.time() + float(p.get("timeout_s", 3600))
    while _t.time() < deadline and stable < 3:
        size = out.stat().st_size if out.exists() else -1
        stable = stable + 1 if size > 0 and size == last else 0
        last = size
        ctx.progress(0.2 + 0.6 * min(1.0, stable / 3), f"waiting for {out.name} ({max(size, 0) / 1e6:.1f} MB)")
        _t.sleep(2)
    if not out.exists() or stable < 3:
        _mark(ws, oid, "failed", {"bridge": r, "file": str(out), "exists": out.exists()})
        raise JobFailed("Premiere reported an export but no complete file appeared", hint=json.dumps(r.get("data"))[:300])
    info = summarize(out)
    _mark(ws, oid, "verified", {"bridge": r, "probe": info})
    eid = new_id("exp")
    ws.db.insert("exports", {"id": eid, "project_id": pid, "plan_version_id": row["id"], "deliverable_id": row["deliverable_id"],
                             "kind": "premiere_export", "path": str(out), "sha256": sha256_file(out), "probe_json": info,
                             "status": "rendered", "created_at": now()})
    from ..jobs import enqueue
    qc = enqueue(ws, "qc", {"export_id": eid}, project_id=pid, thread_id=ctx.job["thread_id"], origin="system")
    ws.automation(pid, "premiere_export", "automated")
    return {"export_id": eid, "path": str(out), "probe": {k: info.get(k) for k in ("duration", "width", "height", "fps_num",
                                                                                    "fps_den", "vcodec", "acodec")},
            "qc_job": qc["id"], "deliverable_id": row["deliverable_id"],
            "summary": f"Premiere export {out.name}: {info['width']}x{info['height']} {info['duration']:.2f}s — QC started",
            "_outputs": [str(out)]}
