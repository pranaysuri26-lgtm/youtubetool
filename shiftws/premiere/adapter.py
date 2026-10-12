"""Premiere controller boundary.

The app speaks normalized operations; this adapter maps them to ONE active writer bridge. Currently supported
bridge: hetpatel-11/Adobe_Premiere_Pro_MCP (CEP panel) via its MCP server, reached through its catalog tools
(get_tool_schema / invoke_tool). Its tool argument names are verified against the live schemas at runtime
(schema_contract) — never assumed.

Statuses are kept independently: bridge server (MCP), Premiere host, CEP panel, export backend.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from pathlib import Path

# normalized op → (bridge tool, required argument names in the bridge's schema)
OPS = {
    "connection_check": ("verify_premiere_connection", []),
    "project_info": ("get_project_info", []),
    "save": ("save_project", []),
    "import_xml": ("import_fcp_xml", ["filePath"]),
    "import_media": ("import_media", ["filePath"]),
    "list_sequences": ("list_sequences", []),
    "read_tracks": ("list_sequence_tracks", ["sequenceId"]),
    "place_clip": ("add_to_timeline", ["sequenceId", "projectItemId", "trackIndex", "time"]),
    "set_motion": ("set_clip_properties", ["clipId", "properties"]),
    "set_gain": ("adjust_audio_levels", ["clipId", "level"]),
    "add_marker": ("add_marker", ["sequenceId", "time", "name"]),
    "caption_track": ("create_caption_track", ["sequenceId", "projectItemId"]),
    "duplicate_sequence": ("duplicate_sequence", ["sequenceId", "newName"]),
    "find_item": ("find_project_item_by_name", ["name"]),
    "export": ("export_sequence", ["sequenceId", "outputPath"]),
    "offline_media": ("check_offline_media", []),
}


def bridge_command(ws) -> list[str] | None:
    cmd = ws.cfg.get("premiere.mcp_command") or ["premiere-pro-mcp"]
    if shutil.which(cmd[0]):
        return cmd
    local = Path(__file__).resolve().parents[2] / "integrations" / "premiere-mcp" / "node_modules" / ".bin" / "premiere-pro-mcp"
    if local.exists():
        return [str(local)]
    return None


class BridgeClient:
    """Synchronous wrapper around an MCP stdio client session to the Premiere bridge."""

    def __init__(self, cmd: list[str], timeout: float = 60):
        self.cmd = cmd
        self.timeout = timeout

    async def _session(self, fn):
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        env = {**os.environ, "PREMIERE_MCP_TELEMETRY": "0"}   # telemetry off by default
        params = StdioServerParameters(command=self.cmd[0], args=self.cmd[1:], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await asyncio.wait_for(s.initialize(), self.timeout)
                return await fn(s)

    def run(self, fn):
        result: dict = {}

        def target():
            try:
                result["v"] = asyncio.run(self._session(fn))
            except BaseException as e:  # noqa: BLE001
                result["e"] = e
        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(self.timeout + 30)
        if "e" in result:
            raise result["e"]
        if "v" not in result:
            raise TimeoutError("Premiere bridge did not answer")
        return result["v"]

    def list_tools(self) -> list[str]:
        async def f(s):
            return [t.name for t in (await s.list_tools()).tools]
        return self.run(f)

    def call(self, tool: str, args: dict | None = None, via_catalog: bool = True) -> dict:
        async def f(s):
            names = {t.name for t in (await s.list_tools()).tools}
            if tool in names or not via_catalog:
                res = await asyncio.wait_for(s.call_tool(tool, args or {}), self.timeout)
            else:
                res = await asyncio.wait_for(s.call_tool("invoke_tool", {"name": tool, "arguments": args or {}}),
                                             self.timeout)
            text = " ".join(getattr(c, "text", "") for c in res.content)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {"text": text}
            return {"is_error": bool(getattr(res, "is_error", False) or getattr(res, "isError", False)), "data": data}
        return self.run(f)


def schema_contract(ws) -> dict:
    """Read each mapped tool's live schema from the bridge and check our argument names still exist."""
    cmd = bridge_command(ws)
    if not cmd:
        return {"ok": False, "reason": "bridge not installed"}
    cli = BridgeClient(cmd)
    out, ok = {}, True
    for op, (tool, required) in OPS.items():
        try:
            r = cli.call("get_tool_schema", {"name": tool}, via_catalog=False)
            schema = r["data"]
            props = json.dumps(schema)
            missing = [a for a in required if f'"{a}"' not in props]
            out[op] = {"tool": tool, "found": not r["is_error"], "missing_args": missing}
            ok &= (not r["is_error"]) and not missing
        except Exception as e:  # noqa: BLE001
            out[op] = {"tool": tool, "found": False, "error": str(e)}
            ok = False
    return {"ok": ok, "ops": out}


def status(ws) -> dict:
    """Independent statuses. Read-only: never launches Premiere (launchIfNeeded=false)."""
    from ..health import premiere as local_premiere
    st = {"bridge": "hetpatel_mcp", "server": "unknown", "host": "unknown", "panel": "unknown", "export_backend": "unknown",
          "local": local_premiere(), "supported_ops": {}, "awaiting_mac_validation": True}
    cmd = bridge_command(ws)
    if not cmd:
        st["server"] = "not_installed"
        st["fix"] = "Install the bridge (`npm install -g adobe-premiere-pro-mcp@1.2.8` then `premiere-pro-mcp --install-cep`) " \
                    "— only if no working bridge already exists on this Mac."
        return st
    cli = BridgeClient(cmd, timeout=45)
    try:
        caps = cli.call("get_capabilities", {"checkConnection": False}, via_catalog=False)
        st["server"] = "ok"
        st["capabilities"] = caps["data"]
    except Exception as e:  # noqa: BLE001
        st["server"] = f"error: {e}"
        return st
    try:
        v = cli.call("verify_premiere_connection", {"launchIfNeeded": False}, via_catalog=False)
        d = v["data"] if isinstance(v["data"], dict) else {}
        txt = json.dumps(d).lower()
        st["connection"] = d
        st["host"] = "connected" if (not v["is_error"] and ("ready" in txt or d.get("ok") or d.get("connected"))) else "not_connected"
        st["panel"] = "running" if "heartbeat" in txt and "missing" not in txt else ("not_running" if "panel" in txt or "heartbeat" in txt else "unknown")
        st["export_backend"] = "unverified (requires a disposable export test on the Mac)"
    except Exception as e:  # noqa: BLE001
        st["host"] = f"error: {e}"
    return st


def plan_operations(ws, plan_row: dict) -> dict:
    """Normalized operations for applying a plan (dry run) — what the job will do, with pre/postconditions."""
    plan = plan_row["plan"]
    name = f"{plan['sequence']['name']} — plan v{plan_row['version']} [{plan_row['id'][-6:]}]"
    ops = [
        {"op": "connection_check", "pre": "bridge server ok", "post": "host connected, panel running"},
        {"op": "save", "pre": "project open", "post": "project saved before mutation"},
        {"op": "list_sequences", "pre": None, "post": f"no sequence named '{name}' exists (else: reuse, no duplicate)"},
        {"op": "import_xml", "args": {"filePath": "<project>/premiere/<plan>.xml"},
         "post": f"exactly one new sequence '{name}'"},
        {"op": "read_tracks", "post": f"V1 has {len(plan['clips'])} clips at plan positions; A1 holds locked narration"},
        {"op": "save", "post": "project saved"},
    ]
    if plan["captions"]["cues"]:
        ops.insert(4, {"op": "note", "detail": "kinetic captions placed on V3 as a transparent overlay render "
                                                "(not an SRT import) — clearly labeled in the sequence"})
    return {"sequence_name": name, "operations": ops, "bridge_tools": {o["op"]: OPS.get(o["op"], ("-",))[0] for o in ops}}
