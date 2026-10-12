"""Threads and messages. One persistent main thread per project, optional topic threads, one channel thread."""
from __future__ import annotations

import json

from ..util import new_id, now

THREAD_KINDS = {"main": "Project chat", "research": "Research", "script": "Script", "footage": "Footage",
                "audio": "Audio", "edit": "Edit", "results": "Results", "channel": "Channel workspace"}


def create_thread(ws, project_id: str | None, kind: str, title: str | None = None) -> dict:
    if kind not in THREAD_KINDS:
        raise ValueError(f"thread kind must be one of {list(THREAD_KINDS)}")
    if kind == "main":
        ex = ws.db.one("SELECT * FROM threads WHERE project_id=? AND kind='main'", (project_id,))
        if ex:
            return ex
    t = {"id": new_id("th"), "project_id": project_id, "kind": kind, "title": title or THREAD_KINDS[kind],
         "created_at": now(), "updated_at": now()}
    ws.db.insert("threads", t)
    ws.emit("thread.created", t, project_id=project_id, thread_id=t["id"])
    return t


def get_thread(ws, thread_id: str) -> dict:
    t = ws.db.one("SELECT * FROM threads WHERE id=?", (thread_id,))
    if not t:
        raise KeyError(thread_id)
    return t


def list_threads(ws, project_id: str | None = None) -> list[dict]:
    if project_id:
        return ws.db.all("SELECT * FROM threads WHERE project_id=? ORDER BY kind!='main', created_at", (project_id,))
    return ws.db.all("SELECT t.*, p.title AS project_title FROM threads t LEFT JOIN projects p ON p.id=t.project_id "
                     "ORDER BY t.updated_at DESC")


def add_message(ws, thread_id: str, role: str, content: str, meta: dict | None = None) -> dict:
    t = get_thread(ws, thread_id)
    m = {"id": new_id("msg"), "thread_id": thread_id, "role": role, "content": content, "meta_json": meta or {},
         "created_at": now()}
    ws.db.insert("messages", m)
    ws.db.update("threads", {"id": thread_id}, {"updated_at": now()})
    if role in ("user", "assistant") and content.strip():
        ws.index("message", m["id"], f"{t['title']} — {role}", content, project_id=t["project_id"], thread_id=thread_id,
                 locator={"thread_id": thread_id, "message_id": m["id"]})
    ws.emit("chat.message", {"id": m["id"], "role": role, "content": content, "meta": meta or {}},
            project_id=t["project_id"], thread_id=thread_id, ref_id=m["id"])
    return {**m, "meta": meta or {}}


def messages(ws, thread_id: str, limit: int = 200, before: str | None = None) -> list[dict]:
    rows = ws.db.all("SELECT * FROM messages WHERE thread_id=? ORDER BY created_at DESC LIMIT ?", (thread_id, limit))
    rows.reverse()
    for r in rows:
        r["meta"] = json.loads(r.pop("meta_json"))
    return rows
