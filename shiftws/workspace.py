"""The Workspace: one object that owns config, database, events, search and memory.
UI buttons, chat, MCP tools and the CLI all call these same methods."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Config, load_config
from .db import DB
from .util import new_id, now


class Workspace:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or load_config()
        self.db = DB(self.cfg.db_path)
        from . import projects
        self.channel_id = projects.ensure_channel(self)

    # ------------------------------------------------------------------ events
    def emit(self, kind: str, payload: dict | None = None, *, project_id=None, thread_id=None, ref_id=None) -> int:
        cur = self.db.x("INSERT INTO events (kind, project_id, thread_id, ref_id, payload_json, created_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (kind, project_id, thread_id, ref_id, json.dumps(payload or {}, default=str), now()))
        return cur.lastrowid

    def events_since(self, seq: int, project_id: str | None = None, limit: int = 200) -> list[dict]:
        if project_id:
            rows = self.db.all("SELECT * FROM events WHERE seq>? AND (project_id=? OR project_id IS NULL) "
                               "ORDER BY seq LIMIT ?", (seq, project_id, limit))
        else:
            rows = self.db.all("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (seq, limit))
        for r in rows:
            r["payload"] = json.loads(r.pop("payload_json"))
        return rows

    # ------------------------------------------------------------------ search
    def index(self, kind: str, ref_id: str, title: str, body: str, *, project_id=None, thread_id=None,
              locator: dict | None = None) -> None:
        self.db.x("DELETE FROM search_index WHERE kind=? AND ref_id=?", (kind, ref_id))
        self.db.x("INSERT INTO search_index (kind, ref_id, project_id, thread_id, locator, title, body) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (kind, ref_id, project_id, thread_id, json.dumps(locator or {}), title or "", body or ""))

    def unindex(self, kind: str, ref_id: str) -> None:
        self.db.x("DELETE FROM search_index WHERE kind=? AND ref_id=?", (kind, ref_id))

    def search(self, query: str, *, scope: str = "all", project_id: str | None = None,
               thread_id: str | None = None, kinds: list[str] | None = None, limit: int = 25) -> list[dict]:
        """scope: thread | project | all. Returns clickable results with a locator (thread/message,
        asset+timestamp, script version, ...)."""
        fts = _fts_query(query)
        if not fts:
            return []
        sql = ("SELECT kind, ref_id, project_id, thread_id, locator, title, "
               "snippet(search_index, 6, '[', ']', ' … ', 14) AS snippet, bm25(search_index, 4.0, 1.0) AS rank "
               "FROM search_index WHERE search_index MATCH ?")
        params: list = [fts]
        if scope == "thread" and thread_id:
            sql += " AND thread_id=?"; params.append(thread_id)
        elif scope == "project" and project_id:
            sql += " AND project_id=?"; params.append(project_id)
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"; params += kinds
        sql += " ORDER BY rank LIMIT ?"; params.append(limit)
        rows = self.db.all(sql, params)
        titles = {p["id"]: p["title"] for p in self.db.all("SELECT id, title FROM projects")}
        for r in rows:
            r["locator"] = json.loads(r["locator"] or "{}")
            r["project_title"] = titles.get(r["project_id"])
        return rows

    # ------------------------------------------------------------------ memory
    def remember(self, *, kind: str, subject: str, content: str, project_id: str | None = None,
                 thread_id: str | None = None, ref_type: str | None = None, ref_id: str | None = None,
                 data: dict | None = None, provenance: str = "user", scope: str | None = None,
                 supersedes: str | None = None) -> dict:
        scope = scope or ("project" if project_id else "channel")
        if scope == "project" and not project_id:
            raise ValueError("project-scoped memory needs a project_id")
        d = {"id": new_id("dec"), "scope": scope, "project_id": project_id if scope == "project" else None,
             "thread_id": thread_id, "kind": kind, "subject": subject, "content": content, "ref_type": ref_type,
             "ref_id": ref_id, "data_json": data or {}, "status": "active", "supersedes": supersedes,
             "provenance": provenance, "created_at": now(), "updated_at": now()}
        self.db.insert("decisions", d)
        if supersedes:
            self.db.update("decisions", {"id": supersedes}, {"status": "superseded", "updated_at": now()})
            self.unindex("decision", supersedes)
        self.index("decision", d["id"], f"{kind}: {subject}", content + " " + json.dumps(data or {}),
                   project_id=d["project_id"], thread_id=thread_id,
                   locator={"decision_id": d["id"], "ref_type": ref_type, "ref_id": ref_id, "thread_id": thread_id})
        self.emit("memory.saved", {"id": d["id"], "kind": kind, "subject": subject}, project_id=project_id,
                  thread_id=thread_id, ref_id=d["id"])
        return self.decision(d["id"])

    def decision(self, did: str) -> dict | None:
        r = self.db.one("SELECT * FROM decisions WHERE id=?", (did,))
        if r:
            r["data"] = json.loads(r.pop("data_json"))
        return r

    def memories(self, *, project_id: str | None = None, include_channel: bool = True, kinds=None,
                 status: str = "active", limit: int = 200) -> list[dict]:
        where, params = ["status=?"], [status]
        if project_id:
            where.append("(project_id=?" + (" OR scope='channel')" if include_channel else ")"))
            params.append(project_id)
        else:
            where.append("scope='channel'")
        if kinds:
            where.append(f"kind IN ({','.join('?' * len(kinds))})"); params += kinds
        rows = self.db.all(f"SELECT * FROM decisions WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?",
                           params + [limit])
        for r in rows:
            r["data"] = json.loads(r.pop("data_json"))
        return rows

    def correct_memory(self, did: str, *, content: str | None = None, subject: str | None = None,
                       data: dict | None = None, provenance: str = "user correction") -> dict:
        old = self.decision(did)
        if not old:
            raise KeyError(did)
        return self.remember(kind=old["kind"], subject=subject or old["subject"], content=content or old["content"],
                             project_id=old["project_id"], thread_id=old["thread_id"], ref_type=old["ref_type"],
                             ref_id=old["ref_id"], data=data if data is not None else old["data"],
                             provenance=provenance, scope=old["scope"], supersedes=did)

    def forget(self, did: str) -> None:
        self.db.update("decisions", {"id": did}, {"status": "deleted", "updated_at": now()})
        self.unindex("decision", did)
        self.emit("memory.deleted", {"id": did}, ref_id=did)

    # ------------------------------------------------------------------ tasks
    def add_task(self, project_id: str | None, title: str, *, kind: str = "todo", detail: str = "") -> dict:
        existing = self.db.one("SELECT * FROM tasks WHERE project_id IS ? AND title=? AND status='open'",
                               (project_id, title))
        if existing:
            return existing
        t = {"id": new_id("task"), "project_id": project_id, "title": title, "status": "open", "kind": kind,
             "detail": detail, "created_at": now()}
        self.db.insert("tasks", t)
        self.emit("task.added", t, project_id=project_id, ref_id=t["id"])
        return t

    def close_task(self, project_id: str | None, title_prefix: str, status: str = "done") -> int:
        cur = self.db.x("UPDATE tasks SET status=?, done_at=? WHERE project_id IS ? AND status='open' AND title LIKE ?",
                        (status, now(), project_id, title_prefix + "%"))
        return cur.rowcount

    def open_tasks(self, project_id: str | None) -> list[dict]:
        return self.db.all("SELECT * FROM tasks WHERE project_id IS ? AND status='open' ORDER BY created_at",
                           (project_id,))

    # ------------------------------------------------------------------ automation measurement
    def automation(self, project_id: str | None, task: str, mode: str, detail: str = "") -> None:
        self.db.insert("automation_events", {"id": new_id("auto"), "project_id": project_id, "task": task,
                                             "mode": mode, "detail": detail, "created_at": now()})

    def automation_report(self, project_id: str | None = None) -> dict:
        where = "WHERE project_id=?" if project_id else ""
        rows = self.db.all(f"SELECT task, mode, COUNT(*) n FROM automation_events {where} GROUP BY task, mode",
                           (project_id,) if project_id else ())
        totals = {"automated": 0, "handoff": 0, "user_decision": 0}
        by_task: dict = {}
        for r in rows:
            totals[r["mode"]] = totals.get(r["mode"], 0) + r["n"]
            by_task.setdefault(r["task"], {})[r["mode"]] = r["n"]
        n = sum(totals.values())
        return {"totals": totals, "by_task": by_task, "steps": n,
                "automated_share": round(totals["automated"] / n, 3) if n else None,
                "note": "Measured from recorded steps in this workspace; a goal metric, not a claim."}


def _fts_query(q: str) -> str:
    words = re.findall(r"[\w']+", q.lower())
    stop = {"the", "a", "an", "for", "my", "we", "i", "to", "of", "in", "on", "and", "find", "that", "this",
            "with", "from", "me", "show", "where", "what", "is", "it", "was", "our"}
    terms = [w.replace("'", "") for w in words if w not in stop and len(w) > 1]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"*' for t in terms)


def open_workspace(home: str | Path | None = None) -> Workspace:
    return Workspace(load_config(home))
