"""Channel profile, presets, preferences (with precedence + provenance), projects and project context."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from . import presets
from .util import new_id, now, slugify

STAGES = ["brief", "script", "narration", "footage", "audio", "edit_plan", "premiere", "review", "export",
          "upload", "results"]


def ensure_channel(ws) -> str:
    row = ws.db.one("SELECT id FROM channel_profile LIMIT 1")
    if row:
        return row["id"]
    cid = "ch_5to9shift"
    t = now()
    with ws.db.tx():
        ws.db.insert("channel_profile", {"id": cid, "name": presets.CHANNEL_NAME, "created_at": t, "updated_at": t})
        for k, v in presets.CHANNEL_DEFAULTS.items():
            _pref_row(ws, "channel", cid, k, v, "initial channel defaults (build spec section 6)")
        for p in presets.FORMAT_PRESETS:
            ws.db.insert("format_presets", {"id": p["id"], "channel_id": cid, "name": p["name"], "kind": p["kind"],
                                            "narrated": int(p["narrated"]), "settings_json": p["settings"]})
        n = presets.NARRATION_PRESET
        ws.db.insert("narration_presets", {"id": n["id"], "channel_id": cid, "name": n["name"], "model": n["model"],
                                           "voice": n["voice"], "confirmed": 0, "settings_json": n["settings"],
                                           "updated_at": t})
        ws.db.insert("threads", {"id": "th_channel", "project_id": None, "kind": "channel",
                                 "title": "Channel workspace", "created_at": t, "updated_at": t})
    voice = ws.cfg.home / "voice.md"
    if not voice.exists():
        voice.write_text(presets.VOICE_MD)
    tdir = ws.cfg.home / "templates"
    tdir.mkdir(exist_ok=True)
    for name, body in presets.TEMPLATES.items():
        if not (tdir / name).exists():
            (tdir / name).write_text(body)
    return cid


def _pref_row(ws, scope, scope_id, key, value, provenance):
    ws.db.x("UPDATE preferences SET active=0 WHERE scope=? AND scope_id=? AND key=? AND active=1",
            (scope, scope_id, key))
    ws.db.insert("preferences", {"id": new_id("pref"), "scope": scope, "scope_id": scope_id, "key": key,
                                 "value_json": json.dumps(value), "provenance": provenance, "active": 1,
                                 "created_at": now()})


def set_preference(ws, scope: str, scope_id: str, key: str, value, provenance: str, project_id=None) -> None:
    """Record a preference change. Channel-level changes are only made when explicitly requested."""
    _pref_row(ws, scope, scope_id, key, value, provenance)
    ws.remember(kind="preference", subject=f"{scope} preference: {key}", content=f"Set {key} = {json.dumps(value)}. "
                f"Why: {provenance}", project_id=project_id if scope != "channel" else None,
                scope="channel" if scope == "channel" else "project", data={"key": key, "value": value},
                provenance=provenance)


def resolve_preferences(ws, project_id: str | None = None) -> dict:
    """Effective preferences: explicit project > format preset > channel default. Each value carries its source."""
    out: dict = {}
    for r in ws.db.all("SELECT key, value_json, provenance FROM preferences WHERE scope='channel' AND active=1"):
        out[r["key"]] = {"value": json.loads(r["value_json"]), "source": "channel", "why": r["provenance"]}
    if project_id:
        p = get_project(ws, project_id)
        preset = ws.db.one("SELECT * FROM format_presets WHERE id=?", (p["format_preset"],))
        for k, v in json.loads(preset["settings_json"]).items():
            out[k] = {"value": v, "source": f"preset:{preset['id']}", "why": preset["name"]}
        for r in ws.db.all("SELECT key, value_json, provenance FROM preferences WHERE scope='preset' AND scope_id=? "
                           "AND active=1", (preset["id"],)):
            out[r["key"]] = {"value": json.loads(r["value_json"]), "source": f"preset:{preset['id']}",
                             "why": r["provenance"]}
        for r in ws.db.all("SELECT key, value_json, provenance FROM preferences WHERE scope='project' AND scope_id=? "
                           "AND active=1", (project_id,)):
            out[r["key"]] = {"value": json.loads(r["value_json"]), "source": "project", "why": r["provenance"]}
    return out


def pref(ws, project_id: str | None, key: str, default=None):
    return resolve_preferences(ws, project_id).get(key, {}).get("value", default)


def narration_preset(ws) -> dict:
    n = ws.db.one("SELECT * FROM narration_presets ORDER BY updated_at DESC LIMIT 1")
    n["settings"] = json.loads(n.pop("settings_json"))
    return n


def update_narration_preset(ws, *, model=None, voice=None, confirmed=None, feedback: dict | None = None,
                            provenance="user") -> dict:
    n = narration_preset(ws)
    s = n["settings"]
    if feedback:
        fb = s.setdefault("tag_feedback", {"works": [], "excessive": [], "spoken_aloud": [], "rejected_styles": []})
        for bucket, items in feedback.items():
            lst = fb.setdefault(bucket, [])
            for i in items:
                if i not in lst:
                    lst.append(i)
            # a tag that is spoken aloud or excessive no longer counts as working
            if bucket in ("spoken_aloud", "excessive"):
                fb["works"] = [w for w in fb.get("works", []) if w not in items]
    vals = {"settings_json": s, "updated_at": now()}
    if model is not None:
        vals["model"] = model
        s["supports_audio_tags"] = model.startswith("eleven_v3")
    if voice is not None:
        vals["voice"] = voice
    if confirmed is not None:
        vals["confirmed"] = int(confirmed)
    ws.db.update("narration_presets", {"id": n["id"]}, vals)
    ws.remember(kind="preference", subject="ElevenLabs narration preset",
                content=f"model={model or n['model']} voice={voice or n['voice']} feedback={json.dumps(feedback or {})}",
                scope="channel", data={"model": model, "voice": voice, "feedback": feedback}, provenance=provenance)
    return narration_preset(ws)


# ---------------------------------------------------------------------- projects

def create_project(ws, title: str, *, format_preset: str = "short_narrated", topic: str = "", game: str = "",
                   promise: str = "", audience: str | None = None, length_seconds: int | None = None,
                   aspect: str | None = None, spoiler_policy: str = "avoid major spoilers", root: str | None = None,
                   origin: str = "ui", thread_note: str | None = None) -> dict:
    preset = ws.db.one("SELECT * FROM format_presets WHERE id=?", (format_preset,))
    if not preset:
        raise ValueError(f"unknown format preset {format_preset}")
    settings = json.loads(preset["settings_json"])
    length_default = None
    if not length_seconds and settings.get("target_seconds"):
        length_seconds = settings["target_seconds"]      # starting target, overridable per project
        length_default = f"preset default {settings['target_seconds']}s (override any time)"
    pid = new_id("prj")
    slug = slugify(title)
    root_dir = Path(root).expanduser() if root else ws.cfg.projects_root / f"{slug}-{pid[-6:]}"
    for f in presets.PROJECT_FOLDERS:
        (root_dir / f).mkdir(parents=True, exist_ok=True)
    brief = {"topic": topic or title, "game": game, "promise": promise, "audience": audience,
             "length_seconds": length_seconds, "aspect": aspect or f"{settings['width']}x{settings['height']}",
             "spoiler_policy": spoiler_policy, "narrated": bool(preset["narrated"]),
             "length_source": length_default or ("user" if length_seconds else None)}
    t = now()
    with ws.db.tx():
        ws.db.insert("projects", {"id": pid, "channel_id": ws.channel_id, "title": title, "slug": slug,
                                  "format_preset": format_preset, "stage": "brief", "status": "active",
                                  "root_dir": str(root_dir), "brief_json": brief, "created_at": t, "updated_at": t})
        th_id = new_id("th")
        ws.db.insert("threads", {"id": th_id, "project_id": pid, "kind": "main", "title": title,
                                 "created_at": t, "updated_at": t})
        ws.db.insert("deliverables", {"id": new_id("dlv"), "project_id": pid, "title": title,
                                      "format_preset": format_preset, "length_seconds": length_seconds,
                                      "thread_id": th_id, "created_at": t})
    ws.index("project", pid, title, json.dumps(brief), project_id=pid, locator={"project_id": pid})
    ws.remember(kind="decision", subject="Project created",
                content=f"Format {preset['name']}; topic: {brief['topic']}; length: "
                        f"{length_seconds or 'not set'}s; spoilers: {spoiler_policy}.",
                project_id=pid, data=brief, provenance=origin)
    if preset["narrated"]:
        ws.add_task(pid, "Write and approve the script", kind="todo")
        ws.add_task(pid, "Generate narration in ElevenLabs and import it", kind="handoff",
                    detail="Copy the tagged script into ElevenLabs, download the audio, drop it in.")
    ws.add_task(pid, "Import or download footage", kind="todo")
    if not length_seconds:
        ws.add_task(pid, "Choose target duration", kind="question")
    write_manifest(ws, pid)
    ws.emit("project.created", {"id": pid, "title": title}, project_id=pid)
    return get_project(ws, pid)


def create_deliverable(ws, project_id: str, title: str, format_preset: str, length_seconds: int | None = None,
                       origin: str = "user") -> dict:
    """A child deliverable (e.g. a Short and a long-form cut) sharing the project's source analysis."""
    if not ws.db.one("SELECT 1 FROM format_presets WHERE id=?", (format_preset,)):
        raise ValueError(f"unknown format preset {format_preset}")
    if not length_seconds:
        st = json.loads(ws.db.one("SELECT settings_json FROM format_presets WHERE id=?", (format_preset,))["settings_json"])
        length_seconds = st.get("target_seconds")
    d = {"id": new_id("dlv"), "project_id": project_id, "title": title, "format_preset": format_preset,
         "length_seconds": length_seconds, "created_at": now()}
    ws.db.insert("deliverables", d)
    ws.remember(kind="decision", subject=f"Deliverable added: {title}",
                content=f"{format_preset}, {length_seconds or 'no'} s target; shares the project's footage analysis.",
                project_id=project_id, ref_type="deliverable", ref_id=d["id"], provenance=origin)
    return d


def list_deliverables(ws, project_id: str) -> list[dict]:
    return ws.db.all("SELECT * FROM deliverables WHERE project_id=? AND status='active' ORDER BY created_at", (project_id,))


def get_deliverable(ws, deliverable_id: str | None, project_id: str) -> dict:
    if deliverable_id:
        d = ws.db.one("SELECT * FROM deliverables WHERE id=?", (deliverable_id,))
        if not d:
            raise KeyError(deliverable_id)
        return d
    return ws.db.one("SELECT * FROM deliverables WHERE project_id=? ORDER BY created_at LIMIT 1", (project_id,))


def get_project(ws, pid: str) -> dict:
    p = ws.db.one("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise KeyError(f"project {pid} not found")
    p["brief"] = json.loads(p.pop("brief_json"))
    return p


def list_projects(ws) -> list[dict]:
    rows = ws.db.all("SELECT * FROM projects WHERE status!='deleted' ORDER BY updated_at DESC")
    for r in rows:
        r["brief"] = json.loads(r.pop("brief_json"))
    return rows


def find_project(ws, ref: str) -> dict | None:
    """Resolve an id, slug or fuzzy title."""
    r = ws.db.one("SELECT id FROM projects WHERE id=? OR slug=?", (ref, ref))
    if not r:
        r = ws.db.one("SELECT id FROM projects WHERE lower(title) LIKE ? ORDER BY updated_at DESC",
                      (f"%{ref.lower()}%",))
    return get_project(ws, r["id"]) if r else None


def update_brief(ws, pid: str, provenance: str = "user", **fields) -> dict:
    p = get_project(ws, pid)
    brief = {**p["brief"], **{k: v for k, v in fields.items() if v is not None}}
    ws.db.update("projects", {"id": pid}, {"brief_json": brief, "updated_at": now()})
    ws.remember(kind="decision", subject="Brief updated", content=json.dumps(fields), project_id=pid,
                data=fields, provenance=provenance)
    if fields.get("length_seconds"):
        ws.close_task(pid, "Choose target duration")
    write_manifest(ws, pid)
    return get_project(ws, pid)


def set_stage(ws, pid: str, stage: str) -> None:
    p = get_project(ws, pid)
    if STAGES.index(stage) > STAGES.index(p["stage"]):
        ws.db.update("projects", {"id": pid}, {"stage": stage, "updated_at": now()})
        ws.emit("project.stage", {"stage": stage}, project_id=pid)


def touch(ws, pid: str) -> None:
    ws.db.update("projects", {"id": pid}, {"updated_at": now()})


def project_context(ws, pid: str) -> dict:
    """Everything a new chat turn or agent needs, so the user never repeats context."""
    p = get_project(ws, pid)
    prefs = resolve_preferences(ws, pid)
    assets = ws.db.all("SELECT a.id, a.title, a.kind, a.duration, a.width, a.height, a.availability, pa.role, pa.label "
                       "FROM project_assets pa JOIN media_assets a ON a.id=pa.asset_id WHERE pa.project_id=? "
                       "ORDER BY pa.added_at", (pid,))
    script = ws.db.one("SELECT id, version, status, word_count, est_seconds, title FROM script_versions "
                       "WHERE project_id=? ORDER BY version DESC LIMIT 1", (pid,))
    take = ws.db.one("SELECT id, take_no, status, duration, is_synthetic_standin FROM narration_takes "
                     "WHERE project_id=? AND status!='replaced' ORDER BY created_at DESC LIMIT 1", (pid,))
    plan = ws.db.one("SELECT id, version, status, change_summary FROM edit_plan_versions WHERE project_id=? "
                     "ORDER BY version DESC LIMIT 1", (pid,))
    exp = ws.db.one("SELECT id, kind, path, status FROM exports WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                    (pid,))
    # failed jobs stop being blockers once a later job of the same kind succeeded
    jobs = ws.db.all("SELECT id, kind, status, progress, message, error FROM jobs j WHERE project_id=? AND "
                     "(status IN ('queued','running') OR (status='failed' AND NOT EXISTS (SELECT 1 FROM jobs k WHERE "
                     "k.project_id=j.project_id AND k.kind=j.kind AND k.status='done' AND k.created_at > j.created_at))) "
                     "ORDER BY created_at DESC LIMIT 10", (pid,))
    decisions = ws.memories(project_id=pid, include_channel=False, limit=25)
    channel_mem = ws.memories(project_id=None, kinds=["preference", "technique", "decision"], limit=15)
    analyzed = {r["asset_id"] for r in ws.db.all(
        "SELECT DISTINCT s.asset_id FROM scenes s JOIN project_assets pa ON pa.asset_id=s.asset_id "
        "WHERE pa.project_id=?", (pid,))}
    for a in assets:
        a["analyzed"] = a["id"] in analyzed
    ctx = {
        "deliverables": list_deliverables(ws, pid),
        "reports": ws.db.all("SELECT id, kind, title, revision, status, path FROM reports WHERE project_id=? AND "
                             "status!='superseded' ORDER BY created_at DESC", (pid,)),
        "project": {k: p[k] for k in ("id", "title", "stage", "format_preset", "root_dir")} | {"brief": p["brief"]},
        "preferences": {k: v["value"] for k, v in prefs.items()},
        "narration_preset": narration_preset(ws),
        "assets": assets, "latest_script": script, "latest_narration": take, "latest_plan": plan,
        "latest_export": exp, "active_jobs": jobs, "open_tasks": ws.open_tasks(pid),
        "decisions": [{k: d[k] for k in ("id", "kind", "subject", "content", "ref_type", "ref_id", "created_at")}
                      for d in decisions],
        "channel_memory": [{k: d[k] for k in ("id", "kind", "subject", "content")} for d in channel_mem],
    }
    ctx["where_we_stopped"] = where_we_stopped(ctx)
    return ctx


def where_we_stopped(ctx: dict) -> dict:
    """Short resume summary plus the next action, derived from structured state (not chat history)."""
    p = ctx["project"]
    bits = [f"{p['title']} — stage: {p['stage'].replace('_', ' ')}."]
    if ctx["latest_script"]:
        s = ctx["latest_script"]
        bits.append(f"Script v{s['version']} ({s['status']}, {s['word_count']} words, ~{s['est_seconds']:.0f}s).")
    if ctx["latest_narration"]:
        n = ctx["latest_narration"]
        bits.append(f"Narration take {n['take_no']} {n['status']}" +
                    (" (local TTS stand-in)" if n["is_synthetic_standin"] else "") + ".")
    vids = [a for a in ctx["assets"] if a["kind"] == "video"]
    if vids:
        bits.append(f"{len(vids)} footage file(s), {sum(1 for a in vids if a['analyzed'])} analyzed.")
    if ctx["latest_plan"]:
        bits.append(f"Edit plan v{ctx['latest_plan']['version']} ({ctx['latest_plan']['status']}).")
    if ctx["latest_export"]:
        bits.append(f"Latest export: {ctx['latest_export']['kind']} ({ctx['latest_export']['status']}).")
    failed = [j for j in ctx["active_jobs"] if j["status"] == "failed"]
    running = [j for j in ctx["active_jobs"] if j["status"] in ("queued", "running")]
    if running:
        bits.append(f"{len(running)} job(s) still running.")
    if ctx["decisions"]:
        d = ctx["decisions"][0]
        bits.append(f"Last decision: {d['subject']}.")
    next_action = _next_action(ctx, failed)
    return {"summary": " ".join(bits), "next_action": next_action}


def _next_action(ctx, failed) -> str:
    if failed:
        return f"Resolve failed job {failed[0]['kind']}: {failed[0]['error'] or failed[0]['message']}"
    narrated = ctx["project"]["brief"].get("narrated")
    s, n = ctx["latest_script"], ctx["latest_narration"]
    vids = [a for a in ctx["assets"] if a["kind"] == "video"]
    if narrated and not s:
        return "Write the script (ElevenLabs tagged + clean versions)."
    if narrated and s and s["status"] == "draft":
        return "Review and approve the script."
    if narrated and not n:
        return "Generate the narration in ElevenLabs from the tagged script, then drop the file here."
    if narrated and n and n["status"] != "locked":
        return "Review the narration alignment and lock the take."
    if not vids:
        return "Import or download footage."
    if any(not a["analyzed"] for a in vids):
        return "Analyze the remaining footage."
    if not ctx["latest_plan"]:
        return "Build the edit plan."
    if not ctx["latest_export"]:
        return "Render the review preview and run QC."
    if not any(t["title"].startswith("Upload in YouTube Studio") for t in ctx["open_tasks"]) and \
            not any(d["subject"].startswith("Upload package") for d in ctx["decisions"]):
        return "Review QC flags, then prepare the upload package."
    if any(t["title"].startswith("Upload in YouTube Studio") for t in ctx["open_tasks"]):
        return "Upload the package in YouTube Studio (manual), then import the Studio CSV when data arrives."
    return "Import Studio analytics and note lessons."


def write_manifest(ws, pid: str) -> Path:
    """Editable/exportable snapshot for agents. The DB stays authoritative."""
    ctx = project_context(ws, pid)
    root = Path(ctx["project"]["root_dir"])
    (root / "project.json").write_text(json.dumps(ctx, indent=2, default=str))
    md = [f"# {ctx['project']['title']}", "", ctx["where_we_stopped"]["summary"], "",
          f"**Next:** {ctx['where_we_stopped']['next_action']}", "", "## Brief"]
    md += [f"- {k}: {v}" for k, v in ctx["project"]["brief"].items()]
    md += ["", "## Decisions"] + [f"- [{d['kind']}] {d['subject']}: {d['content']}" for d in ctx["decisions"]]
    md += ["", "## Open tasks"] + [f"- {t['title']}" for t in ctx["open_tasks"]]
    (root / "context.md").write_text("\n".join(md) + "\n")
    return root / "context.md"


def backup(ws, dest: str | Path | None = None) -> Path:
    """Consistent copy of the database plus every project manifest."""
    dest = Path(dest) if dest else ws.cfg.home / "backups" / f"workspace-{now()[:19].replace(':', '')}"
    dest.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(dest / "workspace.db"))
    ws.db.conn.backup(target)
    target.close()
    for p in list_projects(ws):
        src = Path(p["root_dir"])
        d = dest / "projects" / p["id"]
        d.mkdir(parents=True, exist_ok=True)
        for name in ("project.json", "context.md"):
            if (src / name).exists():
                shutil.copy2(src / name, d / name)
        for sub in ("scripts", "edit-plans"):
            if (src / sub).exists():
                shutil.copytree(src / sub, d / sub, dirs_exist_ok=True)
    return dest


def restore(cfg, backup_dir: str | Path) -> None:
    """Restore the database from a backup directory (the app must be stopped)."""
    src = Path(backup_dir) / "workspace.db"
    if not src.exists():
        raise FileNotFoundError(src)
    if cfg.db_path.exists():
        shutil.copy2(cfg.db_path, cfg.db_path.with_suffix(".db.before-restore"))
    for ext in ("-wal", "-shm"):
        p = Path(str(cfg.db_path) + ext)
        if p.exists():
            p.unlink()
    shutil.copy2(src, cfg.db_path)
