"""Persistent job queue shared by the UI, chat, MCP tools and CLI.

- Jobs live in SQLite; any process running workers can claim one atomically.
- The 'premiere' lane is serialized across processes (one writer); 'media' jobs run in parallel.
- Handlers checkpoint progress so an interrupted job resumes instead of starting over.
- Results are cached by (kind, input_hash): re-requesting finished work returns the existing job.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable

from .util import Cancelled, ToolError, hash_obj, new_id, now, redact

HANDLERS: dict[str, Callable] = {}
LANES: dict[str, str] = {}
STALE_SECONDS = 90


def handler(kind: str, lane: str = "media"):
    def deco(fn):
        HANDLERS[kind] = fn
        LANES[kind] = lane
        return fn
    return deco


class JobFailed(Exception):
    def __init__(self, message: str, hint: str = "", retryable: bool = False):
        super().__init__(message)
        self.hint = hint
        self.retryable = retryable


@dataclass
class JobContext:
    ws: object
    job: dict

    @property
    def id(self) -> str:
        return self.job["id"]

    @property
    def params(self) -> dict:
        return self.job["params"]

    @property
    def checkpoint(self) -> dict:
        return self.job["checkpoint"]

    def progress(self, fraction: float, message: str = "", stage: str | None = None) -> None:
        vals = {"progress": max(0.0, min(1.0, fraction)), "heartbeat_at": now()}
        if message:
            vals["message"] = message[:300]
        if stage:
            vals["stage"] = stage
        self.ws.db.update("jobs", {"id": self.id}, vals)
        self.ws.emit("job.progress", {"id": self.id, "kind": self.job["kind"], **vals},
                     project_id=self.job["project_id"], thread_id=self.job["thread_id"], ref_id=self.id)
        if self.cancelled():
            raise Cancelled(self.id)

    def save_checkpoint(self, **data) -> None:
        self.job["checkpoint"].update(data)
        self.ws.db.update("jobs", {"id": self.id}, {"checkpoint_json": self.job["checkpoint"], "heartbeat_at": now()})

    def cancelled(self) -> bool:
        r = self.ws.db.one("SELECT cancel_requested FROM jobs WHERE id=?", (self.id,))
        return bool(r and r["cancel_requested"])

    def log(self, line: str) -> None:
        from pathlib import Path
        d = Path(self.ws.cfg.logs_dir)
        with open(d / f"{self.id}.log", "a") as f:
            f.write(redact(line) + "\n")


def enqueue(ws, kind: str, params: dict, *, project_id: str | None = None, thread_id: str | None = None,
            origin: str = "ui", cache_key: dict | None = None, max_attempts: int = 2, force: bool = False) -> dict:
    """Queue a job. If an identical job (same kind + input hash) already finished and its outputs still exist,
    return that job instead of repeating the work."""
    from . import workers  # noqa: F401  (registers handlers)
    if kind not in HANDLERS:
        raise ValueError(f"no handler for job kind {kind}")
    input_hash = hash_obj({"kind": kind, **(cache_key if cache_key is not None else params)})
    if not force:
        prev = ws.db.one("SELECT * FROM jobs WHERE kind=? AND input_hash=? AND status IN ('queued','running','done') "
                         "ORDER BY created_at DESC LIMIT 1", (kind, input_hash))
        if prev and (prev["status"] != "done" or _outputs_exist(prev)):
            job = get(ws, prev["id"])
            job["reused"] = True
            if thread_id and thread_id != prev["thread_id"]:
                _job_message(ws, job, thread_id, reused=True)
            return job
    job = {"id": new_id("job"), "project_id": project_id, "thread_id": thread_id, "kind": kind,
           "lane": LANES.get(kind, "media"), "status": "queued", "params_json": params, "input_hash": input_hash,
           "max_attempts": max_attempts, "origin": origin, "created_at": now()}
    ws.db.insert("jobs", job)
    ws.emit("job.queued", {"id": job["id"], "kind": kind, "params": params}, project_id=project_id,
            thread_id=thread_id, ref_id=job["id"])
    j = get(ws, job["id"])
    if thread_id:
        _job_message(ws, j, thread_id)
    return j


def _job_message(ws, job, thread_id, reused=False):
    from .util import new_id as _nid
    ws.db.insert("messages", {"id": _nid("msg"), "thread_id": thread_id, "role": "job",
                              "content": f"{job['kind']} job {'reused (already done)' if reused else 'started'}",
                              "meta_json": {"job_id": job["id"], "reused": reused}, "created_at": now()})


def _outputs_exist(job: dict) -> bool:
    outs = json.loads(job["outputs_json"]) if isinstance(job["outputs_json"], str) else job["outputs_json"]
    return all(os.path.exists(p) for p in outs)


def get(ws, job_id: str) -> dict | None:
    j = ws.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not j:
        return None
    for k in ("params", "result", "outputs", "checkpoint"):
        j[k] = json.loads(j.pop(f"{k}_json"))
    return j


def list_jobs(ws, project_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict]:
    where, params = [], []
    if project_id:
        where.append("project_id=?"); params.append(project_id)
    if status:
        where.append("status=?"); params.append(status)
    sql = "SELECT id FROM jobs" + (f" WHERE {' AND '.join(where)}" if where else "") + \
          " ORDER BY created_at DESC LIMIT ?"
    return [get(ws, r["id"]) for r in ws.db.all(sql, params + [limit])]


def cancel(ws, job_id: str) -> dict:
    ws.db.x("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
    ws.db.x("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=? AND status='queued'", (now(), job_id))
    j = get(ws, job_id)
    ws.emit("job.cancel_requested", {"id": job_id}, project_id=j["project_id"], thread_id=j["thread_id"],
            ref_id=job_id)
    return j


def resume(ws, job_id: str) -> dict:
    """Re-queue a failed or cancelled job; it continues from its checkpoint."""
    ws.db.x("UPDATE jobs SET status='queued', cancel_requested=0, error=NULL, error_hint=NULL, attempts=0 "
            "WHERE id=? AND status IN ('failed','cancelled')", (job_id,))
    j = get(ws, job_id)
    ws.emit("job.queued", {"id": job_id, "kind": j["kind"], "resumed": True}, project_id=j["project_id"],
            thread_id=j["thread_id"], ref_id=job_id)
    return j


def recover(ws) -> list[str]:
    """After a crash/restart: jobs left 'running' with a stale heartbeat go back to the queue."""
    cutoff = time.time() - STALE_SECONDS
    stale = []
    for j in ws.db.all("SELECT id, heartbeat_at, started_at FROM jobs WHERE status='running'"):
        hb = j["heartbeat_at"] or j["started_at"]
        if not hb or _ts(hb) < cutoff:
            ws.db.x("UPDATE jobs SET status='queued', message='recovered after restart' WHERE id=?", (j["id"],))
            stale.append(j["id"])
    return stale


def _ts(s: str) -> float:
    from datetime import datetime, timezone
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


def claim(ws, lanes=("media", "premiere")) -> dict | None:
    """Atomically claim the oldest queued job. Premiere jobs only when no other Premiere job is running."""
    for lane in lanes:
        extra = ("AND NOT EXISTS (SELECT 1 FROM jobs j2 WHERE j2.lane='premiere' AND j2.status='running')"
                 if lane == "premiere" else "")
        cur = ws.db.x(
            f"UPDATE jobs SET status='running', attempts=attempts+1, started_at=?, heartbeat_at=? "
            f"WHERE id=(SELECT id FROM jobs WHERE status='queued' AND lane=? {extra} ORDER BY created_at LIMIT 1) "
            f"AND status='queued' RETURNING id", (now(), now(), lane))
        row = cur.fetchone()
        if row:
            return get(ws, row[0])
    return None


def run_job(ws, job: dict) -> dict:
    ctx = JobContext(ws, job)
    fn = HANDLERS[job["kind"]]
    ws.emit("job.started", {"id": job["id"], "kind": job["kind"]}, project_id=job["project_id"],
            thread_id=job["thread_id"], ref_id=job["id"])
    stop = threading.Event()

    def beat():
        while not stop.wait(15):
            ws.db.update("jobs", {"id": job["id"]}, {"heartbeat_at": now()})
    hb = threading.Thread(target=beat, daemon=True)
    hb.start()
    try:
        result = fn(ctx) or {}
        outputs = result.pop("_outputs", [])
        ws.db.update("jobs", {"id": job["id"]}, {"status": "done", "progress": 1.0, "result_json": result,
                                                  "outputs_json": outputs, "finished_at": now(),
                                                  "message": result.get("summary", "done")[:300]})
        ws.emit("job.done", {"id": job["id"], "kind": job["kind"], "result": result}, project_id=job["project_id"],
                thread_id=job["thread_id"], ref_id=job["id"])
    except Cancelled:
        ws.db.update("jobs", {"id": job["id"]}, {"status": "cancelled", "finished_at": now(),
                                                  "message": "cancelled"})
        ws.emit("job.cancelled", {"id": job["id"], "kind": job["kind"]}, project_id=job["project_id"],
                thread_id=job["thread_id"], ref_id=job["id"])
    except (JobFailed, ToolError, Exception) as e:  # noqa: BLE001 - every failure is recorded, none crash the worker
        hint = getattr(e, "hint", "")
        retryable = getattr(e, "retryable", not isinstance(e, (JobFailed, ValueError, KeyError, PermissionError)))
        detail = redact(getattr(e, "output", "") or traceback.format_exc(limit=4))
        ctx.log(detail)
        j = get(ws, job["id"])
        if retryable and j["attempts"] < j["max_attempts"]:
            ws.db.update("jobs", {"id": job["id"]}, {"status": "queued", "message": f"retrying: {e}"[:300]})
        else:
            ws.db.update("jobs", {"id": job["id"]}, {"status": "failed", "error": redact(str(e))[:500],
                                                      "error_hint": hint, "finished_at": now()})
            ws.emit("job.failed", {"id": job["id"], "kind": job["kind"], "error": redact(str(e)), "hint": hint},
                    project_id=job["project_id"], thread_id=job["thread_id"], ref_id=job["id"])
    finally:
        stop.set()
    return get(ws, job["id"])


class Workers:
    """Background worker pool. Media lane runs `parallel` jobs; the Premiere lane gets its own single worker."""

    def __init__(self, ws, parallel: int = 2):
        self.ws = ws
        self.parallel = parallel
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        from . import workers  # noqa: F401  (registers handlers)
        recover(self.ws)
        for i in range(self.parallel):
            self._spawn(("media",), f"media-{i}")
        self._spawn(("premiere",), "premiere")

    def _spawn(self, lanes, name):
        def loop():
            while not self._stop.is_set():
                job = claim(self.ws, lanes)
                if job is None:
                    self._stop.wait(0.5)
                    continue
                run_job(self.ws, job)
        t = threading.Thread(target=loop, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()


def wait(ws, job_id: str, timeout: float = 600, poll: float = 0.3) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        j = get(ws, job_id)
        if j["status"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(poll)
    return get(ws, job_id)


def run_inline(ws, job: dict) -> dict:
    """Run a queued job in this thread (CLI/tests without a worker pool)."""
    from . import workers  # noqa: F401
    if job["status"] == "done":
        return job
    ws.db.x("UPDATE jobs SET status='running', attempts=attempts+1, started_at=?, heartbeat_at=? WHERE id=? "
            "AND status='queued'", (now(), now(), job["id"]))
    return run_job(ws, get(ws, job["id"]))
