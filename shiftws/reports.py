"""Versioned Markdown analysis reports tied to a project + originating thread + exact source hashes/settings.

- Modes: quick | standard | deep | focused (+ inventory across many recordings).
- Each report is stored as a file under the project (editable) AND as indexed sections in the DB, so later chat
  turns retrieve only relevant sections instead of re-reading the whole report.
- Coverage accounting: which frames / contact sheets / intervals were generated and which were actually reviewed.
- Shot-boundary discovery (FFmpeg scene score) finds cuts with no kept crv frame → candidate dense passes.
- External edits to the Markdown are detected by hash and reconciled into a new revision.
- A source whose bytes change marks the report stale; previous revisions are kept.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .jobs import JobFailed, enqueue, handler
from .util import new_id, now, run, sha256_file

MODES = {
    "quick": {"scene": 0.35, "max_frames": 60, "dedup_threshold": 10, "grid": False, "transcribe": True,
              "label": "Quick Scan"},
    "standard": {"scene": 0.30, "max_frames": None, "dedup_threshold": 8, "grid": True, "transcribe": True,
                 "label": "Standard Analysis"},
    # The user's requested starting configuration for fast-cut long sources (validated against crv 0.10.7 --help).
    # whisper "turbo" is not passed: crv reuses our local transcript sidecar, so its Whisper never runs.
    "deep": {"scene": 0.22, "max_frames": 450, "dedup_threshold": 6, "grid": True, "transcribe": True,
             "frame_width": 1280, "label": "Deep Dive", "requested_whisper_model": "turbo (superseded by workspace transcript)"},
    "focused": {"scene": 0.15, "max_frames": 200, "dedup_threshold": 3, "grid": True, "transcribe": True,
                "frame_width": 1600, "label": "Focused Mechanic Investigation"},
}

RUBRIC = ("CATEGORY", "CHARACTERS/ELEMENTS VISIBLE", "WHY NOTABLE", "VISUAL QUALITY 1-5", "STORY/SIGNIFICANCE 1-5",
          "ON-SCREEN TEXT")


def shot_boundaries(path: str, start=None, end=None, threshold: float = 0.3) -> list[float]:
    cmd = ["ffmpeg", "-nostdin", "-v", "info"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", path]
    if end:
        cmd += ["-t", str(end - (start or 0))]
    cmd += ["-vf", f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"]
    out = run(cmd, timeout=3600, check=False).stdout
    return [round(float(t) + (start or 0), 3) for t in re.findall(r"pts_time:([\d.]+)", out)]


def coverage(ws, asset_id: str, start=None, end=None) -> dict:
    a = ws.db.one("SELECT duration FROM media_assets WHERE id=?", (asset_id,))
    s0, s1 = start or 0.0, end or (a["duration"] or 0)
    frames = ws.db.all("SELECT start, described_by FROM scenes WHERE asset_id=? AND start>=? AND start<? ORDER BY start",
                       (asset_id, s0, s1))
    cov = ws.db.all("SELECT kind, ref, start, end, reviewed_by FROM coverage_records WHERE asset_id=?", (asset_id,))
    sheets = [c for c in cov if c["kind"] == "sheet"]
    dense = [c for c in cov if c["kind"] == "dense_pass"]
    reviewed_t = sorted(f["start"] for f in frames if f["described_by"])
    gaps, prev = [], s0
    for t in [f["start"] for f in frames] + [s1]:
        if t - prev > 6.0:
            gaps.append([round(prev, 2), round(t, 2)])
        prev = t
    return {"window": [s0, s1], "frames_generated": len(frames), "frames_reviewed": len(reviewed_t),
            "sheets_generated": len(sheets), "sheets_reviewed": sum(1 for c in sheets if c["reviewed_by"]),
            "dense_passes": len(dense), "intervals_without_frames_gt6s": gaps,
            "complete": bool(frames) and len(reviewed_t) == len(frames) and all(c["reviewed_by"] for c in sheets) and not gaps,
            "statement": "Sampled scene frames are not continuous observation. This pass is "
                         + ("fully reviewed for its sampled frames and sheets" if frames and len(reviewed_t) == len(frames)
                            else "NOT exhaustive")
                         + "; cuts between kept frames may be unseen unless covered by a dense pass."}


def mark_reviewed(ws, asset_id: str, kind: str, ref: str, reviewer: str, result: str = "") -> dict:
    r = ws.db.one("SELECT id FROM coverage_records WHERE asset_id=? AND kind=? AND ref=?", (asset_id, kind, ref))
    if r:
        ws.db.update("coverage_records", {"id": r["id"]}, {"reviewed_by": reviewer, "result": result, "reviewed_at": now()})
        return {"id": r["id"], "updated": True}
    cid = new_id("cov")
    ws.db.insert("coverage_records", {"id": cid, "asset_id": asset_id, "kind": kind, "ref": ref, "reviewed_by": reviewer,
                                      "result": result, "created_at": now(), "reviewed_at": now()})
    return {"id": cid, "updated": False}


def _register_generated(ws, asset_id, pid, crv_out: str):
    for f in sorted((Path(crv_out) / "grids").glob("grid_*.jpg")):
        if not ws.db.one("SELECT 1 FROM coverage_records WHERE asset_id=? AND kind='sheet' AND ref=?", (asset_id, str(f))):
            ws.db.insert("coverage_records", {"id": new_id("cov"), "asset_id": asset_id, "project_id": pid, "kind": "sheet",
                                              "ref": str(f), "created_at": now()})


def skipped_intervals(ws, asset, start=None, end=None) -> list[dict]:
    """Cuts detected by FFmpeg that have no kept crv frame within 0.5 s → candidates for a dense pass."""
    cuts = shot_boundaries(asset["current_path"], start, end)
    kept = [r["start"] for r in ws.db.all("SELECT start FROM scenes WHERE asset_id=?", (asset["id"],))]
    out = []
    for c in cuts:
        if not any(abs(k - c) <= 0.5 for k in kept):
            out.append({"cut_at": c, "window": [round(max(0, c - 1.0), 2), round(c + 1.0, 2)]})
    return out


@handler("dense_pass")
def job_dense(ctx):
    """Extract every Nth frame in a short window (actions, edit boundaries, HUD details) at source resolution."""
    from .analysis import analysis_dir
    from .assets import get_asset
    ws, p = ctx.ws, ctx.params
    a = get_asset(ws, p["asset_id"])
    s, e = float(p["start"]), float(p["end"])
    fps = float(p.get("fps", 6))
    d = analysis_dir(ws, a) / f"dense-{s:.2f}-{e:.2f}-{fps:g}"
    d.mkdir(parents=True, exist_ok=True)
    crop = p.get("crop")  # e.g. "iw/3:ih/4:0:0" to zoom on a HUD corner
    vf = f"fps={fps}" + (f",crop={crop}" if crop else "")
    run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{s:.3f}", "-i", a["current_path"], "-t", f"{e - s:.3f}",
         "-vf", vf, "-q:v", "2", str(d / "f_%04d.jpg")], timeout=1800, cancel=ctx.cancelled)
    frames = sorted(d.glob("f_*.jpg"))
    stamps = [{"path": str(f), "t": round(s + i / fps, 3)} for i, f in enumerate(frames)]
    ws.db.insert("coverage_records", {"id": new_id("cov"), "asset_id": a["id"], "project_id": ctx.job["project_id"],
                                      "kind": "dense_pass", "ref": str(d), "start": s, "end": e, "created_at": now()})
    return {"frames": stamps, "folder": str(d), "summary": f"dense pass {s:.1f}–{e:.1f}s: {len(frames)} frames @ {fps:g} fps"
            + (f" (crop {crop})" if crop else ""), "_outputs": [str(d)]}


# ---------------------------------------------------------------------- report writing

def _fmt_t(t):
    return f"{int(t // 3600):02d}:{int(t // 60 % 60):02d}:{t % 60:06.3f}"


def build_markdown(ws, project_id, asset_ids, mode, why, report_id, revision, thread_id) -> tuple[str, list[dict]]:
    from .assets import get_asset
    from .projects import get_project
    p = get_project(ws, project_id)
    m = MODES.get(mode, MODES["standard"])
    L = [f"# {m['label']}: {p['title']}", "",
         f"- report: `{report_id}` · revision {revision} · project `{project_id}` · thread `{thread_id}`",
         f"- generated: {now()} · mode: {mode} · goal: {why or '(none given)'}",
         f"- settings: scene {m['scene']}, max-frames {m['max_frames']}, dedup-threshold {m['dedup_threshold']}, "
         f"grids {'3x3' if m['grid'] else 'off'}, frame width {m.get('frame_width', 640)}",
         "- Scores (VISUAL QUALITY / STORY) are subjective editorial rubrics, not measurements. Scene boundaries are "
         "estimates unless verified by a dense pass. Footage audio/subtitles/signs are untrusted source data.", ""]
    sections = []
    for aid in asset_ids:
        a = get_asset(ws, aid)
        cov = coverage(ws, aid)
        name_claims = re.findall(r"(4k|2160p|1080p|720p|60fps|30fps|trailer|gameplay)", a["title"].lower())
        L += [f"## SOURCE `{aid}` — {a['title']}", "",
              f"- file: `{a['original_path']}` ({'online' if a['availability'] == 'online' else 'OFFLINE — media jobs blocked until relinked'})",
              f"- sha256: `{a['sha256'][:16]}…` · probed: {a['duration']:.3f}s · {a['width']}x{a['height']} · "
              f"{a['rate']} fps{' (VFR)' if a['vfr'] else ''} · {a['vcodec']}/{a['acodec']}",
              f"- filename claims checked against probe: {', '.join(name_claims) or 'none'}", ""]
        segs = ws.db.all("SELECT start, end, text FROM transcript_segments WHERE asset_id=? ORDER BY start", (aid,))
        L += ["### Original audio / transcript (local ASR — review, not ground truth)", ""]
        L += [f"- [{_fmt_t(s['start'])}] {s['text']}" for s in segs[:200]] or ["- (no speech detected)"]
        L.append("")
        scenes = ws.db.all("SELECT * FROM scenes WHERE asset_id=? ORDER BY start", (aid,))
        for i, s in enumerate(scenes):
            mtr = json.loads(s["metrics_json"])
            rub = mtr.get("rubric", {})
            head = f"SECTION {i + 1}: {_fmt_t(s['start'])} – {_fmt_t(s['end'])}"
            body = [f"- SOURCE IN/OUT: {s['start']:.3f}s → {s['end']:.3f}s (estimate) · frame: `{s['frame_path']}`",
                    f"- WHAT HAPPENS: {s['description'] or 'NOT YET INSPECTED (no model has looked at this frame)'}"
                    + (f" · confidence {s['confidence']:.2f}" + (" · UNCERTAIN" if s['uncertain'] else "") if s['description'] else "")]
            for k in RUBRIC:
                body.append(f"- {k}: {rub.get(k, '—')}")
            body.append(f"- metrics: luma {mtr.get('luma')}, pillarbox {mtr.get('pillarbox_frac')}, black overlap "
                        f"{mtr.get('black_overlap')}s{' · dark scene (not black)' if mtr.get('dark_but_not_black') else ''}")
            said = [x["text"] for x in segs if x["end"] > s["start"] and x["start"] < s["end"]]
            if said:
                body.append(f"- audio during scene: \"{' '.join(said)[:300]}\"")
            L += [f"### {head}", ""] + body + [""]
            sections.append({"heading": head, "body": "\n".join(body), "asset_id": aid, "start": s["start"], "end": s["end"]})
        inv = ws.db.all("SELECT * FROM investigations WHERE project_id=? ORDER BY created_at", (project_id,))
        L += ["### Coverage accounting", "",
              f"- frames generated {cov['frames_generated']}, reviewed {cov['frames_reviewed']}; contact sheets "
              f"generated {cov['sheets_generated']}, reviewed {cov['sheets_reviewed']}; dense passes {cov['dense_passes']}",
              f"- intervals > 6 s without a kept frame: {cov['intervals_without_frames_gt6s'] or 'none'}",
              f"- {cov['statement']}", ""]
        sections.append({"heading": f"Coverage {a['title']}", "body": json.dumps(cov), "asset_id": aid, "start": None, "end": None})
    if inv := ws.db.all("SELECT * FROM investigations WHERE project_id=? ORDER BY created_at", (project_id,)):
        L += ["## Investigations", ""]
        for x in inv:
            label = {"confirmed": "CONFIRMED (directly visible)", "suggestive": "SUGGESTIVE (not conclusive)",
                     "speculation": "SPECULATION", "no_evidence_in_inspected":
                     "NO SUPPORTING EVIDENCE FOUND IN THE INSPECTED FOOTAGE (not proof the game lacks it)"}[x["status"]]
            ev = json.loads(x["evidence_json"])
            body = [f"- verdict: {label}", f"- note: {x['note']}"] + [f"- evidence: `{e.get('asset_id')}` @ {e.get('t')}s "
                                                                       f"frame `{e.get('frame')}` — {e.get('note', '')}" for e in ev]
            body.append(f"- inspected at verdict time: {x['inspected_json']}")
            L += [f"### {x['question']}", ""] + body + [""]
            sections.append({"heading": f"Investigation: {x['question']}", "body": "\n".join(body), "asset_id": None,
                             "start": None, "end": None})
    total_frames = sum(coverage(ws, a)["frames_generated"] for a in asset_ids)
    total_rev = sum(coverage(ws, a)["frames_reviewed"] for a in asset_ids)
    L += ["## Summary totals", "", f"- sources: {len(asset_ids)} · scene sections: {total_frames} · reviewed: {total_rev}",
          "- This report is " + ("complete for its sampled frames" if total_frames and total_frames == total_rev
                                 else "a partial pass — not exhaustive"), ""]
    return "\n".join(L), sections


def write_report(ws, project_id, asset_ids, mode, why=None, thread_id=None, kind="analysis", parent=None,
                 settings_extra=None) -> dict:
    from .projects import get_project
    last = ws.db.one("SELECT * FROM reports WHERE project_id=? AND kind=? AND status!='superseded' ORDER BY revision DESC "
                     "LIMIT 1", (project_id, kind)) if parent is None else ws.db.one("SELECT * FROM reports WHERE id=?", (parent,))
    rev = (last["revision"] + 1) if last else 1
    rid = new_id("rep")
    md, sections = build_markdown(ws, project_id, asset_ids, mode, why, rid, rev, thread_id)
    d = Path(get_project(ws, project_id)["root_dir"]) / "analysis"
    path = d / f"{kind}_report_r{rev:02d}.md"
    path.write_text(md)
    hashes = {a: ws.db.one("SELECT sha256 FROM media_assets WHERE id=?", (a,))["sha256"] for a in asset_ids}
    if last:
        ws.db.update("reports", {"id": last["id"]}, {"status": "superseded"})
    ws.db.insert("reports", {"id": rid, "project_id": project_id, "thread_id": thread_id, "kind": kind,
                             "title": f"{MODES.get(mode, {}).get('label', kind)} r{rev}", "revision": rev,
                             "parent_id": last["id"] if last else None, "path": str(path),
                             "content_sha256": sha256_file(path), "source_hashes_json": hashes,
                             "settings_json": {"mode": mode, "why": why, **MODES.get(mode, {}), **(settings_extra or {})},
                             "status": "current", "created_at": now()})
    _index_sections(ws, rid, project_id, thread_id, sections)
    ws.remember(kind="fact", subject=f"Analysis report r{rev} ({mode})", content=f"{len(asset_ids)} source(s), "
                f"{len(sections)} sections → {path.name}", project_id=project_id, thread_id=thread_id,
                ref_type="report", ref_id=rid, data={"path": str(path)}, provenance="system")
    return {"report_id": rid, "revision": rev, "path": str(path), "sections": len(sections)}


def _index_sections(ws, rid, project_id, thread_id, sections):
    ws.db.x("DELETE FROM report_sections WHERE report_id=?", (rid,))
    for i, s in enumerate(sections):
        ws.db.insert("report_sections", {"report_id": rid, "idx": i, "heading": s["heading"], "body": s["body"],
                                         "asset_id": s.get("asset_id"), "start": s.get("start"), "end": s.get("end")})
        ws.index("report", f"{rid}:{i}", s["heading"], s["body"], project_id=project_id, thread_id=thread_id,
                 locator={"report_id": rid, "section": i, "asset_id": s.get("asset_id"), "t": s.get("start")})


def refresh_if_inspected(ws, project_id: str) -> list[dict]:
    """New frame inspections since a report was written → write the next revision from the DB (no media work)."""
    out = []
    for r in ws.db.all("SELECT * FROM reports WHERE project_id=? AND status='current' AND kind='analysis'", (project_id,)):
        ids = list(json.loads(r["source_hashes_json"]))
        newer = ws.db.one("SELECT COUNT(*) n FROM coverage_records WHERE asset_id IN (%s) AND reviewed_at > ?"
                          % ",".join("?" * len(ids)), ids + [r["created_at"]])["n"] if ids else 0
        if newer:
            st = json.loads(r["settings_json"])
            out.append(write_report(ws, project_id, ids, st.get("mode", "standard"), st.get("why"), r["thread_id"],
                                    kind="analysis", parent=r["id"]))
    return out


def report_sections(ws, project_id: str, query: str | None = None, report_id: str | None = None, limit: int = 8) -> list[dict]:
    """Retrieve only the relevant sections of the current report(s) for a chat turn."""
    reconcile_external_edits(ws, project_id)
    refresh_if_inspected(ws, project_id)
    if query:
        hits = ws.search(query, scope="project", project_id=project_id, kinds=["report"], limit=limit)
        out = []
        for h in hits:
            rid, idx = h["ref_id"].split(":")
            r = ws.db.one("SELECT rs.*, r.status, r.revision, r.path FROM report_sections rs JOIN reports r ON r.id=rs.report_id "
                          "WHERE rs.report_id=? AND rs.idx=?", (rid, int(idx)))
            if r and r["status"] != "superseded":
                out.append(r)
        return out
    rid = report_id or (ws.db.one("SELECT id FROM reports WHERE project_id=? AND status!='superseded' ORDER BY created_at "
                                  "DESC LIMIT 1", (project_id,)) or {}).get("id")
    return ws.db.all("SELECT * FROM report_sections WHERE report_id=? ORDER BY idx LIMIT ?", (rid, limit)) if rid else []


def reconcile_external_edits(ws, project_id: str) -> list[dict]:
    """If a report file was edited outside the app, index the edited text as a new revision (provenance kept)."""
    changed = []
    for r in ws.db.all("SELECT * FROM reports WHERE project_id=? AND status!='superseded'", (project_id,)):
        p = Path(r["path"])
        if not p.exists():
            continue
        h = sha256_file(p)
        if h == r["content_sha256"]:
            continue
        text = p.read_text()
        parts = re.split(r"^### ", text, flags=re.M)
        sections = [{"heading": x.splitlines()[0].strip(), "body": "\n".join(x.splitlines()[1:]).strip()} for x in parts[1:]]
        rid = new_id("rep")
        ws.db.update("reports", {"id": r["id"]}, {"status": "superseded"})
        ws.db.insert("reports", {**{k: r[k] for k in ("project_id", "thread_id", "kind", "path", "source_hashes_json",
                                                      "settings_json")},
                                 "id": rid, "title": r["title"].split(" r")[0] + f" r{r['revision'] + 1} (edited)",
                                 "revision": r["revision"] + 1, "parent_id": r["id"], "content_sha256": h,
                                 "status": r["status"], "created_at": now()})
        _index_sections(ws, rid, project_id, r["thread_id"], sections)
        ws.remember(kind="fact", subject="Report edited outside the app",
                    content=f"{p.name} changed on disk; indexed as revision {r['revision'] + 1}. Previous revision kept.",
                    project_id=project_id, ref_type="report", ref_id=rid, provenance="external edit")
        changed.append({"report_id": rid, "path": str(p)})
    return changed


def check_stale(ws, project_id: str) -> list[dict]:
    """Mark reports stale when any source file's bytes changed (or note offline sources)."""
    from .assets import check_modified
    out = []
    for r in ws.db.all("SELECT * FROM reports WHERE project_id=? AND status='current'", (project_id,)):
        for aid, h in json.loads(r["source_hashes_json"]).items():
            a = ws.db.one("SELECT * FROM media_assets WHERE id=?", (aid,))
            if a and Path(a["current_path"]).exists() and check_modified(ws, aid):
                ws.db.update("reports", {"id": r["id"]}, {"status": "stale", "stale_reason": f"source {aid} changed"})
                out.append({"report_id": r["id"], "asset_id": aid})
    return out


@handler("analysis_report")
def job_report(ctx):
    """Register sources (already imported), analyze each with the chosen mode, write the versioned report."""
    from .analysis import transcribe_asset, run_crv, build_scenes, _asset
    ws, p = ctx.ws, ctx.params
    pid = ctx.job["project_id"]
    mode = p.get("mode", "standard")
    m = MODES[mode]
    ids = p["asset_ids"]
    done = set(ctx.checkpoint.get("done", []))
    notes = {}
    for i, aid in enumerate(ids):
        if aid in done:
            continue
        a = _asset(ws, aid)
        base = i / len(ids)
        ctx.progress(base, f"analyzing {a['title']} ({i + 1}/{len(ids)}) — {m['label']}", stage="analyze")
        t = transcribe_asset(ws, aid, None, p.get("start"), p.get("end"), pid) if m["transcribe"] else {}
        c = run_crv(ws, a, t.get("srt"), ctx, start=p.get("start"), end=p.get("end"), max_frames=m["max_frames"],
                    scene=m["scene"], frame_width=m.get("frame_width", 640), dedup_threshold=m["dedup_threshold"],
                    grid=m["grid"], why=p.get("why"))
        cov = build_scenes(ws, a, c["out"], p.get("start"), p.get("end"), pid)
        _register_generated(ws, aid, pid, c["out"])
        if mode in ("deep", "focused"):
            notes[aid] = {"skipped_intervals": skipped_intervals(ws, a, p.get("start"), p.get("end"))[:40]}
        notes.setdefault(aid, {})["coverage"] = cov
        done.add(aid)
        ctx.save_checkpoint(done=sorted(done), notes=notes)
    ctx.progress(0.95, "writing the Markdown report", stage="report")
    rep = write_report(ws, pid, ids, mode, p.get("why"), ctx.job["thread_id"], kind=p.get("kind", "analysis"),
                       settings_extra={"skipped": {k: len(v.get("skipped_intervals", [])) for k, v in notes.items()}})
    if ctx.job["thread_id"]:
        from .chat.threads import add_message
        add_message(ws, ctx.job["thread_id"], "system", f"Analysis report ready: **{Path(rep['path']).name}** "
                    f"(revision {rep['revision']}, {rep['sections']} sections)", meta={"report_id": rep["report_id"],
                                                                                      "artifact": rep["path"]})
    ws.automation(pid, "analysis_report", "automated", mode)
    ws.add_task(pid, "Inspect key frames and fill WHAT HAPPENS / rubric", kind="todo")
    return {**rep, "mode": mode, "notes": notes,
            "summary": f"{m['label']} report r{rep['revision']}: {len(ids)} source(s), {rep['sections']} sections",
            "_outputs": [rep["path"]]}


def record_investigation(ws, project_id: str, question: str, status: str, evidence: list[dict], note: str = "") -> dict:
    allowed = ("confirmed", "suggestive", "speculation", "no_evidence_in_inspected")
    if status not in allowed:
        raise ValueError(f"status must be one of {allowed}")
    if status in ("confirmed", "suggestive") and not any(e.get("frame") for e in evidence):
        raise ValueError("a confirmed/suggestive verdict needs saved frame references as evidence")
    assets = {e.get("asset_id") for e in evidence if e.get("asset_id")} or {
        r["asset_id"] for r in ws.db.all("SELECT asset_id FROM project_assets WHERE project_id=?", (project_id,))}
    inspected = {a: {k: v for k, v in coverage(ws, a).items() if k in ("frames_generated", "frames_reviewed",
                                                                       "sheets_reviewed", "dense_passes")}
                 for a in assets if a}
    iid = new_id("inv")
    ws.db.insert("investigations", {"id": iid, "project_id": project_id, "question": question, "status": status,
                                    "evidence_json": evidence, "inspected_json": inspected, "note": note, "created_at": now()})
    phr = "No supporting evidence found in the inspected footage" if status == "no_evidence_in_inspected" else status
    ws.remember(kind="fact", subject=f"Investigation: {question}", content=f"{phr}. {note}", project_id=project_id,
                ref_type="investigation", ref_id=iid, data={"status": status, "evidence": evidence}, provenance="analysis")
    return {"investigation_id": iid, "status": status, "inspected": inspected}


@handler("inventory")
def job_inventory(ctx):
    """Batch inventory across many recordings: one master index with stable ids, duplicates/overlaps,
    shot entries and ranked Shorts candidates (rationale kept, linked to evidence)."""
    from .analysis import transcribe_asset, run_crv, build_scenes, _asset
    from .projects import get_project
    ws, p = ctx.ws, ctx.params
    pid = ctx.job["project_id"]
    ids = p["asset_ids"]
    m = MODES[p.get("mode", "quick")]
    for i, aid in enumerate(ids):
        if aid in ctx.checkpoint.get("done", []):
            continue
        a = _asset(ws, aid)
        ctx.progress(i / len(ids), f"inventory {i + 1}/{len(ids)}: {a['title']}", stage="scan")
        t = transcribe_asset(ws, aid, None, None, None, pid)
        c = run_crv(ws, a, t.get("srt"), ctx, max_frames=m["max_frames"], scene=m["scene"],
                    dedup_threshold=m["dedup_threshold"], grid=m["grid"])
        build_scenes(ws, a, c["out"], project_id=pid)
        _register_generated(ws, aid, pid, c["out"])
        ctx.save_checkpoint(done=ctx.checkpoint.get("done", []) + [aid])
    # overlaps by perceptual hash of kept frames across files
    from PIL import Image
    def dhash(path):
        im = Image.open(path).convert("L").resize((9, 8))
        px = list(im.tobytes())
        return sum(1 << i for i in range(64) if px[(i // 8) * 9 + i % 8] > px[(i // 8) * 9 + i % 8 + 1])
    hashes = {}
    for aid in ids:
        for s in ws.db.all("SELECT id, start, frame_path FROM scenes WHERE asset_id=?", (aid,)):
            if s["frame_path"] and Path(s["frame_path"]).exists():
                hashes[(aid, s["start"])] = dhash(s["frame_path"])
    overlaps = []
    keys = list(hashes)
    for i, k1 in enumerate(keys):
        for k2 in keys[i + 1:]:
            if k1[0] != k2[0] and bin(hashes[k1] ^ hashes[k2]).count("1") <= 4:
                overlaps.append({"a": k1[0], "a_t": k1[1], "b": k2[0], "b_t": k2[1]})
    sha_dups = ws.db.all("SELECT sha256, GROUP_CONCAT(id) ids FROM media_assets WHERE id IN (%s) GROUP BY sha256 HAVING COUNT(*)>1"
                         % ",".join("?" * len(ids)), ids)
    cands = []
    for aid in ids:
        for s in ws.db.all("SELECT * FROM scenes WHERE asset_id=? ORDER BY start", (aid,)):
            mt = json.loads(s["metrics_json"])
            rub = mt.get("rubric", {})
            sig = float(str(rub.get("STORY/SIGNIFICANCE 1-5", "0")).split()[0] or 0) if rub else 0
            vis = float(str(rub.get("VISUAL QUALITY 1-5", "0")).split()[0] or 0) if rub else 0
            score = sig * 2 + vis + (1 if s["described_by"] else 0) - (2 if mt.get("pillarbox_frac", 0) > 0.05 else 0)
            cands.append({"asset_id": aid, "start": s["start"], "end": s["end"], "score": score, "frame": s["frame_path"],
                          "rationale": (f"story {sig}, visual {vis}" if rub else "unrated — needs inspection") +
                                       ("; title/pillarboxed frame" if mt.get("pillarbox_frac", 0) > 0.05 else "")})
    cands.sort(key=lambda c: -c["score"])
    root = Path(get_project(ws, pid)["root_dir"]) / "analysis"
    rev = len(list(root.glob("inventory_index_r*.md"))) + 1
    L = [f"# Recording inventory r{rev}", "", f"- recordings: {len(ids)} · generated {now()}", "", "## Files", ""]
    for aid in ids:
        a = ws.db.one("SELECT * FROM media_assets WHERE id=?", (aid,))
        n = ws.db.one("SELECT COUNT(*) n FROM scenes WHERE asset_id=?", (aid,))["n"]
        L.append(f"- `{aid}` {a['title']} — {a['duration']:.1f}s {a['width']}x{a['height']} · {n} shot entries · "
                 f"sha `{a['sha256'][:12]}`")
    L += ["", "## Duplicates / overlaps", ""] + ([f"- identical files: {d['ids']}" for d in sha_dups] or ["- no identical files"])
    L += [f"- visually overlapping: `{o['a']}`@{o['a_t']:.1f}s ≈ `{o['b']}`@{o['b_t']:.1f}s" for o in overlaps[:40]] or []
    L += ["", "## Shorts candidates (ranked; rationale kept — scores are editorial, unrated items need inspection)", ""]
    L += [f"{i + 1}. `{c['asset_id']}` {c['start']:.1f}–{c['end']:.1f}s · score {c['score']:.1f} · {c['rationale']} · "
          f"frame `{c['frame']}`" for i, c in enumerate(cands[:20])]
    path = root / f"inventory_index_r{rev:02d}.md"
    path.write_text("\n".join(L) + "\n")
    rid = new_id("rep")
    ws.db.insert("reports", {"id": rid, "project_id": pid, "thread_id": ctx.job["thread_id"], "kind": "inventory",
                             "title": f"Recording inventory r{rev}", "revision": rev, "path": str(path),
                             "content_sha256": sha256_file(path),
                             "source_hashes_json": {a: ws.db.one("SELECT sha256 FROM media_assets WHERE id=?", (a,))["sha256"] for a in ids},
                             "settings_json": {"mode": p.get("mode", "quick")}, "status": "current", "created_at": now()})
    secs = [{"heading": f"Shorts candidate {i + 1}", "body": json.dumps(c), "asset_id": c["asset_id"], "start": c["start"],
             "end": c["end"]} for i, c in enumerate(cands[:50])]
    _index_sections(ws, rid, pid, ctx.job["thread_id"], secs)
    ws.automation(pid, "inventory", "automated", f"{len(ids)} recordings")
    return {"report_id": rid, "path": str(path), "recordings": len(ids), "overlaps": len(overlaps),
            "duplicates": len(sha_dups), "candidates": len(cands),
            "summary": f"inventory of {len(ids)} recordings: {len(overlaps)} visual overlaps, {len(cands)} shot entries",
            "_outputs": [str(path)]}
