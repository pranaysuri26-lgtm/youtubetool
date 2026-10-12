"""Missed-commitment reminders and cross-project lessons.

Reminders are evidence-backed: each cites the originating decision (id, thread, date) and the current discrepancy.
They never rewrite anything. Explicit newer instructions supersede older ones (superseded decisions are ignored).

Lessons: project-specific learnings are suggested elsewhere WITH their origin; only explicit channel-level
preferences apply everywhere. Performance-based lessons stay hypotheses with data references + confidence.
"""
from __future__ import annotations

import json
import re

from .util import new_id, now


def _norm(t: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (t or "").lower())


def _contains(hay: str, needle: str, frac: float = 0.7) -> bool:
    h, n = set(_norm(hay)), _norm(needle)
    if not n:
        return True
    return sum(1 for w in n if w in h) / len(n) >= frac


def check_commitments(ws, project_id: str, deliverable_id: str | None = None) -> list[dict]:
    from .scripts_eleven import get_script
    out = []
    decisions = ws.memories(project_id=project_id, include_channel=True, limit=500)
    script = get_script(ws, project_id=project_id, deliverable_id=deliverable_id)
    plan_row = ws.db.one("SELECT * FROM edit_plan_versions WHERE project_id=? ORDER BY version DESC LIMIT 1", (project_id,))
    plan = json.loads(plan_row["plan_json"]) if plan_row else None

    def cite(d):
        return {"decision_id": d["id"], "subject": d["subject"], "thread_id": d["thread_id"], "date": d["created_at"][:10]}

    for d in decisions:
        data = d["data"] or {}
        k = data.get("commitment")
        if not k:
            continue
        if k == "hook" and script:
            hook_txt = data.get("text", "")
            first = script["doc"]["sections"][0]["clean"] if script["doc"]["sections"] else ""
            if hook_txt and not _contains(first, hook_txt):
                out.append({"kind": "approved hook missing", **cite(d), "expected": hook_txt,
                            "current": f"script v{script['version']} opens: \"{first[:120]}\""})
        elif k == "payoff" and script:
            promise = data.get("text", "")
            if promise and not _contains(script["clean_text"], promise, 0.6):
                out.append({"kind": "promised payoff not delivered", **cite(d), "expected": promise,
                            "current": f"not found in script v{script['version']}"})
        elif k == "caption_rule" and plan:
            rule = data.get("rule", {})
            st = plan["captions"]["style"]
            diff = {key: (val, st.get(key)) for key, val in rule.items() if st.get(key) != val}
            if diff:
                out.append({"kind": "caption rule overlooked", **cite(d), "expected": rule,
                            "current": {k2: v[1] for k2, v in diff.items()}})
        elif k == "shot" and plan:
            aid, t = data.get("asset_id"), data.get("t")
            ok = False
            for c in plan["clips"]:
                if c["asset_id"] == aid:
                    from .timecode import Rate
                    sr = Rate(**c["src_rate"])
                    if sr.frames_to_seconds(c["src_in"]) - 0.5 <= (t or 0) <= sr.frames_to_seconds(c["src_out"]) + 0.5:
                        ok = True
            if not ok:
                out.append({"kind": "selected shot missing from plan", **cite(d), "expected": f"{aid} @ {t}s",
                            "current": f"plan v{plan_row['version']} doesn't include it"})
    if out:
        for r in out:
            key = f"{r['kind']}:{r['decision_id']}"
            if not ws.db.one("SELECT 1 FROM notifications WHERE ref_id=? AND kind='reminder'", (key,)):
                ws.db.insert("notifications", {"id": new_id("ntf"), "kind": "reminder", "title": r["kind"],
                                               "body": json.dumps(r, default=str), "ref_id": key, "project_id": project_id,
                                               "created_at": now()})
    return out


def commit(ws, project_id: str, kind: str, subject: str, content: str, *, thread_id=None, **data) -> dict:
    """Record an explicit commitment (approved hook, promised payoff, caption rule, selected shot)."""
    assert kind in ("hook", "payoff", "caption_rule", "shot")
    return ws.remember(kind="approval" if kind in ("hook", "shot") else "decision", subject=subject, content=content,
                       project_id=project_id, thread_id=thread_id, data={"commitment": kind, **data}, provenance="user")


def save_lesson(ws, *, subject: str, content: str, origin_project_id: str | None, scope: str = "suggestion",
                evidence: dict | None = None, confidence: float | None = None, thread_id=None) -> dict:
    """scope: suggestion (project-learned, offered elsewhere with origin) | channel_rule (explicit, applies everywhere)
    | hypothesis (performance-based; data refs + confidence)."""
    assert scope in ("suggestion", "channel_rule", "hypothesis")
    return ws.remember(kind="technique" if scope != "hypothesis" else "fact", subject=f"Lesson: {subject}", content=content,
                       project_id=origin_project_id if scope != "channel_rule" else None, thread_id=thread_id,
                       scope="channel" if scope == "channel_rule" else ("project" if origin_project_id else "channel"),
                       data={"lesson_scope": scope, "evidence": evidence or {}, "confidence": confidence,
                             "origin_project_id": origin_project_id}, provenance="user" if scope == "channel_rule" else "learned")


def relevant_lessons(ws, project_id: str, query: str, limit: int = 6) -> list[dict]:
    """Lessons from OTHER projects relevant to the query, each naming where it came from."""
    hits = ws.search(query, scope="all", kinds=["decision"], limit=40)
    titles = {p["id"]: p["title"] for p in ws.db.all("SELECT id, title FROM projects")}
    out = []
    for h in hits:
        d = ws.decision(h["ref_id"])
        if not d or d["status"] != "active" or d["project_id"] == project_id:
            continue
        if d["kind"] not in ("technique", "approval", "rejection", "decision", "fact"):
            continue
        ls = d["data"].get("lesson_scope")
        out.append({"decision_id": d["id"], "subject": d["subject"], "content": d["content"], "kind": d["kind"],
                    "origin_project": titles.get(d["project_id"], "channel"), "origin_thread": d["thread_id"],
                    "date": d["created_at"][:10],
                    "status": "confirmed instruction" if (ls == "channel_rule" or d["scope"] == "channel" and d["kind"] == "preference")
                    else ("hypothesis" if ls == "hypothesis" else "reminder from another project (not a rule here)"),
                    "confidence": d["data"].get("confidence")})
        if len(out) >= limit:
            break
    return out


def record_correction(ws, decision_id: str, correct: str, project_id: str | None = None, message_id: str | None = None) -> dict:
    """A remembered claim was wrong: supersede it and keep the correction so it can't resurface."""
    old = ws.decision(decision_id)
    new = ws.correct_memory(decision_id, content=correct, provenance="user correction")
    ws.db.insert("corrections", {"id": new_id("cor"), "project_id": project_id or old["project_id"], "wrong": old["content"],
                                 "correct": correct, "decision_id": new["id"], "superseded_decision_id": decision_id,
                                 "source_message_id": message_id, "created_at": now()})
    return {"corrected": new["id"], "superseded": decision_id}
