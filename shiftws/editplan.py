"""Canonical, machine-readable edit plan — created and validated before anything touches Premiere.

Timing is integer frames: source positions in the SOURCE's rational rate, destination positions in the
SEQUENCE rate. Seconds are derived for display only. Every clip/cue has a stable id so revisions like
"replace the music" or "change the shot under one sentence" touch only what they name.
"""
from __future__ import annotations

import copy
import json
import re
from fractions import Fraction
from pathlib import Path

from .timecode import Rate
from .util import hash_obj, new_id, now

SCHEMA = "shiftws.editplan/1"


def _seq(ws, project_id, preset_id: str | None = None) -> dict:
    from .projects import resolve_preferences
    prefs = {k: v["value"] for k, v in resolve_preferences(ws, project_id).items()}
    if preset_id:  # a deliverable can use a different format than the project's first one
        prefs.update(json.loads(ws.db.one("SELECT settings_json FROM format_presets WHERE id=?", (preset_id,))["settings_json"]))
        for r in ws.db.all("SELECT key, value_json FROM preferences WHERE scope='project' AND scope_id=? AND active=1",
                           (project_id,)):
            prefs[r["key"]] = json.loads(r["value_json"])
    r = Rate.parse(prefs.get("fps", "30/1"))
    return {"width": prefs.get("width", 1920), "height": prefs.get("height", 1080), "fps": r.to_dict(),
            "prefs": prefs}


def _src_rate(a) -> Rate:
    return Rate(a["fps_num"], a["fps_den"]) if a.get("fps_num") else Rate(30, 1)


def create_plan(ws, project_id: str, *, music_asset_id=None, sfx_asset_ids=None, notes="", origin="user",
                deliverable_id: str | None = None) -> dict:
    from .assets import get_asset, project_assets
    from .narration import locked_take
    from .projects import get_project
    from .scripts_eleven import get_script
    from .projects import get_deliverable
    p = get_project(ws, project_id)
    dlv = get_deliverable(ws, deliverable_id, project_id)
    s = _seq(ws, project_id, dlv["format_preset"])
    prefs = s.pop("prefs")
    seq_rate = Rate(**s["fps"])
    narrated = bool(ws.db.one("SELECT narrated FROM format_presets WHERE id=?", (dlv["format_preset"],))["narrated"])
    take = locked_take(ws, project_id, dlv["id"])
    if narrated and not take:
        raise ValueError("Lock a narration take before building the edit plan (the plan references it exactly).")
    script = get_script(ws, project_id=project_id, deliverable_id=dlv["id"]) or get_script(ws, project_id=project_id)
    footage = [a for a in project_assets(ws, project_id) if a["kind"] == "video" and a["role"] in ("footage", "download")]
    if not footage:
        raise ValueError("No footage in this project yet.")
    vo_dur = take["duration"] if take else None
    total_s = (vo_dur + 0.6) if vo_dur else float(dlv["length_seconds"] or p["brief"].get("length_seconds") or 30)
    total_f = seq_rate.seconds_to_frames(total_s, "ceil")

    # ---- section windows on the sequence (from narration alignment, else evenly from the script)
    windows = []
    if take and take["alignment"].get("sections"):
        for sec in take["alignment"]["sections"]:
            windows.append({"section_id": sec["section_id"], "label": sec["label"], "start": sec["start"], "end": sec["end"]})
        windows[0]["start"] = 0.0
        for i in range(len(windows) - 1):
            windows[i]["end"] = windows[i + 1]["start"]
        windows[-1]["end"] = total_s
    elif script:
        secs = script["doc"]["sections"]
        step = total_s / len(secs)
        windows = [{"section_id": x["id"], "label": x["label"], "start": i * step, "end": (i + 1) * step}
                   for i, x in enumerate(secs)]
    else:
        windows = [{"section_id": "all", "label": "body", "start": 0.0, "end": total_s}]
    sec_text = {x["id"]: x for x in (script["doc"]["sections"] if script else [])}

    # ---- footage selection per section (evidence first, then sequential main-story footage)
    used: list[tuple[str, float]] = []
    pool = _scene_pool(ws, footage)
    shot_len = 3.0 if s["height"] > s["width"] else 4.5
    clips, markers = [], []
    for w in windows:
        markers.append({"id": f"m-{w['section_id']}", "frame": seq_rate.seconds_to_frames(w["start"]),
                        "name": w["label"], "comment": (sec_text.get(w["section_id"], {}).get("clean", "")[:120])})
        t = w["start"]
        while t < w["end"] - 0.05:
            d = min(shot_len, w["end"] - t)
            if w["end"] - (t + d) < 1.0:
                d = w["end"] - t
            pick = _pick_scene(ws, pool, used, sec_text.get(w["section_id"]), d, project_id)
            a = pick["asset"]
            sr = _src_rate(a)
            src_in = sr.seconds_to_frames(pick["src_start"])
            src_out = src_in + sr.seconds_to_frames(d, "ceil")
            dst_in = seq_rate.seconds_to_frames(t)
            dst_out = seq_rate.seconds_to_frames(t + d)
            clips.append({
                "id": f"c{len(clips) + 1:03d}", "track": "V1", "asset_id": a["id"], "src_rate": sr.to_dict(),
                "src_in": src_in, "src_out": src_out, "dst_in": dst_in, "dst_out": dst_out,
                "transform": _transform(a, s, pick.get("metrics", {}), len(clips), prefs),
                "audio": {"use_source_audio": True, "track": "A2",
                          "gain_db": -20.0 if narrated else -3.0},
                "purpose": w["label"],
                "evidence": {"section_id": w["section_id"], "scene_id": pick.get("scene_id"), "basis": pick["basis"],
                             "confidence": pick.get("confidence")}})
            used.append((a["id"], pick["src_start"]))
            t += d
    # ---- audio cues
    music = []
    # Audio policy: suggest_only_until_selected. Music enters the plan only when explicitly passed or selected
    # (approved) in this project — never because the catalog contains something suitable.
    music_on = music_asset_id or ((prefs.get("music") or {}).get("enabled") and _approved(ws, project_id, "music"))
    if music_on:
        mid = music_asset_id or _approved(ws, project_id, "music")
        if mid:
            ma = get_asset(ws, mid)
            music.append({"id": "mus1", "asset_id": mid, "track": "A3", "dst_in": 0, "dst_out": total_f,
                          "src_in_s": 0.0, "gain_db": -18.0, "fade_in_s": 0.8, "fade_out_s": 1.5,
                          "duck_under_vo": bool(take), "purpose": "underscore",
                          "license": ma["license"]["license"] if ma["license"] else "unverified"})
    # SFX: only assets the user selected for this project; each placed once, where its stated purpose says
    # (e.g. "hook impact" → the hook→setup transition), aligned on the sound's measured hit point.
    sfx = []
    sel = [{"asset_id": a, "purpose": "selected"} for a in (sfx_asset_ids or [])] or _selected_sfx(ws, project_id)
    sfx_on = (prefs.get("sfx") or {}).get("enabled", True)
    gain = float((prefs.get("sfx") or {}).get("gain_db", -16))
    if sfx_on and sel:
        by_label = {w["label"]: w for w in windows}
        for i, s_ in enumerate(sel):
            purpose = s_["purpose"].lower()
            target = next((w for lbl, w in by_label.items() if lbl in purpose and lbl != "hook"), None)
            if target is None:
                target = windows[1] if len(windows) > 1 else windows[0]   # hook accent → hook/setup transition
            hit = (ws.db.one("SELECT hit_s FROM audio_analysis WHERE asset_id=?", (s_["asset_id"],)) or {}).get("hit_s") or 0.0
            sfx.append({"id": f"sfx{i + 1}", "asset_id": s_["asset_id"], "track": "A4",
                        "at_frame": seq_rate.seconds_to_frames(target["start"]), "hit_offset_s": hit, "gain_db": gain,
                        "purpose": s_["purpose"]})
    captions = _captions(ws, take, seq_rate, prefs) if take else {"style": prefs.get("captions", {}), "cues": []}
    plan = {"schema": SCHEMA, "project_id": project_id, "deliverable_id": dlv["id"],
            "sequence": {"width": s["width"], "height": s["height"], "fps": s["fps"], "duration_frames": total_f,
                         "name": f"{dlv['title']}"},
            "narration": ({"take_id": take["id"], "asset_id": take["asset_id"], "sha256": take["original_sha256"],
                           "locked_path": take["locked_path"], "track": "A1", "dst_in": 0, "gain_db": 0.0, "locked": True}
                          if take else None),
            "script_id": script["id"] if script else None,
            "clips": clips, "music": music, "sfx": sfx, "captions": captions, "markers": markers,
            "transitions": [], "notes": notes}
    return _save(ws, project_id, plan, "initial plan", [], origin)


def _scene_pool(ws, footage) -> list[dict]:
    pool = []
    for a in footage:
        rows = ws.db.all("SELECT * FROM scenes WHERE asset_id=? ORDER BY start", (a["id"],))
        if not rows:
            pool.append({"asset": a, "scene_id": None, "start": 0.0, "end": a["duration"] or 0, "metrics": {},
                         "description": "", "confidence": None})
        for r in rows:
            m = json.loads(r["metrics_json"])
            pool.append({"asset": a, "scene_id": r["id"], "start": r["start"], "end": r["end"], "metrics": m,
                         "description": r["description"], "confidence": r["confidence"], "frame": r["frame_path"]})
    return pool


def _pick_scene(ws, pool, used, section, d, project_id) -> dict:
    """1) a scene whose inspected description or transcript matches the section; 2) next unused, non-title,
    non-black scene in source order (main-story preference). The basis is recorded honestly."""
    if section:
        from .analysis import find_footage
        q = " ".join(re.findall(r"[A-Za-z]{5,}", section.get("clean", "")))[:200]
        for h in find_footage(ws, q, project_id, limit=5) if q else []:
            if h["basis"] == "vision description" and (h["confidence"] or 0) >= 0.6:
                for c in pool:
                    if c["scene_id"] and c["asset"]["id"] == h["asset_id"] and abs(c["start"] - (h["start"] or -1)) < 0.01 \
                            and (c["asset"]["id"], c["start"]) not in used and (c["asset"]["duration"] or 0) - c["start"] >= d:
                        return {**c, "src_start": c["start"], "basis": "vision description matches section",
                                "confidence": h["confidence"]}
    for c in pool:
        m = c["metrics"]
        title_like = m.get("pillarbox_frac", 0) > 0.05 or m.get("black_overlap", 0) > 0.3
        room = (c["asset"]["duration"] or 0) - c["start"]
        if (c["asset"]["id"], c["start"]) in used or title_like or room < d:
            continue
        return {**c, "src_start": c["start"], "basis": "sequential footage (uninspected) — review shot choice"}
    c = pool[len(used) % len(pool)]
    return {**c, "src_start": max(0.0, min(c["start"], (c["asset"]["duration"] or d) - d)),
            "basis": "reused footage (pool exhausted) — review"}


def _transform(a, seq, metrics, i, prefs) -> dict:
    """Aspect-preserving crop-to-fill for vertical; never letterbox unless the preset allows intentional bars."""
    sw, sh = a["width"] or seq["width"], a["height"] or seq["height"]
    tw, th = seq["width"], seq["height"]
    active_w = sw * (1 - 2 * metrics.get("pillarbox_frac", 0))
    mode = "fit" if prefs.get("bars") == "intentional_letterbox_allowed" else "fill"
    scale = max(tw / active_w, th / sh) if mode == "fill" else min(tw / sw, th / sh)
    z = (prefs.get("zoom") or {}).get("max_scale", 1.05)
    zoom = {"from": 1.0, "to": z} if i % 2 == 0 else {"from": z, "to": 1.0}
    return {"mode": mode, "scale": round(scale, 5), "focus_x": 0.5, "focus_y": 0.5, "zoom": zoom}


def _captions(ws, take, seq_rate, prefs) -> dict:
    style = dict(prefs.get("captions") or {})
    if style.get("style") in (None, "none"):
        return {"style": style, "cues": []}
    from .narration import script_word_timings
    words, source = script_word_timings(ws, take)
    words = [{"word": w["w"], "start": w["start"], "end": w["end"], "src": w["src"]} for w in words]
    group = int(style.get("max_words_on_screen", 3)) if style.get("style") == "kinetic_word" else 7
    cues, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        brk = len(cur) >= group or (nxt and nxt["start"] - w["end"] > 0.45) or w["word"][-1:] in ".!?,"
        if brk or not nxt:
            cues.append({"id": f"cap{len(cues) + 1:03d}", "start": seq_rate.seconds_to_frames(cur[0]["start"]),
                         "end": seq_rate.seconds_to_frames(cur[-1]["end"]),
                         "words": [{"w": x["word"], "start": seq_rate.seconds_to_frames(x["start"]),
                                    "end": seq_rate.seconds_to_frames(x["end"]), "timing": x["src"]} for x in cur]})
            cur = []
    interp = sum(1 for w in words if w["src"] == "interpolated")
    return {"style": style, "cues": cues,
            "source": (f"approved script words timed by narration take {take['take_no']}"
                       + (f" ({interp} word(s) interpolated — check them)" if interp else "")) if source == "script"
            else f"narration take {take['take_no']} ASR words (no script linked)"}


def _selected_sfx(ws, project_id) -> list[dict]:
    rows = ws.db.all("SELECT u.asset_id, u.purpose FROM audio_usage u JOIN media_assets a ON a.id=u.asset_id WHERE "
                     "u.status IN ('approved','favorite') AND u.project_id=? AND a.tags_json LIKE '%\"sfx\"%' AND "
                     "u.asset_id NOT IN (SELECT asset_id FROM audio_usage WHERE status='rejected' AND project_id=? "
                     "AND created_at > u.created_at) ORDER BY u.created_at", (project_id, project_id))
    seen, out = set(), []
    for r in rows:
        if r["asset_id"] not in seen:
            seen.add(r["asset_id"])
            out.append({"asset_id": r["asset_id"], "purpose": r["purpose"] or "accent"})
    return out


def _approved(ws, project_id, role) -> str | None:
    r = ws.db.one("SELECT u.asset_id FROM audio_usage u JOIN media_assets a ON a.id=u.asset_id WHERE u.status IN "
                  "('approved','favorite') AND a.tags_json LIKE ? AND u.project_id=? "
                  "AND u.asset_id NOT IN (SELECT asset_id FROM audio_usage WHERE status='rejected' AND project_id=?) "
                  "ORDER BY (u.project_id=?) DESC, u.created_at DESC LIMIT 1",
                  (f'%"{role}"%', project_id, project_id, project_id))
    # Selection is per project: a favorite elsewhere is a suggestion here, never an automatic placement.
    return r["asset_id"] if r else None


# ---------------------------------------------------------------------- validation

def validate(ws, plan: dict) -> dict:
    from .assets import get_asset
    from .narration import get_take
    errors, warnings = [], []
    if plan.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    seq = plan["sequence"]
    total = seq["duration_frames"]
    for k in ("width", "height", "duration_frames"):
        if not isinstance(seq.get(k), int) or seq[k] <= 0:
            errors.append(f"sequence.{k} must be a positive integer")
    ids = set()
    tracks: dict[str, list] = {}
    for c in plan["clips"]:
        if c["id"] in ids:
            errors.append(f"duplicate id {c['id']}")
        ids.add(c["id"])
        for k in ("src_in", "src_out", "dst_in", "dst_out"):
            if not isinstance(c[k], int):
                errors.append(f"{c['id']}.{k} must be integer frames (got {c[k]!r})")
        a = get_asset(ws, c["asset_id"])
        if not a:
            errors.append(f"{c['id']}: asset {c['asset_id']} missing")
            continue
        if a["availability"] != "online" or not Path(a["current_path"]).exists():
            errors.append(f"{c['id']}: {a['title']} is offline")
        sr = Rate(**c["src_rate"])
        if a["duration"] and sr.frames_to_seconds(c["src_out"]) > a["duration"] + 0.05:
            errors.append(f"{c['id']}: source out {sr.frames_to_seconds(c['src_out']):.2f}s beyond {a['title']} ({a['duration']:.2f}s)")
        if c["src_out"] <= c["src_in"] or c["dst_out"] <= c["dst_in"]:
            errors.append(f"{c['id']}: empty range")
        if c["dst_out"] > total:
            errors.append(f"{c['id']}: ends after the sequence")
        if c["transform"]["mode"] == "fit" and (a["width"] or 0) / max(1, a["height"] or 1) != seq["width"] / seq["height"]:
            warnings.append(f"{c['id']}: 'fit' leaves bars — allowed only for intentional letterbox")
        if c["evidence"]["basis"].startswith(("sequential", "reused")):
            warnings.append(f"{c['id']}: shot not backed by inspected evidence ({c['evidence']['basis']})")
        tracks.setdefault(c["track"], []).append(c)
    for tr, cs in tracks.items():
        cs = sorted(cs, key=lambda x: x["dst_in"])
        for x, y in zip(cs, cs[1:]):
            if y["dst_in"] < x["dst_out"]:
                errors.append(f"{tr}: {x['id']} overlaps {y['id']}")
            elif y["dst_in"] > x["dst_out"]:
                warnings.append(f"{tr}: gap of {y['dst_in'] - x['dst_out']} frames between {x['id']} and {y['id']}")
        if tr == "V1" and cs and (cs[0]["dst_in"] > 0 or cs[-1]["dst_out"] < total):
            warnings.append("V1 does not cover the full sequence (black at start/end)")
    n = plan.get("narration")
    if n:
        t = get_take(ws, n["take_id"])
        if t["status"] != "locked":
            errors.append("narration take is not locked")
        if n["sha256"] != t["original_sha256"]:
            errors.append("narration reference does not match the locked take")
        from .util import sha256_file
        if not n.get("locked_path") or not Path(n["locked_path"]).exists() or sha256_file(n["locked_path"]) != n["sha256"]:
            errors.append("locked narration file missing or changed")
        if n["gain_db"] != 0.0 or n["dst_in"] != 0:
            errors.append("narration gain/position changed — locked narration must stay as approved")
    for cue in plan["music"] + plan["sfx"]:
        a = get_asset(ws, cue["asset_id"])
        if not a:
            errors.append(f"{cue['id']}: asset missing")
            continue
        if not (-60 <= cue["gain_db"] <= 6):
            errors.append(f"{cue['id']}: gain {cue['gain_db']} dB out of range")
        lic = a["license"]["license"] if a["license"] else "unverified"
        if lic == "unverified":
            warnings.append(f"{cue['id']}: {a['title']} is license-unverified")
    for c in plan["captions"]["cues"]:
        if c["end"] <= c["start"] or c["end"] > total:
            errors.append(f"{c['id']}: caption timing invalid")
    return {"valid": not errors, "errors": errors, "warnings": warnings}


# ---------------------------------------------------------------------- persistence & revisions

def _save(ws, project_id, plan, summary, changed, origin) -> dict:
    last = ws.db.one("SELECT id, version FROM edit_plan_versions WHERE project_id=? ORDER BY version DESC LIMIT 1",
                     (project_id,))
    dl = plan.get("deliverable_id")
    v = (last["version"] + 1) if last else 1
    pid_ = new_id("plan")
    plan = {**plan, "version": v}
    val = validate(ws, plan)
    with ws.db.tx():
        ws.db.insert("edit_plan_versions", {"id": pid_, "project_id": project_id, "version": v, "deliverable_id": dl,
                                            "parent_id": last["id"] if last else None, "plan_json": plan,
                                            "validation_json": val, "change_summary": summary,
                                            "changed_ids_json": changed, "status": "valid" if val["valid"] else "invalid",
                                            "created_at": now()})
        for c in plan["clips"]:
            ws.db.insert("timeline_clips", {"plan_id": pid_, "clip_id": c["id"], "track": c["track"],
                                            "asset_id": c["asset_id"], "src_in_frames": c["src_in"],
                                            "src_out_frames": c["src_out"], "dst_frames": c["dst_in"], "data_json": c})
        for c in plan["music"]:
            ws.db.insert("music_cues", {"plan_id": pid_, "cue_id": c["id"], "asset_id": c["asset_id"], "data_json": c})
        for c in plan["sfx"]:
            ws.db.insert("sfx_cues", {"plan_id": pid_, "cue_id": c["id"], "asset_id": c["asset_id"], "data_json": c})
        for c in plan["captions"]["cues"]:
            ws.db.insert("caption_cues", {"plan_id": pid_, "cue_id": c["id"], "data_json": c})
    from .projects import get_project, set_stage, write_manifest
    d = Path(get_project(ws, project_id)["root_dir"]) / "edit-plans"
    (d / f"plan_v{v:02d}.json").write_text(json.dumps(plan, indent=1))
    ws.remember(kind="decision", subject=f"Edit plan v{v}", content=summary + (f" (changed: {', '.join(changed)})" if changed else ""),
                project_id=project_id, ref_type="plan", ref_id=pid_, data={"changed": changed, "valid": val["valid"]},
                provenance=origin)
    set_stage(ws, project_id, "edit_plan")
    ws.automation(project_id, "edit_plan" if not changed else "edit_revision", "automated", summary)
    write_manifest(ws, project_id)
    return {"plan_id": pid_, "version": v, "validation": val, "changed": changed, "file": str(d / f"plan_v{v:02d}.json"),
            "clips": len(plan["clips"]), "music": len(plan["music"]), "sfx": len(plan["sfx"]),
            "captions": len(plan["captions"]["cues"])}


def get_plan(ws, project_id: str, version: int | None = None, deliverable_id: str | None = None) -> dict:
    if version:
        r = ws.db.one("SELECT * FROM edit_plan_versions WHERE project_id=? AND version=?", (project_id, version))
    elif deliverable_id:
        r = ws.db.one("SELECT * FROM edit_plan_versions WHERE deliverable_id=? ORDER BY version DESC LIMIT 1",
                      (deliverable_id,))
    else:
        r = ws.db.one("SELECT * FROM edit_plan_versions WHERE project_id=? ORDER BY version DESC LIMIT 1", (project_id,))
    if not r:
        raise ValueError("No edit plan yet — create one first.")
    r["plan"] = json.loads(r.pop("plan_json"))
    r["validation"] = json.loads(r.pop("validation_json"))
    r["changed_ids"] = json.loads(r.pop("changed_ids_json"))
    return r


def revise_plan(ws, project_id: str, operations: list[dict], reason: str, origin="user",
                deliverable_id: str | None = None) -> dict:
    """Apply normalized operations by stable id; untouched clips/cues are copied verbatim."""
    cur = get_plan(ws, project_id, deliverable_id=deliverable_id)
    plan = copy.deepcopy(cur["plan"])
    changed: list[str] = []
    seq_rate = Rate(**plan["sequence"]["fps"])
    for op in operations:
        kind = op.get("op")
        if kind == "replace_music":
            if plan["music"]:
                for m in plan["music"]:
                    m["asset_id"] = op["asset_id"]
                    m["src_in_s"] = float(op.get("src_in_s", 0.0))
                    changed.append(m["id"])
            else:
                plan["music"].append({"id": "mus1", "asset_id": op["asset_id"], "track": "A3", "dst_in": 0,
                                      "dst_out": plan["sequence"]["duration_frames"], "src_in_s": 0.0, "gain_db": -18.0,
                                      "fade_in_s": 0.8, "fade_out_s": 1.5, "duck_under_vo": bool(plan["narration"]),
                                      "purpose": "underscore"})
                changed.append("mus1")
        elif kind == "remove_music":
            changed += [m["id"] for m in plan["music"]]
            plan["music"] = []
        elif kind == "set_cue_gain":
            for c in plan["music"] + plan["sfx"]:
                if c["id"] == op["cue_id"]:
                    c["gain_db"] = float(op["gain_db"]); changed.append(c["id"])
        elif kind == "scale_sfx":
            for c in plan["sfx"]:
                c["gain_db"] = round(c["gain_db"] + float(op["delta_db"]), 2); changed.append(c["id"])
        elif kind == "remove_cue":
            for lst in ("music", "sfx"):
                before = len(plan[lst])
                plan[lst] = [c for c in plan[lst] if c["id"] != op["cue_id"]]
                if len(plan[lst]) != before:
                    changed.append(op["cue_id"])
        elif kind == "add_sfx":
            cid = f"sfx{len(plan['sfx']) + 1}"
            hit = (ws.db.one("SELECT hit_s FROM audio_analysis WHERE asset_id=?", (op["asset_id"],)) or {}).get("hit_s") or 0.0
            at = op.get("at_frame") if op.get("at_frame") is not None else seq_rate.seconds_to_frames(float(op["at_seconds"]))
            plan["sfx"].append({"id": cid, "asset_id": op["asset_id"], "track": op.get("track", "A4"), "at_frame": int(at),
                                "hit_offset_s": hit, "gain_db": float(op.get("gain_db", -16)), "purpose": op.get("purpose", "")})
            changed.append(cid)
        elif kind == "replace_clip":
            from .assets import get_asset
            for c in plan["clips"]:
                if c["id"] == op["clip_id"]:
                    a = get_asset(ws, op.get("asset_id", c["asset_id"]))
                    sr = _src_rate(a)
                    length = c["src_out"] - c["src_in"]
                    length = round(Fraction(length) * sr.frac / Rate(**c["src_rate"]).frac)
                    c["asset_id"] = a["id"]
                    c["src_rate"] = sr.to_dict()
                    c["src_in"] = sr.seconds_to_frames(float(op["src_start"]))
                    c["src_out"] = c["src_in"] + int(length)
                    c["evidence"] = {**c["evidence"], "basis": op.get("basis", "user-selected shot"),
                                     "scene_id": op.get("scene_id")}
                    changed.append(c["id"])
        elif kind == "replace_shot_under_sentence":
            # find clips overlapping the words of a sentence in the narration, then replace the first one
            words = op["sentence"].lower().split()[:4]
            hits = [c for c in plan["captions"]["cues"] if " ".join(w["w"].lower().strip(".,!?") for w in c["words"]).startswith(" ".join(words)[:12])]
            if hits:
                f = hits[0]["start"]
                for c in plan["clips"]:
                    if c["dst_in"] <= f < c["dst_out"]:
                        return revise_plan(ws, project_id, [{"op": "replace_clip", "clip_id": c["id"], **{k: v for k, v in op.items() if k not in ("op", "sentence")}}] +
                                           [o for o in operations if o is not op], reason, origin)
        elif kind == "remove_bars":
            for c in plan["clips"]:
                if op.get("clip_id") in (None, "all", c["id"]) and c["transform"]["mode"] != "fill":
                    c["transform"]["mode"] = "fill"; changed.append(c["id"])
        elif kind == "caption_fix":
            # correct caption words without changing timing: {cue_id, words:[...]} (same count) or {find, replace}
            for c in plan["captions"]["cues"]:
                if op.get("cue_id") in (None, c["id"]):
                    if op.get("words") and op.get("cue_id") == c["id"]:
                        if len(op["words"]) != len(c["words"]):
                            raise ValueError(f"{c['id']} has {len(c['words'])} words; give the same number")
                        for w, new_w in zip(c["words"], op["words"]):
                            w["w"] = new_w
                        changed.append(c["id"])
                    elif op.get("find"):
                        hit = False
                        for w in c["words"]:
                            if w["w"].strip(".,!?").lower() == op["find"].lower():
                                w["w"] = w["w"].replace(w["w"].strip(".,!?"), op["replace"])
                                hit = True
                        if hit:
                            changed.append(c["id"])
            if any(x.startswith("cap") for x in changed):
                changed.append("captions.text")
        elif kind == "set_caption_style":
            plan["captions"]["style"].update({k: v for k, v in op.items() if k != "op"})
            changed.append("captions.style")
        elif kind == "set_zoom":
            for c in plan["clips"]:
                if op.get("clip_id") in (None, "all", c["id"]):
                    c["transform"]["zoom"] = {"from": float(op.get("from", 1.0)), "to": float(op.get("to", 1.0))}
                    changed.append(c["id"])
        else:
            raise ValueError(f"unknown operation {kind}")
    if not changed:
        raise ValueError("no plan element matched the requested operations")
    changed = sorted(set(changed))
    res = _save(ws, project_id, plan, reason, changed, origin)
    res["unchanged_clips"] = sum(1 for c in plan["clips"] if c["id"] not in changed)
    res["invalidates"] = affected_stages(changed)
    return res


def affected_stages(changed: list[str]) -> list[str]:
    """Which render stages a change invalidates. Picture and mix are cached separately."""
    out = set()
    for c in changed:
        if c.startswith("c") and c[1:].isdigit():
            out |= {"picture", "mix"}            # clip source audio is in the mix too
        elif c.startswith(("mus", "sfx")):
            out.add("mix")
        elif c.startswith(("captions", "cap")):
            out.add("picture")
    return sorted(out)


def picture_key(plan) -> str:
    return hash_obj({"seq": plan["sequence"], "clips": [{k: c[k] for k in ("asset_id", "src_rate", "src_in", "src_out",
                                                                             "dst_in", "dst_out", "transform")}
                                                        for c in plan["clips"]], "captions": plan["captions"]})


def mix_key(plan) -> str:
    return hash_obj({"seq": plan["sequence"]["duration_frames"], "fps": plan["sequence"]["fps"],
                     "narration": plan["narration"], "music": plan["music"], "sfx": plan["sfx"],
                     "clip_audio": [{k: c[k] for k in ("asset_id", "src_rate", "src_in", "src_out", "dst_in", "dst_out", "audio")}
                                    for c in plan["clips"]]})
