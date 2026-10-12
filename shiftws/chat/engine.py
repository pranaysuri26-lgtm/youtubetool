"""Chat turns: persist the user message, route attachments deterministically, run the provider with live events,
persist the reply and tool activity. Turns run in background threads and can be cancelled."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ..util import new_id, now
from .providers import OfflineProvider, TurnEvents, get_provider
from .threads import add_message, get_thread

_TURNS: dict[str, threading.Event] = {}


def time_window(text: str) -> tuple[float | None, float | None]:
    """'first 60 seconds' / 'first 2 minutes' / 'from 1:20 to 2:05' / 'between 30s and 90s' → (start, end)."""
    import re as _re
    t = (text or "").lower()

    def secs(v: str) -> float:
        if ":" in v:
            parts = [float(x) for x in v.split(":")]
            return sum(x * 60 ** i for i, x in enumerate(reversed(parts)))
        return float(v)
    m = _re.search(r"first\s+(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds|m|min|mins|minutes)\b", t)
    if m:
        v = float(m.group(1)) * (60 if m.group(2).startswith("m") else 1)
        return 0.0, v
    m = _re.search(r"(?:from|between)\s+(\d+(?::\d+){0,2}|\d+(?:\.\d+)?)\s*s?\s+(?:to|and|-)\s+(\d+(?::\d+){0,2}|\d+(?:\.\d+)?)\s*s?\b", t)
    if m:
        return secs(m.group(1)), secs(m.group(2))
    return None, None


def handle_attachments(ws, thread: dict, paths: list[str], text: str = "") -> list[str]:
    """Dropped files: narration audio for a narrated project waiting on VO → narration import; everything else →
    media import. Returns short notes that are added to the turn so the AI knows what happened."""
    from .. import tools as T
    from ..probe import kind_for
    notes = []
    pid = thread["project_id"]
    if not pid:
        return ["Files dropped in the channel thread were not imported (open a project thread)."]
    scope = {"project_id": pid, "thread_id": thread["id"], "origin": "chat"}
    waiting_vo = ws.db.one("SELECT 1 FROM tasks WHERE project_id=? AND status='open' AND title LIKE 'Generate narration%'",
                           (pid,))
    media = []
    for p in paths:
        k = kind_for(p)
        if k == "audio" and waiting_vo:
            r = T.call(ws, "narration_import", {"path": p}, scope=scope)
            notes.append(f"Imported {Path(p).name} as narration take {r['data'].get('take_no') if r['data'] else '?'} "
                         f"(alignment job {r['job_ids']}).")
        else:
            media.append(p)
    footage = [p for p in media if Path(p).is_dir() or kind_for(p) == "video"]
    other = [p for p in media if p not in footage]
    if footage:
        from ..projects import pref
        mode = pref(ws, pid, "analysis_mode", "standard")
        start, end = time_window(text)
        r = T.call(ws, "analysis_report", {"paths": footage, "mode": mode, "start": start, "end": end}, scope=scope)
        win = f" for {start:g}–{end:g}s" if end else ""
        notes.append(f"Registered {len(footage)} gameplay source(s) and started a {mode} analysis report{win} "
                     f"(job {r['job_ids']}); the report will be linked to this chat.")
    if other:
        r = T.call(ws, "assets_import", {"paths": other}, scope=scope)
        notes.append(f"Importing {len(other)} file(s) (job {r['job_ids']}).")
    return notes


def send(ws, thread_id: str, text: str, *, attachments: list[str] | None = None, provider: str | None = None,
         wait: bool = False) -> dict:
    thread = get_thread(ws, thread_id)
    msg = add_message(ws, thread_id, "user", text, meta={"attachments": attachments or []})
    for a in attachments or []:
        ws.db.insert("attachments", {"id": new_id("att"), "message_id": msg["id"], "path": a,
                                     "handled_as": "folder" if Path(a).is_dir() else "media_import", "created_at": now()})
    notes = handle_attachments(ws, thread, attachments, text) if attachments else []
    for n in notes:
        add_message(ws, thread_id, "system", n)
    turn_id = new_id("turn")
    cancel = threading.Event()
    _TURNS[turn_id] = cancel
    prompt = text + ("\n\n[Workspace already did: " + " ".join(notes) + "]" if notes else "")
    extra = turn_retrieval(ws, thread, text)
    if extra:
        prompt += "\n\n[Retrieved for this turn by the workspace — cite, don't assume more]\n" + extra
    result: dict = {}

    def work():
        nonlocal result
        result = _run(ws, thread, prompt, provider, cancel, turn_id)
        _TURNS.pop(turn_id, None)

    th = threading.Thread(target=work, name=turn_id, daemon=True)
    th.start()
    if wait:
        th.join()
        return {"turn_id": turn_id, "user_message_id": msg["id"], **result}
    return {"turn_id": turn_id, "user_message_id": msg["id"], "status": "running"}


def turn_retrieval(ws, thread: dict, text: str) -> str:
    """Relevant report sections, cross-project lessons, open reminders and corrections for this message only."""
    pid = thread["project_id"]
    parts = []
    try:
        if pid:
            from ..reports import report_sections
            secs = report_sections(ws, pid, text, limit=5)
            if secs:
                parts.append("Report sections: " + json.dumps([{"report": s_["report_id"], "rev": s_.get("revision"),
                                                                 "heading": s_["heading"], "asset": s_.get("asset_id"),
                                                                 "t": s_.get("start"), "body": s_["body"][:500]}
                                                                for s_ in secs]))
            from ..commitments import check_commitments
            rem = check_commitments(ws, pid)
            if rem:
                parts.append("Open reminders (evidence-backed, mention briefly, don't rewrite): " + json.dumps(rem, default=str)[:1500])
        from ..commitments import relevant_lessons
        les = relevant_lessons(ws, pid, text, limit=4)
        if les:
            parts.append("Lessons from other videos (name the origin; reminders are not rules): " +
                         json.dumps(les, default=str)[:1500])
        cor = ws.db.all("SELECT wrong, correct FROM corrections WHERE project_id IS ? OR project_id IS NULL "
                        "ORDER BY created_at DESC LIMIT 8", (pid,))
        if cor:
            parts.append("Corrections (never repeat the wrong version): " + json.dumps(cor))
    except Exception as e:  # noqa: BLE001 - retrieval must never block a turn
        parts.append(f"(retrieval error: {e})")
    return "\n".join(parts)


def cancel_turn(turn_id: str) -> bool:
    ev = _TURNS.get(turn_id)
    if ev:
        ev.set()
        return True
    return False


def _run(ws, thread, prompt, provider_name, cancel, turn_id) -> dict:
    prov = get_provider(ws, provider_name)
    avail = prov.available(ws)
    tid, pid = thread["id"], thread["project_id"]
    if not avail.get("ok"):
        add_message(ws, tid, "system", f"AI provider **{prov.name}** unavailable: {avail.get('reason')}. "
                    f"{avail.get('fix') or ''} Using offline commands for this turn.",
                    meta={"provider_unavailable": prov.name})
        prov = OfflineProvider()
    ws.emit("chat.turn_started", {"turn_id": turn_id, "provider": prov.name}, project_id=pid, thread_id=tid)
    tool_calls = []
    started = time.time()

    def on_text(t):
        ws.emit("chat.delta", {"turn_id": turn_id, "text": t}, project_id=pid, thread_id=tid)

    def on_tool(name, args):
        short = name.replace("mcp__shiftws__", "")
        tool_calls.append({"tool": short, "args": args})
        ws.db.insert("tool_events", {"id": new_id("tev"), "thread_id": tid, "turn_id": turn_id, "tool": short,
                                     "args_json": args, "created_at": now()})
        add_message(ws, tid, "tool", f"→ {short}", meta={"tool": short, "args": args, "turn_id": turn_id})

    def on_tool_result(tool_use_id, content):
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            data = None
        ev_row = ws.db.one("SELECT id FROM tool_events WHERE turn_id=? AND result_json IS NULL ORDER BY created_at LIMIT 1",
                           (turn_id,))
        if ev_row:
            ws.db.update("tool_events", {"id": ev_row["id"]}, {"result_json": content[:20000],
                                                              "job_ids_json": (data or {}).get("job_ids", []) if isinstance(data, dict) else []})
        if isinstance(data, dict) and data.get("job_ids"):
            for j in data["job_ids"]:
                ws.emit("chat.job_link", {"turn_id": turn_id, "job_id": j}, project_id=pid, thread_id=tid)

    def on_status(s):
        add_message(ws, tid, "system", s)

    try:
        res = prov.run_turn(ws, {**thread, **get_thread(ws, tid)}, prompt,
                            TurnEvents(on_text, on_tool, on_tool_result, on_status), cancel)
    except Exception as e:  # noqa: BLE001
        res = {"is_error": True, "error": str(e), "text": ""}
    meta = {"provider": prov.name, "turn_id": turn_id, "tools": tool_calls, "seconds": round(time.time() - started, 1),
            "cost_usd": res.get("cost_usd"), "session_id": res.get("session_id")}
    if res.get("cancelled"):
        add_message(ws, tid, "system", "Turn cancelled. Running jobs continue; cancel them from their cards.", meta=meta)
    elif res.get("is_error") and not res.get("text"):
        add_message(ws, tid, "system", f"The AI turn failed: {res.get('error')}. Your message is saved; retry or use "
                    "offline commands.", meta=meta | {"error": True})
    else:
        add_message(ws, tid, "assistant", res.get("text") or "(no reply)", meta=meta)
    if res.get("session_id"):
        ws.db.update("threads", {"id": tid}, {"provider": prov.name, "provider_session_id": res["session_id"],
                                              "updated_at": now()})
    if pid:
        from ..projects import touch, write_manifest
        touch(ws, pid)
        write_manifest(ws, pid)
    ws.emit("chat.turn_done", {"turn_id": turn_id, "provider": prov.name}, project_id=pid, thread_id=tid)
    return {"provider": prov.name, "text": res.get("text"), "tools": tool_calls, "error": res.get("error")}


def resume_summary(ws, thread_id: str) -> dict:
    """'Where we stopped' for a thread, from structured state — shown when a chat is reopened."""
    t = get_thread(ws, thread_id)
    if t["project_id"]:
        from ..projects import project_context
        ctx = project_context(ws, t["project_id"])
        w = ctx["where_we_stopped"]
        running = [j for j in ctx["active_jobs"] if j["status"] in ("queued", "running")]
        approved = [d["subject"] for d in ctx["decisions"] if d["kind"] == "approval"][:5]
        blockers = [f"{j['kind']}: {j['error']}" for j in ctx["active_jobs"] if j["status"] == "failed"]
        offline = [a["title"] for a in ctx["assets"] if a["availability"] != "online"]
        if offline:
            blockers.append(f"offline media (relink to continue media jobs): {', '.join(offline[:3])}")
        from ..commitments import check_commitments
        reminders = [f"{r['kind']} (from {r['date']}: {r['subject']})" for r in check_commitments(ws, t["project_id"])]
        return {"summary": w["summary"], "next_action": w["next_action"], "open_tasks": ctx["open_tasks"],
                "running_jobs": running, "approved": approved, "blockers": blockers, "reminders": reminders}
    n = ws.db.one("SELECT COUNT(*) n FROM projects WHERE status='active'")["n"]
    recent = ws.db.all("SELECT title, stage FROM projects WHERE status='active' ORDER BY updated_at DESC LIMIT 3")
    return {"summary": f"{n} active project(s). Recent: " + "; ".join(f"{r['title']} ({r['stage']})" for r in recent),
            "next_action": "Pick a project or start a new one.", "open_tasks": ws.open_tasks(None), "running_jobs": []}
