"""Workspace MCP server (stdio) for Claude Code.

Started by the chat's Claude Code runner with SHIFTWS_HOME / SHIFTWS_PROJECT_ID / SHIFTWS_THREAD_ID, or
registered by the user in a project-scoped .mcp.json. Tools enqueue the same jobs as UI buttons.
If the desktop engine is not running, this process runs workers itself so queued jobs still progress.
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import tools as T
from .workspace import open_workspace

INSTRUCTIONS = """5 to 9 Shift workspace tools. Act through these tools instead of telling the user to run commands.
Pass explicit project/asset ids when you have them; the current project/thread are defaults.
Long work returns job ids — use job_wait to follow it. Save decisions with memory_save (what changed and why).
Never invent footage moments: use footage_find / scene_frames, Read the frame images, then scene_describe with an
honest confidence. Scripts: script_save always produces ElevenLabs-tagged + clean versions."""


def engine_alive(home: Path) -> bool:
    hb = home / "engine.heartbeat"
    try:
        return time.time() - float(hb.read_text()) < 15
    except (OSError, ValueError):
        return False


def build_server(ws) -> MCPServer:
    server = MCPServer(name="shiftws", instructions=INSTRUCTIONS, version="0.1.0")
    for fn in T.TOOLS:
        server.tool(name=fn.__name__, description=(fn.__doc__ or fn.__name__).strip())(_bind(ws, fn))

    @server.resource("shiftws://project/{project_id}/context", name="project_context", mime_type="application/json")
    def res_context(project_id: str) -> str:
        from .projects import project_context
        return json.dumps(project_context(ws, project_id), default=str)

    @server.resource("shiftws://project/{project_id}/script", name="latest_script", mime_type="application/json")
    def res_script(project_id: str) -> str:
        from .scripts_eleven import get_script
        return json.dumps(get_script(ws, project_id=project_id), default=str)

    @server.resource("shiftws://channel/voice", name="voice_md", mime_type="text/markdown")
    def res_voice() -> str:
        return (Path(ws.cfg.home) / "voice.md").read_text()

    return server


def _bind(ws, fn):
    import typing
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)
    params = [p.replace(annotation=hints.get(p.name, p.annotation)) for p in list(sig.parameters.values())[1:]]

    @functools.wraps(fn)
    def wrapper(**kwargs):
        res = T.call(ws, fn.__name__, kwargs)
        return json.dumps(res, default=str)

    wrapper.__signature__ = sig.replace(parameters=params, return_annotation=str)
    wrapper.__annotations__ = {p.name: p.annotation for p in params} | {"return": str}
    return wrapper


def main():
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    ws = open_workspace(os.environ.get("SHIFTWS_HOME"))
    if not engine_alive(Path(ws.cfg.home)):
        from .jobs import Workers
        Workers(ws, parallel=int(ws.cfg.get("media_parallelism", 2))).start()
    build_server(ws).run("stdio")


if __name__ == "__main__":
    main()
