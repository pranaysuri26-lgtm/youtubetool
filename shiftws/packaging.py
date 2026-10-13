"""Upload package (no publishing), YouTube Studio CSV import, and comment triage input.

Uses the vendored yt-* skills' real tools (chapters.py, title.py, retention.py) and stores their outputs as
versioned project artifacts. Nothing is posted or published from here."""
from __future__ import annotations

import csv
import json
import re
import shutil
from pathlib import Path

from .jobs import JobFailed, handler
from .skills import run_skill_tool
from .timecode import Rate
from .transcribe import to_srt
from .util import new_id, now

STUDIO_URL = "https://studio.youtube.com/"


def captions_srt(plan) -> str:
    rate = Rate(**plan["sequence"]["fps"])
    segs = [{"start": rate.frames_to_seconds(c["start"]), "end": rate.frames_to_seconds(c["end"]),
             "text": " ".join(w["w"] for w in c["words"])} for c in plan["captions"]["cues"]]
    return to_srt(segs)


def chapters_from_plan(plan, srt_path: Path) -> dict:
    """Chapters from FINAL sequence timing (section markers), validated by yt-chapters' chapters.py rules."""
    rate = Rate(**plan["sequence"]["fps"])
    marks = [(rate.frames_to_seconds(m["frame"]), m["name"].title()) for m in plan.get("markers", [])]
    dur = rate.frames_to_seconds(plan["sequence"]["duration_frames"])
    lines = [f"{int(t // 60)}:{int(t % 60):02d} {name}" for t, name in marks]
    ok = bool(marks) and marks[0][0] == 0 and len(marks) >= 3 and all(b[0] - a[0] >= 10 for a, b in zip(marks, marks[1:])) \
        and dur - marks[-1][0] >= 10
    skill = None
    try:
        skill = run_skill_tool("yt-chapters", "chapters.py", [str(srt_path), "--json"])
    except Exception as e:  # noqa: BLE001
        skill = {"error": str(e)}
    return {"lines": lines, "valid_for_youtube": ok,
            "reason": None if ok else "YouTube needs ≥3 chapters, first at 0:00, each ≥10 s — not applicable to this cut",
            "skill_output": skill}


@handler("upload_package")
def job_package(ctx):
    from .assets import get_asset
    from .editplan import get_plan
    from .projects import get_project, set_stage, write_manifest
    from .scripts_eleven import get_script
    ws, p = ctx.ws, ctx.params
    pid = ctx.job["project_id"]
    proj = get_project(ws, pid)
    dlv = p.get("deliverable_id")
    if p.get("export_id"):
        e = ws.db.one("SELECT * FROM exports WHERE id=? AND project_id=?", (p["export_id"], pid))
    else:
        e = ws.db.one("SELECT * FROM exports WHERE project_id=? AND (? IS NULL OR deliverable_id=?) AND status IN "
                      "('qc_passed','qc_flagged') ORDER BY (kind!='preview') DESC, created_at DESC LIMIT 1",
                      (pid, dlv, dlv))
    if not e:
        raise JobFailed("no QC-checked export yet" + (f" for deliverable {dlv}" if dlv else ""),
                        hint="Render and run QC first.")
    if e["status"] not in ("qc_passed", "qc_flagged"):
        raise JobFailed(f"export {e['id']} has not passed QC ({e['status']})", hint="Run qc_run on it first.")
    plan = json.loads(ws.db.one("SELECT plan_json FROM edit_plan_versions WHERE id=?", (e["plan_version_id"],))["plan_json"])
    script = get_script(ws, plan.get("script_id")) if plan.get("script_id") else None
    if script and plan.get("deliverable_id") and script.get("deliverable_id") not in (None, plan["deliverable_id"]):
        script = None   # never package another deliverable's script
    dl_row = ws.db.one("SELECT * FROM deliverables WHERE id=?", (e["deliverable_id"],)) if e["deliverable_id"] else None
    sub = re.sub(r"[^a-z0-9]+", "-", (dl_row["title"] if dl_row else "main").lower()).strip("-")[:40] or "main"
    rev = len(list((Path(proj["root_dir"]) / "upload-package" / sub).glob("rev*"))) + 1
    d = Path(proj["root_dir"]) / "upload-package" / sub / f"rev{rev:02d}"
    d.mkdir(parents=True, exist_ok=True)
    video = d / Path(e["path"]).name
    try:
        video.hardlink_to(e["path"])
    except OSError:
        shutil.copy2(e["path"], video)
    srt = d / "captions.srt"
    srt.write_text(captions_srt(plan) if plan["captions"]["cues"] else "")
    chapters = chapters_from_plan(plan, srt) if plan["captions"]["cues"] else {"lines": [], "valid_for_youtube": False,
                                                                               "reason": "no transcript captions"}
    title = p.get("title") or (script["title"] if script and script["title"] else proj["title"])
    lint = None
    try:
        lint = run_skill_tool("yt-package", "title.py", ["--title", title, "--json"])
    except Exception as ex:  # noqa: BLE001
        lint = {"error": str(ex)}
    credits, unverified = [], []
    for cue in plan["music"] + plan["sfx"]:
        a = get_asset(ws, cue["asset_id"])
        lic = a["license"] or {}
        if not lic.get("verified"):
            unverified.append(a["title"])
        line = lic.get("attribution_text") or f"\"{a['title']}\" — {lic.get('license', 'license-unverified')}"
        if line not in credits:
            credits.append(line)
    for c in plan["clips"]:
        a = get_asset(ws, c["asset_id"])
        lic = a["license"] or {}
        if lic.get("license") and lic["license"] != "unverified":
            line = f"Footage: {a['source']['title'] if a['source'] else a['title']} — {lic['license']}"
            if line not in credits:
                credits.append(line)
    desc = p.get("description") or (script["clean_text"].split("\n")[0][:300] if script else "")
    description = desc + "\n\n" + ("\n".join(chapters["lines"]) + "\n\n" if chapters["valid_for_youtube"] else "") + \
        ("Credits:\n" + "\n".join(f"- {c}" for c in credits) if credits else "")
    (d / "description.txt").write_text(description.strip() + "\n")
    (d / "title.txt").write_text(title + "\n")
    tags = p.get("tags") or []
    (d / "tags.txt").write_text(", ".join(tags) + "\n")
    qc = json.loads(e["qc_json"])
    thumb = ["# Thumbnail brief (text only — no image API)", "", f"Title pairing: **{title}**", "",
             "- Show the moment the hook promises; one subject, high contrast, readable at small size.",
             "- Max 3–4 words of text; don't repeat the title verbatim.",
             "- Use a frame from the final cut (see frames below) — the thumbnail must be fulfilled by the video.", ""]
    frames = ws.db.all("SELECT frame_path, start FROM scenes WHERE described_by IS NOT NULL ORDER BY confidence DESC LIMIT 4")
    thumb += [f"- candidate frame @ {f['start']:.1f}s: {f['frame_path']}" for f in frames] or ["- (no inspected frames yet)"]
    (d / "thumbnail_brief.md").write_text("\n".join(thumb) + "\n")
    checklist = ["# Upload checklist", "", f"- [ ] Open YouTube Studio: {STUDIO_URL}",
                 f"- [ ] Upload `{video.name}` ({qc.get('status')}; {len(qc.get('flags', []))} QC flag(s) reviewed?)",
                 "- [ ] Paste title.txt / description.txt / tags.txt",
                 "- [ ] Upload captions.srt" + (" (kinetic captions are burned in; SRT is for accessibility)" if plan["captions"]["style"].get("style") == "kinetic_word" else ""),
                 "- [ ] Thumbnail per thumbnail_brief.md",
                 "- [ ] Check music/SFX credits are in the description" + (f" — license-unverified: {', '.join(unverified)}" if unverified else ""),
                 "- [ ] Audience / made-for-kids / visibility — your choice (not automated)",
                 "- [ ] Publish yourself. This workspace never publishes."]
    (d / "checklist.md").write_text("\n".join(checklist) + "\n")
    meta = {"package_rev": rev, "export_id": e["id"], "deliverable_id": e["deliverable_id"],
            "deliverable": dl_row["title"] if dl_row else None, "plan_version_id": e["plan_version_id"], "video": str(video),
            "title": title, "title_lint": lint, "chapters": chapters, "credits": credits,
            "license_unverified": unverified, "qc_status": qc.get("status"), "studio_url": STUDIO_URL, "created_at": now()}
    (d / "package.json").write_text(json.dumps(meta, indent=2, default=str))
    ws.db.insert("artifacts", {"id": new_id("art"), "project_id": pid, "kind": "upload_package", "cache_key": f"{e['id']}:{rev}",
                               "path": str(d), "depends_json": [f"export:{e['id']}"], "job_id": ctx.id, "created_at": now()})
    ws.remember(kind="fact", subject=f"Upload package rev {rev} ready", content=f"For export {e['id']} ({qc.get('status')}). "
                "Publishing stays a manual step in YouTube Studio.", project_id=pid, ref_type="export", ref_id=e["id"],
                data={"path": str(d)}, provenance="system")
    ws.add_task(pid, "Upload in YouTube Studio (manual)", kind="handoff", detail=str(d / "checklist.md"))
    ws.automation(pid, "upload_package", "automated")
    ws.automation(pid, "publish", "handoff", "publishing is manual by design")
    set_stage(ws, pid, "upload")
    write_manifest(ws, pid)
    return {**meta, "folder": str(d), "summary": f"upload package rev {rev} in {d.name} (title lint, credits, checklist)",
            "_outputs": [str(d)]}


# ---------------------------------------------------------------------- analytics

COLUMN_HINTS = {
    "position": [r"video position", r"position", r"time", r"seconds"],
    "retention": [r"audience retention", r"retention", r"% of viewers", r"absolute audience retention"],
    "relative_retention": [r"relative"],
    "views": [r"^views$", r"views"],
    "watch_time_hours": [r"watch time"],
    "avg_view_duration": [r"average view duration"],
    "impressions": [r"^impressions$"],
    "ctr": [r"click-through", r"ctr"],
    "stayed_to_watch": [r"stayed to watch", r"viewed vs\. swiped", r"viewed"],
    "date": [r"^date$", r"day"],
}


def map_columns(headers: list[str]) -> dict:
    mapping = {}
    for field, pats in COLUMN_HINTS.items():
        for h in headers:
            if any(re.search(p, h.strip().lower()) for p in pats) and h not in mapping.values():
                mapping[field] = h
                break
    return mapping


def import_analytics(ws, project_id: str, csv_path: str, export_id: str | None = None) -> dict:
    p = Path(csv_path).expanduser()
    if not p.exists():
        raise FileNotFoundError(p)
    with open(p, newline="", encoding="utf-8-sig", errors="replace") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        raise ValueError("empty CSV")
    headers = rows[0]
    mapping = map_columns(headers)
    kind = "retention" if "retention" in mapping and "position" in mapping else ("overview" if mapping else "unknown")
    data = []
    for r in rows[1:]:
        rec = {}
        for f, h in mapping.items():
            i = headers.index(h)
            v = r[i].strip() if i < len(r) else ""
            num = v.replace("%", "").replace(",", "")
            try:
                rec[f] = float(num)
            except ValueError:
                rec[f] = v if v else None
        data.append(rec)
    expected = ["views", "impressions", "ctr", "avg_view_duration", "retention", "stayed_to_watch"]
    missing = [f for f in expected if f not in mapping]
    findings = {}
    if kind == "retention":
        try:
            findings = run_skill_tool("yt-retention", "retention.py", [str(p), "--json"])
        except Exception as e:  # noqa: BLE001
            findings = {"error": str(e)}
    if not export_id:
        r = ws.db.one("SELECT id FROM exports WHERE project_id=? AND kind!='preview' ORDER BY created_at DESC LIMIT 1",
                      (project_id,)) or ws.db.one("SELECT id FROM exports WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                                                  (project_id,))
        export_id = r["id"] if r else None
    iid = new_id("perf")
    ws.db.insert("performance_imports", {"id": iid, "project_id": project_id, "export_id": export_id, "csv_path": str(p),
                                         "report_kind": kind, "mapping_json": mapping, "rows_json": data,
                                         "missing_json": missing, "findings_json": findings, "created_at": now()})
    ws.remember(kind="fact", subject=f"Studio CSV imported ({kind})",
                content=f"{len(data)} rows from {p.name}; mapped {sorted(mapping)}; missing (not zero): {missing}. "
                        "Findings are hypotheses tied to this export version.",
                project_id=project_id, ref_type="export", ref_id=export_id, data={"import_id": iid}, provenance="user import")
    ws.automation(project_id, "analytics_import", "automated")
    return {"import_id": iid, "kind": kind, "rows": len(data), "mapping": mapping, "missing_fields": missing,
            "export_id": export_id, "findings": findings,
            "note": "Missing fields are shown as missing, never as zero. Views alone are not retention/CTR evidence."}


def triage_comments(ws, project_id: str, comments: list[str]) -> dict:
    """Input for the yt-comment skill (no connected read tool is assumed). Saves the raw list as an artifact;
    the AI provider categorizes/drafts replies using the skill; nothing is posted."""
    from .projects import get_project
    d = Path(get_project(ws, project_id)["root_dir"]) / "research"
    f = d / f"comments_{now()[:10]}.json"
    f.write_text(json.dumps(comments, indent=1))
    return {"path": str(f), "count": len(comments), "skill": "yt-comment",
            "note": "Drafts only — replies and pins are posted by you in YouTube Studio."}
