"""Local engine API for the desktop shell. Loopback only, per-install session token on every request.
UI buttons call /api/tools/<name> — the same tool functions the chat agent uses."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from . import jobs as J
from . import projects as P
from . import tools as T
from .chat import engine as chat
from .chat import threads as TH
from .workspace import Workspace

WS: Workspace | None = None
TOKEN = ""


def create_app(ws: Workspace, start_background: bool = True) -> FastAPI:
    global WS, TOKEN
    WS, TOKEN = ws, ws.cfg.api_token()
    app = FastAPI(title="5 to 9 Shift engine", docs_url=None, redoc_url=None)

    def auth(request: Request):
        tok = request.headers.get("x-shiftws-token") or request.query_params.get("token")
        if tok != TOKEN:
            raise HTTPException(401, "missing or bad session token")
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            raise HTTPException(403, "loopback only")

    if start_background:
        _start_background(ws)

    @app.get("/api/ping")
    def ping():
        return {"ok": True, "app": "5 to 9 Shift"}

    @app.get("/api/projects", dependencies=[Depends(auth)])
    def projects():
        out = []
        for p in P.list_projects(ws):
            th = TH.list_threads(ws, p["id"])
            out.append({**p, "threads": th, "deliverables": P.list_deliverables(ws, p["id"])})
        return {"projects": out, "channel_thread": TH.get_thread(ws, "th_channel")}

    @app.post("/api/projects", dependencies=[Depends(auth)])
    def create_project(body: dict = Body(...)):
        return T.call(ws, "project_create", body, scope={"origin": "ui"})

    @app.get("/api/projects/{pid}/context", dependencies=[Depends(auth)])
    def context(pid: str):
        return P.project_context(ws, pid)

    @app.post("/api/threads", dependencies=[Depends(auth)])
    def new_thread(body: dict = Body(...)):
        return TH.create_thread(ws, body.get("project_id"), body["kind"], body.get("title"))

    @app.get("/api/threads/{tid}/messages", dependencies=[Depends(auth)])
    def messages(tid: str, limit: int = 300):
        return {"thread": TH.get_thread(ws, tid), "messages": TH.messages(ws, tid, limit)}

    @app.get("/api/threads/{tid}/resume", dependencies=[Depends(auth)])
    def resume(tid: str):
        return chat.resume_summary(ws, tid)

    @app.post("/api/threads/{tid}/send", dependencies=[Depends(auth)])
    def send(tid: str, body: dict = Body(...)):
        return chat.send(ws, tid, body.get("text", ""), attachments=body.get("attachments") or [],
                         provider=body.get("provider"))

    @app.post("/api/turns/{turn_id}/cancel", dependencies=[Depends(auth)])
    def cancel_turn(turn_id: str):
        return {"cancelled": chat.cancel_turn(turn_id)}

    @app.get("/api/jobs", dependencies=[Depends(auth)])
    def jobs(project_id: str | None = None):
        return {"jobs": J.list_jobs(ws, project_id, None, 60)}

    @app.get("/api/jobs/{jid}", dependencies=[Depends(auth)])
    def job(jid: str):
        return J.get(ws, jid)

    @app.post("/api/jobs/{jid}/{action}", dependencies=[Depends(auth)])
    def job_action(jid: str, action: str):
        if action == "cancel":
            return J.cancel(ws, jid)
        if action == "resume":
            return J.resume(ws, jid)
        raise HTTPException(400, "unknown action")

    @app.post("/api/tools/{name}", dependencies=[Depends(auth)])
    def tool(name: str, body: dict = Body(default={})):
        if name not in T.BY_NAME:
            raise HTTPException(404, f"no tool {name}")
        scope = body.pop("_scope", {}) or {}
        return T.call(ws, name, body, scope={"origin": "ui", **scope})

    @app.get("/api/search", dependencies=[Depends(auth)])
    def search(q: str, scope: str = "all", project_id: str | None = None, thread_id: str | None = None):
        return {"results": ws.search(q, scope=scope, project_id=project_id, thread_id=thread_id, limit=40)}

    @app.get("/api/memory", dependencies=[Depends(auth)])
    def memory(project_id: str | None = None, status: str = "active"):
        return {"items": ws.memories(project_id=project_id, status=status, limit=500)}

    @app.post("/api/memory/{did}", dependencies=[Depends(auth)])
    def memory_correct(did: str, body: dict = Body(...)):
        from .commitments import record_correction
        return record_correction(ws, did, body["content"])

    @app.delete("/api/memory/{did}", dependencies=[Depends(auth)])
    def memory_delete(did: str):
        ws.forget(did)
        return {"deleted": did}

    @app.get("/api/notifications", dependencies=[Depends(auth)])
    def notifications():
        return {"items": ws.db.all("SELECT * FROM notifications ORDER BY created_at DESC LIMIT 60")}

    @app.get("/api/connections", dependencies=[Depends(auth)])
    def connections():
        from .chat.providers import PROVIDERS
        from .registry import load_registry
        provs = {}
        for name, cls in PROVIDERS.items():
            try:
                provs[name] = cls().available(ws)
            except Exception as e:  # noqa: BLE001
                provs[name] = {"ok": False, "reason": str(e)}
        return {"providers": provs, "active_provider": ws.cfg.get("ai.provider"), "integrations": load_registry(),
                "settings": {k: ws.cfg.settings[k] for k in ("projects_root", "watch_folders", "transcription", "premiere")}}

    @app.get("/api/premiere", dependencies=[Depends(auth)])
    def premiere():
        from .premiere.adapter import status
        return status(ws)

    @app.get("/api/doctor", dependencies=[Depends(auth)])
    def doctor():
        from .health import doctor as d, report_markdown
        r = d(ws)
        return {"report": r, "markdown": report_markdown(r)}

    @app.get("/api/automation", dependencies=[Depends(auth)])
    def automation(project_id: str | None = None):
        return ws.automation_report(project_id)

    @app.get("/api/file", dependencies=[Depends(auth)])
    def file(path: str):
        from .util import ensure_within
        roots = [ws.cfg.projects_root, ws.cfg.cache_dir] + [Path(a["current_path"]).parent for a in
                                                           ws.db.all("SELECT DISTINCT current_path FROM media_assets")]
        p = ensure_within(path, roots)
        if not p.exists():
            raise HTTPException(404, "not found")
        return FileResponse(p)

    @app.get("/api/events", dependencies=[Depends(auth)])
    async def events(since: int = 0, project_id: str | None = None):
        async def gen():
            seq = since
            idle = 0
            while True:
                rows = ws.events_since(seq, project_id)
                for r in rows:
                    seq = r["seq"]
                    yield f"id: {seq}\ndata: {json.dumps(r, default=str)}\n\n"
                if not rows:
                    idle += 1
                    if idle % 30 == 0:
                        yield ": keepalive\n\n"
                    await asyncio.sleep(0.4)
        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def _start_background(ws):
    from .catalog import CatalogWatcher
    from .jobs import Workers
    from .narration import NarrationWatcher
    Workers(ws, parallel=int(ws.cfg.get("media_parallelism", 2))).start()
    NarrationWatcher(ws).start()
    CatalogWatcher(ws).start()
    hb = Path(ws.cfg.home) / "engine.heartbeat"

    def beat():
        while True:
            hb.write_text(str(time.time()))
            time.sleep(5)
    threading.Thread(target=beat, daemon=True, name="engine-heartbeat").start()


def serve(ws, port: int = 8765):
    import uvicorn
    app = create_app(ws)
    (Path(ws.cfg.home) / "engine.port").write_text(str(port))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
