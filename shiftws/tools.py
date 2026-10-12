"""Workspace tools — one implementation used by the MCP server (Claude Code), the Ollama provider, offline chat
commands, the HTTP API and the CLI. Every tool returns typed JSON:

    {"status": "ok"|"queued"|"error"|"needs_input", "data": ..., "warnings": [...], "artifacts": [...], "job_ids": [...]}

Long work is enqueued as jobs (same queue as UI buttons). Destructive or public actions are not tools.
"""
from __future__ import annotations

import json
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from . import jobs as J
from . import projects as P

# Current chat scope (set per MCP process from env, or per call by the API).
CURRENT: ContextVar[dict] = ContextVar("CURRENT", default={})


def _scope() -> dict:
    s = dict(CURRENT.get())
    s.setdefault("project_id", os.environ.get("SHIFTWS_PROJECT_ID") or None)
    s.setdefault("thread_id", os.environ.get("SHIFTWS_THREAD_ID") or None)
    s.setdefault("origin", os.environ.get("SHIFTWS_ORIGIN", "mcp"))
    return s


def _pid(project_id: str | None) -> str:
    pid = project_id or _scope()["project_id"]
    if not pid:
        raise ValueError("project_id is required (no current project in this thread)")
    return pid


def ok(data=None, *, warnings=None, artifacts=None, job_ids=None, status="ok") -> dict:
    return {"status": status, "data": data, "warnings": warnings or [], "artifacts": artifacts or [],
            "job_ids": job_ids or []}


def _job(ws, kind, params, project_id, cache_key=None, force=False) -> dict:
    s = _scope()
    j = J.enqueue(ws, kind, params, project_id=project_id, thread_id=s["thread_id"], origin=s["origin"],
                  cache_key=cache_key, force=force)
    return ok({"job_id": j["id"], "kind": kind, "status": j["status"], "reused": j.get("reused", False),
               "result": j["result"] if j["status"] == "done" else None},
              job_ids=[j["id"]], status="ok" if j["status"] == "done" else "queued")


# ============================================================== projects & memory

def project_create(ws, title: str, format_preset: str = "short_narrated", topic: str = "", game: str = "",
                   promise: str = "", length_seconds: int | None = None, spoiler_policy: str = "avoid major spoilers",
                   audience: str | None = None) -> dict:
    """Create a video project (folders, main chat thread, tasks). format_preset: short_narrated |
    short_no_commentary | long_narrated | long_no_commentary | cinematic."""
    p = P.create_project(ws, title, format_preset=format_preset, topic=topic, game=game, promise=promise,
                         length_seconds=length_seconds, spoiler_policy=spoiler_policy, audience=audience,
                         origin=_scope()["origin"])
    th = ws.db.one("SELECT id FROM threads WHERE project_id=? AND kind='main'", (p["id"],))
    return ok({"project_id": p["id"], "title": p["title"], "root_dir": p["root_dir"], "main_thread_id": th["id"],
               "open_tasks": [t["title"] for t in ws.open_tasks(p["id"])]})


def project_list(ws) -> dict:
    return ok([{"id": p["id"], "title": p["title"], "stage": p["stage"], "format": p["format_preset"],
                "updated_at": p["updated_at"]} for p in P.list_projects(ws)])


def project_context(ws, project_id: str | None = None) -> dict:
    """Full structured state for a project: brief, effective preferences, assets, script, narration, plan,
    exports, open tasks, decisions and 'where we stopped'."""
    return ok(P.project_context(ws, _pid(project_id)))


def project_update_brief(ws, project_id: str | None = None, topic: str | None = None, game: str | None = None,
                         promise: str | None = None, length_seconds: int | None = None, audience: str | None = None,
                         spoiler_policy: str | None = None) -> dict:
    p = P.update_brief(ws, _pid(project_id), provenance=_scope()["origin"], topic=topic, game=game, promise=promise,
                       length_seconds=length_seconds, audience=audience, spoiler_policy=spoiler_policy)
    return ok(p["brief"])


def project_set_preferences(ws, preferences: dict, reason: str, project_id: str | None = None,
                            make_channel_default: bool = False) -> dict:
    """Set preferences. Project-scoped by default; channel defaults only when the user explicitly says so."""
    scope = "channel" if make_channel_default else "project"
    pid = None if make_channel_default else _pid(project_id)
    for k, v in preferences.items():
        P.set_preference(ws, scope, ws.channel_id if make_channel_default else pid, k, v, reason, project_id=pid)
    return ok(P.resolve_preferences(ws, pid) if pid else P.resolve_preferences(ws))


def memory_save(ws, kind: str, subject: str, content: str, project_id: str | None = None, ref_type: str | None = None,
                ref_id: str | None = None, data: dict | None = None, channel_level: bool = False) -> dict:
    """Save a structured fact/decision (what changed and why). kind: decision | approval | rejection | fact |
    preference | technique | note. Project-scoped unless channel_level=True (only when the user asks)."""
    s = _scope()
    d = ws.remember(kind=kind, subject=subject, content=content,
                    project_id=None if channel_level else (project_id or s["project_id"]), thread_id=s["thread_id"],
                    ref_type=ref_type, ref_id=ref_id, data=data, provenance=s["origin"],
                    scope="channel" if channel_level or not (project_id or s["project_id"]) else "project")
    return ok(d)


def memory_list(ws, project_id: str | None = None, kinds: list[str] | None = None) -> dict:
    return ok(ws.memories(project_id=project_id or _scope()["project_id"], kinds=kinds))


def memory_correct(ws, memory_id: str, content: str | None = None, subject: str | None = None) -> dict:
    return ok(ws.correct_memory(memory_id, content=content, subject=subject))


def memory_forget(ws, memory_id: str) -> dict:
    ws.forget(memory_id)
    return ok({"deleted": memory_id})


def search(ws, query: str, scope: str = "all", kinds: list[str] | None = None, limit: int = 15) -> dict:
    """Search messages, scripts, transcripts, scenes, audio, assets and decisions.
    scope: thread | project | all. Results carry a locator (thread/message, asset + timestamp, script...)."""
    s = _scope()
    return ok(ws.search(query, scope=scope, project_id=s["project_id"], thread_id=s["thread_id"], kinds=kinds,
                        limit=limit))


def task_add(ws, title: str, project_id: str | None = None, kind: str = "todo", detail: str = "") -> dict:
    return ok(ws.add_task(_pid(project_id), title, kind=kind, detail=detail))


def task_done(ws, title_prefix: str, project_id: str | None = None) -> dict:
    return ok({"closed": ws.close_task(_pid(project_id), title_prefix)})


def sources_register(ws, title: str, url: str = "", category: str = "reported", notes: str = "",
                     project_id: str | None = None) -> dict:
    """Save a research source. category: measured | reported | inference | opinion; public vs private is explicit."""
    from .util import new_id, now
    pid = _pid(project_id)
    sid = new_id("src")
    ws.db.insert("source_references", {"id": sid, "project_id": pid, "url": url, "title": title, "kind": "research",
                                       "observed": category, "notes": notes, "acquired_at": now()})
    ws.index("source", sid, title, f"{notes} {url} {category}", project_id=pid, locator={"source_id": sid, "url": url})
    return ok({"source_id": sid})


# ============================================================== media

def assets_import(ws, paths: list[str], project_id: str | None = None, role: str | None = None,
                  copy: bool = False) -> dict:
    """Import local files or folders (by reference unless copy=True). Dedupes by content hash."""
    return _job(ws, "import_media", {"paths": paths, "role": role, "copy": copy}, _pid(project_id))


def assets_download(ws, url: str, project_id: str | None = None, start: float | None = None,
                    end: float | None = None) -> dict:
    """Download a supported URL with yt-dlp into this project's folder (optional seconds range)."""
    return _job(ws, "download", {"url": url, "start": start, "end": end}, _pid(project_id))


def assets_list(ws, project_id: str | None = None, role: str | None = None) -> dict:
    from .assets import project_assets
    out = []
    for a in project_assets(ws, _pid(project_id), role=role):
        out.append({k: a[k] for k in ("id", "title", "kind", "role", "duration", "rate", "width", "height",
                                      "availability", "current_path")} |
                   {"license": a["license"]["license"] if a["license"] else None})
    return ok(out)


def video_analyze(ws, asset_id: str, project_id: str | None = None, start: float | None = None,
                  end: float | None = None, max_frames: int | None = None) -> dict:
    """Analyze footage locally: transcript (once) + claude-real-video scene frames + metrics. Cached by hash."""
    return _job(ws, "analyze_video", {"asset_id": asset_id, "start": start, "end": end, "max_frames": max_frames},
                _pid(project_id))


def scene_frames(ws, asset_id: str, start: float | None = None, end: float | None = None, limit: int = 12,
                 only_uninspected: bool = False) -> dict:
    """List analyzed scene frames (image paths you can Read) with timestamps and metrics."""
    sql = "SELECT id, start, end, frame_path, description, confidence, metrics_json FROM scenes WHERE asset_id=?"
    params: list = [asset_id]
    if start is not None:
        sql += " AND end>?"; params.append(start)
    if end is not None:
        sql += " AND start<?"; params.append(end)
    if only_uninspected:
        sql += " AND described_by IS NULL"
    rows = ws.db.all(sql + " ORDER BY start LIMIT ?", params + [limit])
    for r in rows:
        r["metrics"] = json.loads(r.pop("metrics_json"))
    return ok(rows, warnings=["Sampled scene frames are not continuous coverage; inspect dense frames around key moments."])


def scene_describe(ws, scene_id: str, description: str, confidence: float, tags: list[str] | None = None,
                   rubric: dict | None = None) -> dict:
    """Record what you SAW in a frame after reading the image. Confidence 0-1; below 0.6 stays marked uncertain.
    rubric (deep dive, optional): {"CATEGORY", "CHARACTERS/ELEMENTS VISIBLE", "WHY NOTABLE", "VISUAL QUALITY 1-5",
    "STORY/SIGNIFICANCE 1-5", "ON-SCREEN TEXT"} — subjective editorial scores. Marks the frame as reviewed."""
    from .analysis import describe_scene
    s = _scope()
    return ok(describe_scene(ws, scene_id, description, tags=tags, confidence=confidence,
                             described_by=f"{s['origin']}-vision", project_id=s["project_id"], rubric=rubric))


def analysis_report(ws, mode: str = "standard", asset_ids: list[str] | None = None, paths: list[str] | None = None,
                    why: str | None = None, start: float | None = None, end: float | None = None,
                    project_id: str | None = None) -> dict:
    """Analyze gameplay and write a versioned Markdown report linked to this project/chat. mode: quick | standard |
    deep (max-frames 450, scene 0.22, dedup 6, 3x3 grids) | focused (window, dense). `paths` (files or folders,
    spaces fine) are registered first. The report persists; later turns use report_sections, not re-upload."""
    from .acquire import job_import
    pid = _pid(project_id)
    ids = list(asset_ids or [])
    if paths:
        from .assets import import_file
        from . import probe
        for item in paths:
            q = Path(item).expanduser()
            files = [f for f in sorted(q.rglob("*")) if f.is_file() and probe.kind_for(f) == "video"] if q.is_dir() else [q]
            for f in files:
                ids.append(import_file(ws, f, project_id=pid, role="footage")["id"])
    if not ids:
        from .assets import project_assets
        ids = [a["id"] for a in project_assets(ws, pid, kind="video")]
    kind = "inventory" if mode == "inventory" else "analysis_report"
    return _job(ws, kind, {"asset_ids": ids, "mode": "quick" if mode == "inventory" else mode, "why": why,
                           "start": start, "end": end}, pid)


def report_sections(ws, query: str | None = None, report_id: str | None = None, project_id: str | None = None,
                    limit: int = 8) -> dict:
    """Retrieve the relevant sections of this project's saved analysis report(s) (detects external edits first)."""
    from .reports import report_sections as rs
    return ok(rs(ws, _pid(project_id), query, report_id, limit))


def dense_pass(ws, asset_id: str, start: float, end: float, fps: float = 6, crop: str | None = None,
               project_id: str | None = None) -> dict:
    """Extract dense frames (default 6 fps) in a short window at source resolution; optional crop (FFmpeg crop
    syntax, e.g. 'iw/3:ih/4:0:0') to zoom on HUD/small text. Recorded in coverage."""
    return _job(ws, "dense_pass", {"asset_id": asset_id, "start": start, "end": end, "fps": fps, "crop": crop},
                _pid(project_id))


def coverage_status(ws, asset_id: str, start: float | None = None, end: float | None = None) -> dict:
    """What has actually been reviewed: frames, contact sheets, dense passes, gaps. Never call a partial pass exhaustive."""
    from .reports import coverage
    return ok(coverage(ws, asset_id, start, end))


def coverage_mark(ws, asset_id: str, kind: str, ref: str, result: str = "") -> dict:
    """Mark a contact sheet / dense pass / interval as reviewed by you (kind: sheet | dense_pass | interval | frame)."""
    from .reports import mark_reviewed
    return ok(mark_reviewed(ws, asset_id, kind, ref, f"{_scope()['origin']}-vision", result))


def skipped_cuts(ws, asset_id: str, start: float | None = None, end: float | None = None) -> dict:
    """Shot boundaries (FFmpeg scene detection) with no kept frame nearby — candidates for dense_pass."""
    from .assets import get_asset
    from .reports import skipped_intervals
    return ok(skipped_intervals(ws, get_asset(ws, asset_id), start, end))


def investigation_record(ws, question: str, status: str, evidence: list[dict] | None = None, note: str = "",
                         project_id: str | None = None) -> dict:
    """Record a mechanic/claim verdict. status: confirmed | suggestive | speculation | no_evidence_in_inspected.
    confirmed/suggestive need evidence [{asset_id, t, frame, note}] with saved frame paths. Absence in inspected
    footage is NOT proof the game lacks something."""
    from .reports import record_investigation
    return ok(record_investigation(ws, _pid(project_id), question, status, evidence or [], note))


def report_refresh(ws, project_id: str | None = None, mode: str = "standard", why: str | None = None) -> dict:
    """Write a new report revision from current analysis + descriptions (previous revisions are kept)."""
    from .reports import write_report
    from .assets import project_assets
    pid = _pid(project_id)
    ids = [a["id"] for a in project_assets(ws, pid, kind="video")]
    return ok(write_report(ws, pid, ids, mode, why, _scope()["thread_id"]))


def deliverable_create(ws, title: str, format_preset: str, length_seconds: int | None = None,
                       project_id: str | None = None) -> dict:
    """Add a child deliverable (e.g. a Short and a long-form cut) sharing this project's source analysis."""
    from .projects import create_deliverable
    return ok(create_deliverable(ws, _pid(project_id), title, format_preset, length_seconds, _scope()["origin"]))


def deliverable_list(ws, project_id: str | None = None) -> dict:
    from .projects import list_deliverables
    return ok(list_deliverables(ws, _pid(project_id)))


def commitment_record(ws, kind: str, subject: str, content: str, text: str | None = None, rule: dict | None = None,
                      asset_id: str | None = None, t: float | None = None, project_id: str | None = None) -> dict:
    """Record an explicit commitment the user approved: kind hook {text} | payoff {text} | caption_rule {rule} |
    shot {asset_id, t}. Later work is checked against it (reminders, never automatic rewrites)."""
    from .commitments import commit
    extra = {k: v for k, v in {"text": text, "rule": rule, "asset_id": asset_id, "t": t}.items() if v is not None}
    return ok(commit(ws, _pid(project_id), kind, subject, content, thread_id=_scope()["thread_id"], **extra))


def commitments_check(ws, project_id: str | None = None, deliverable_id: str | None = None) -> dict:
    """Evidence-backed reminders: approved hook omitted, promised payoff missing, caption rule overlooked,
    selected shot missing — each citing the originating decision."""
    from .commitments import check_commitments
    return ok(check_commitments(ws, _pid(project_id), deliverable_id))


def lessons_find(ws, query: str, project_id: str | None = None) -> dict:
    """Relevant experiences from OTHER videos, each naming its origin project/thread and whether it is a confirmed
    instruction, a reminder, or a hypothesis."""
    from .commitments import relevant_lessons
    return ok(relevant_lessons(ws, project_id or _scope()["project_id"], query))


def lesson_save(ws, subject: str, content: str, scope: str = "suggestion", confidence: float | None = None,
                evidence: dict | None = None, project_id: str | None = None) -> dict:
    """Save a lesson. scope: suggestion (project-learned) | channel_rule (only when the user says it's general) |
    hypothesis (from performance data; include evidence + confidence)."""
    from .commitments import save_lesson
    return ok(save_lesson(ws, subject=subject, content=content, origin_project_id=project_id or _scope()["project_id"],
                          scope=scope, evidence=evidence, confidence=confidence, thread_id=_scope()["thread_id"]))


def correction_record(ws, memory_id: str, correct: str) -> dict:
    """The user corrected a remembered claim: supersede it so the old error never comes back."""
    from .commitments import record_correction
    return ok(record_correction(ws, memory_id, correct, _scope()["project_id"]))


def catalog_add_folder(ws, path: str, library: str | None = None, role: str | None = None) -> dict:
    """Add a channel-wide music/SFX folder (monitored for additions/removals; drives may disconnect)."""
    from .catalog import add_folder
    return ok(add_folder(ws, path, library, role))


def notifications_list(ws, unread_only: bool = True) -> dict:
    rows = ws.db.all("SELECT * FROM notifications" + (" WHERE read=0" if unread_only else "") +
                     " ORDER BY created_at DESC LIMIT 50")
    return ok(rows)


def comments_triage_input(ws, comments: list[str], project_id: str | None = None) -> dict:
    """Save pasted/imported comments for yt-comment triage (drafts only; nothing is posted)."""
    from .packaging import triage_comments
    return ok(triage_comments(ws, _pid(project_id), comments))


def premiere_inspect(ws, sequence_id: str, project_id: str | None = None) -> dict:
    """Repair mode, read-only: actual tracks, mute state, offline media, narration placement in a sequence."""
    return _job(ws, "premiere_inspect", {"sequence_id": sequence_id}, _pid(project_id), force=True)


def footage_find(ws, query: str, project_id: str | None = None, all_projects: bool = False, limit: int = 10) -> dict:
    """Find footage moments by transcript, scene descriptions and tags. Returns asset, time range, evidence,
    confidence and the basis (vision description vs transcript vs uninspected)."""
    from .analysis import find_footage
    return ok(find_footage(ws, query, None if all_projects else (project_id or _scope()["project_id"]), limit))


# ============================================================== script & narration

def script_save(ws, sections: list[dict], title: str = "", hooks: list[dict] | None = None,
                pronunciations: list[dict] | None = None, editorial_notes: str = "", change_note: str = "",
                approve: bool = False, project_id: str | None = None, deliverable_id: str | None = None) -> dict:
    """Save a script version. Each section: {label: hook|setup|progression|payoff|ending, tagged: spoken text with
    inline ElevenLabs audio tags like [curious], notes, beats:[{visual, evidence}], claims:[...]}.
    The clean caption/alignment version and all five export files are produced automatically."""
    from .scripts_eleven import save_script
    s = _scope()
    doc = {"title": title, "sections": sections, "hooks": hooks or [], "pronunciations": pronunciations or [],
           "editorial_notes": editorial_notes}
    r = save_script(ws, _pid(project_id), doc, change_note=change_note, origin=s["origin"], thread_id=s["thread_id"],
                    approve=approve, deliverable_id=deliverable_id)
    return ok(r, warnings=r["warnings"], artifacts=list(r["files"].values()))


def script_get(ws, project_id: str | None = None, script_id: str | None = None) -> dict:
    from .scripts_eleven import get_script
    return ok(get_script(ws, script_id, None if script_id else _pid(project_id)))


def script_approve(ws, script_id: str) -> dict:
    from .scripts_eleven import approve
    return ok(approve(ws, script_id, origin=_scope()["origin"]))


def script_score_hooks(ws, hooks: list[str]) -> dict:
    """Score hook candidates with yt-script's hookscore.py (editorial heuristic, not a view prediction)."""
    from .scripts_eleven import score_hooks
    return ok(score_hooks(ws, hooks))


def narration_tag_feedback(ws, works: list[str] | None = None, excessive: list[str] | None = None,
                           spoken_aloud: list[str] | None = None, rejected_styles: list[str] | None = None) -> dict:
    """Remember which ElevenLabs tags worked with the voice, which were excessive or read aloud."""
    from .scripts_eleven import record_tag_feedback
    return ok(record_tag_feedback(ws, works=works or [], excessive=excessive or [], spoken_aloud=spoken_aloud or [],
                                  rejected_styles=rejected_styles or []))


def narration_import(ws, path: str, project_id: str | None = None, script_id: str | None = None,
                     generation_settings: dict | None = None, synthetic_standin: bool = False,
                     deliverable_id: str | None = None) -> dict:
    """Import a narration take the user generated in ElevenLabs. Alignment runs automatically."""
    from .narration import import_take
    s = _scope()
    t = import_take(ws, _pid(project_id), path, script_version_id=script_id, generation_settings=generation_settings,
                    synthetic_standin=synthetic_standin, thread_id=s["thread_id"], origin=s["origin"],
                    deliverable_id=deliverable_id)
    return ok({k: t[k] for k in ("id", "take_no", "status", "duration") if k in t} | {"align_job": t.get("align_job")},
              job_ids=[t["align_job"]] if t.get("align_job") else [])


def narration_status(ws, project_id: str | None = None) -> dict:
    from .narration import get_take
    rows = ws.db.all("SELECT id FROM narration_takes WHERE project_id=? ORDER BY take_no", (_pid(project_id),))
    return ok([get_take(ws, r["id"]) for r in rows])


def narration_lock(ws, take_id: str) -> dict:
    """Lock an approved take. Original bytes are preserved and verified; edits work around it."""
    from .narration import lock_take
    return ok(lock_take(ws, take_id, origin=_scope()["origin"], thread_id=_scope()["thread_id"]))


# ============================================================== audio catalog

def audio_index_library(ws, folder: str, library: str | None = None) -> dict:
    """Index a music/SFX folder into the shared catalog (originals stay in place)."""
    return _job(ws, "index_audio_library", {"folder": folder, "library": library or Path(folder).name}, None,
                cache_key={"folder": str(Path(folder).expanduser().resolve())})


def audio_find(ws, request: str, role: str | None = None, status: str | None = None, limit: int = 6) -> dict:
    """Find music/SFX: 'short bass impact with audible midrange', 'restrained transition', status='approved'
    finds previously approved sounds. Labels show heuristic confidence; license state is included."""
    from .audio_catalog import find_audio
    return ok(find_audio(ws, request, project_id=_scope()["project_id"], role=role, status=status, limit=limit))


def audio_decide(ws, asset_id: str, status: str, purpose: str = "", note: str = "", project_id: str | None = None) -> dict:
    """Record approved / rejected / favorite for an audio asset, with the purpose. Saved to memory."""
    from .audio_catalog import record_usage
    s = _scope()
    return ok(record_usage(ws, asset_id, status, project_id=project_id or s["project_id"], purpose=purpose, note=note,
                           thread_id=s["thread_id"]))


def audio_preview(ws, asset_id: str, at_seconds: float = 0.0, gain_db: float = -12.0, project_id: str | None = None,
                  align_hit: bool = True, duration: float = 12.0) -> dict:
    """Render a short preview of a music/SFX asset mixed against the locked narration (not in isolation)."""
    return _job(ws, "audio_preview", {"asset_id": asset_id, "at": at_seconds, "gain_db": gain_db, "align_hit": align_hit,
                                      "duration": duration}, _pid(project_id))


# ============================================================== edit, premiere, review, export

def edit_plan_create(ws, project_id: str | None = None, music_asset_id: str | None = None,
                     sfx_asset_ids: list[str] | None = None, notes: str = "", deliverable_id: str | None = None) -> dict:
    """Build a canonical edit plan from the locked narration, script beats, analyzed footage and approved audio."""
    from .editplan import create_plan
    return ok(create_plan(ws, _pid(project_id), music_asset_id=music_asset_id, sfx_asset_ids=sfx_asset_ids,
                          notes=notes, origin=_scope()["origin"], deliverable_id=deliverable_id))


def edit_plan_revise(ws, operations: list[dict], reason: str, project_id: str | None = None,
                     deliverable_id: str | None = None) -> dict:
    """Targeted revision by stable IDs. Ops: replace_music{asset_id}, set_cue_gain{cue_id,gain_db},
    scale_sfx{delta_db}, replace_clip{clip_id,asset_id,src_start}, remove_bars{clip_id|all}, set_caption_style{...},
    remove_cue{cue_id}, add_sfx{asset_id,at_frame,gain_db}. Only affected artifacts are invalidated."""
    from .editplan import revise_plan
    return ok(revise_plan(ws, _pid(project_id), operations, reason, origin=_scope()["origin"],
                          deliverable_id=deliverable_id))


def edit_plan_get(ws, project_id: str | None = None, version: int | None = None) -> dict:
    from .editplan import get_plan
    return ok(get_plan(ws, _pid(project_id), version))


def edit_plan_validate(ws, project_id: str | None = None) -> dict:
    from .editplan import get_plan, validate
    p = get_plan(ws, _pid(project_id))
    return ok(validate(ws, p["plan"]))


def preview_render(ws, project_id: str | None = None, deliverable_id: str | None = None) -> dict:
    """Render the review preview from the current edit plan (FFmpeg, with burned kinetic captions overlay)."""
    from .editplan import get_plan
    pid = _pid(project_id)
    p = get_plan(ws, pid, deliverable_id=deliverable_id)
    return _job(ws, "render_preview", {"plan_id": p["id"]}, pid)


def qc_run(ws, export_id: str | None = None, project_id: str | None = None) -> dict:
    """Independent QC of a rendered file: loudness, true peak, silence, black frames, bars, duration, captions,
    narration-unchanged check. Flags are candidates for review, not proof of mistakes."""
    pid = _pid(project_id)
    if not export_id:
        r = ws.db.one("SELECT id FROM exports WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (pid,))
        if not r:
            return ok(None, status="error", warnings=["nothing rendered yet — run preview_render first"])
        export_id = r["id"]
    return _job(ws, "qc", {"export_id": export_id}, pid)


def export_run(ws, project_id: str | None = None, kind: str = "youtube") -> dict:
    """Render the final deliverable from the current plan (FFmpeg path) and inspect the actual file."""
    from .editplan import get_plan
    pid = _pid(project_id)
    p = get_plan(ws, pid)
    return _job(ws, "render_preview", {"plan_id": p["id"], "kind": kind}, pid)


def upload_package_create(ws, project_id: str | None = None, title: str | None = None,
                          description: str | None = None, tags: list[str] | None = None) -> dict:
    """Assemble the upload package: video, captions, chapters (from final timing), title/description/tags,
    credits block from license records, thumbnail brief, checklist. Does not publish."""
    return _job(ws, "upload_package", {"title": title, "description": description, "tags": tags}, _pid(project_id),
                force=True)


def analytics_import(ws, csv_path: str, project_id: str | None = None, export_id: str | None = None) -> dict:
    """Import a YouTube Studio CSV export (retention or overview). Missing fields stay missing."""
    from .packaging import import_analytics
    return ok(import_analytics(ws, _pid(project_id), csv_path, export_id=export_id))


def premiere_status(ws) -> dict:
    """Read-only Premiere bridge check: installed bridge, connection, version, active project/sequence."""
    from .premiere.adapter import status
    return ok(status(ws))


def premiere_apply_plan(ws, project_id: str | None = None, dry_run: bool = True) -> dict:
    """Apply the current edit plan to a NEW sequence in Premiere (saves first, one writer, verified readback).
    dry_run=True returns the normalized operations without touching Premiere."""
    from .editplan import get_plan
    pid = _pid(project_id)
    p = get_plan(ws, pid)
    if dry_run:
        from .premiere.adapter import plan_operations
        return ok(plan_operations(ws, p))
    return _job(ws, "premiere_apply", {"plan_id": p["id"]}, pid, force=True)


# ============================================================== jobs

def job_status(ws, job_id: str) -> dict:
    j = J.get(ws, job_id)
    return ok({k: j[k] for k in ("id", "kind", "status", "progress", "message", "error", "error_hint", "result")})


def job_wait(ws, job_id: str, seconds: int = 120) -> dict:
    """Wait for a job to finish (max 300 s per call), then return its result."""
    j = J.wait(ws, job_id, timeout=min(300, max(1, seconds)))
    return ok({k: j[k] for k in ("id", "kind", "status", "progress", "message", "error", "error_hint", "result")})


def job_cancel(ws, job_id: str) -> dict:
    return ok(J.cancel(ws, job_id)["status"])


def job_resume(ws, job_id: str) -> dict:
    """Resume a failed/cancelled job from its checkpoint."""
    return ok(J.resume(ws, job_id)["status"], job_ids=[job_id])


def jobs_list(ws, project_id: str | None = None, status: str | None = None) -> dict:
    return ok([{k: j[k] for k in ("id", "kind", "status", "progress", "message", "error")}
               for j in J.list_jobs(ws, project_id or _scope()["project_id"], status, 20)])


def automation_report(ws, project_id: str | None = None) -> dict:
    """Measured share of steps that ran without the user vs handoffs (goal metric, not a claim)."""
    return ok(ws.automation_report(project_id or _scope()["project_id"]))


TOOLS = [project_create, project_list, project_context, project_update_brief, project_set_preferences,
         memory_save, memory_list, memory_correct, memory_forget, search, task_add, task_done, sources_register,
         assets_import, assets_download, assets_list, video_analyze, scene_frames, scene_describe, footage_find,
         script_save, script_get, script_approve, script_score_hooks, narration_tag_feedback, narration_import,
         narration_status, narration_lock, audio_index_library, audio_find, audio_decide, audio_preview,
         analysis_report, report_sections, report_refresh, dense_pass, coverage_status, coverage_mark, skipped_cuts,
         investigation_record, deliverable_create, deliverable_list, commitment_record, commitments_check,
         lessons_find, lesson_save, correction_record, catalog_add_folder, notifications_list, comments_triage_input,
         premiere_inspect, edit_plan_create, edit_plan_revise, edit_plan_get, edit_plan_validate, preview_render, qc_run, export_run,
         upload_package_create, analytics_import, premiere_status, premiere_apply_plan, job_status, job_wait,
         job_cancel, job_resume, jobs_list, automation_report]

BY_NAME = {f.__name__: f for f in TOOLS}


def call(ws, name: str, args: dict[str, Any], scope: dict | None = None) -> dict:
    token = CURRENT.set({**_scope(), **(scope or {})}) if scope else None
    try:
        return BY_NAME[name](ws, **args)
    except Exception as e:  # noqa: BLE001
        return ok(None, status="error", warnings=[f"{type(e).__name__}: {e}", getattr(e, "hint", "")])
    finally:
        if token is not None:
            CURRENT.reset(token)
