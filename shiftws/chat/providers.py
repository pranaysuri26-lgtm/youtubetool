"""AI providers. Provider handling is separate from memory and tools: every provider reaches the workspace only
through the same tool layer, and memory lives in the database, not in a provider's chat history.

- ClaudeCodeProvider: runs the user's installed, unmodified `claude` CLI in documented non-interactive mode
  (`claude -p --output-format stream-json`), scoped to the workspace MCP server. Auth is entirely the CLI's own
  (the user's login via Anthropic's flow). This app never reads, copies or stores Claude credentials.
- OllamaProvider: optional local model via http://127.0.0.1:11434 with tool calling (off by default).
- OfflineProvider: deterministic commands so the workspace stays usable without any AI. Clearly labeled.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from .. import tools as T
from .context import build_context

# Variables that would attach a child CLI run to a parent Claude Code session (when the app itself is launched
# from inside Claude Code). Removing them only changes session identity; credentials are not touched or read.
_SESSION_VARS = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_TEE_SDK_STDOUT", "CLAUDE_CODE_MESSAGING_SOCKET",
                 "CLAUDE_CODE_MESSAGING_TOKEN")

ALLOWED = ["mcp__shiftws", "Read", "Glob", "Grep"]
DENIED = ["Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent"]


class TurnEvents:
    """Callbacks a provider reports through while a turn runs."""

    def __init__(self, on_text: Callable[[str], None], on_tool: Callable[[str, dict], None],
                 on_tool_result: Callable[[str, str], None], on_status: Callable[[str], None]):
        self.text, self.tool, self.tool_result, self.status = on_text, on_tool, on_tool_result, on_status


class Provider:
    name = "base"

    def available(self, ws) -> dict:
        raise NotImplementedError

    def run_turn(self, ws, thread: dict, text: str, ev: TurnEvents, cancel: threading.Event) -> dict:
        raise NotImplementedError


class ClaudeCodeProvider(Provider):
    name = "claude_code"

    def binary(self, ws) -> str | None:
        b = ws.cfg.get("ai.claude_bin", "claude")
        return shutil.which(b) or (b if Path(b).exists() else None)

    def available(self, ws) -> dict:
        b = self.binary(ws)
        if not b:
            return {"ok": False, "reason": "Claude Code CLI not found on PATH", "fix": "Install Claude Code, then log in."}
        try:
            p = subprocess.run([b, "auth", "status"], capture_output=True, text=True, timeout=20, env=self._env())
            st = json.loads(p.stdout[p.stdout.find("{"):]) if "{" in p.stdout else {}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            st = {}
        logged = bool(st.get("loggedIn"))
        v = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        return {"ok": logged, "version": v, "auth_method": st.get("authMethod"),
                "reason": None if logged else "Claude Code is not logged in", "fix": None if logged else "Run `claude` and log in."}

    def _env(self, extra: dict | None = None) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in _SESSION_VARS}
        env.update(extra or {})
        return env

    def agent_dir(self, ws) -> Path:
        """The workspace's own agent folder: project-scoped yt-* skills + CLAUDE.md. Global config is untouched."""
        d = Path(ws.cfg.home) / "agent"
        if not (d / ".claude" / "skills").exists():
            from ..skills import install_agent_skills
            install_agent_skills(d)
        md = d / "CLAUDE.md"
        if not md.exists():
            md.write_text("# 5 to 9 Shift agent folder\n\nThis folder is managed by the workspace. The project state "
                          "lives in the workspace database; use the shiftws MCP tools.\nThe yt-* skills here read the "
                          f"voice profile at {Path(ws.cfg.home) / 'voice.md'} (not ~/.claude/youtube/voice.md).\n")
        return d

    def run_turn(self, ws, thread, text, ev, cancel):
        b = self.binary(ws)
        if not b:
            raise RuntimeError("Claude Code CLI not found")
        agent = self.agent_dir(ws)
        mcp_cfg = {"mcpServers": {"shiftws": {"command": sys.executable, "args": ["-m", "shiftws.mcp_server"],
                                              "env": {"SHIFTWS_HOME": str(ws.cfg.home),
                                                      "SHIFTWS_PROJECT_ID": thread["project_id"] or "",
                                                      "SHIFTWS_THREAD_ID": thread["id"], "SHIFTWS_ORIGIN": "chat",
                                                      "PYTHONPATH": str(Path(__file__).resolve().parents[2])}}}}
        fd, cfg_path = tempfile.mkstemp(prefix="shiftws-mcp-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(mcp_cfg, f)
        resume = thread.get("provider_session_id") if thread.get("provider") == self.name else None
        ctx = build_context(ws, thread["id"], include_history=not resume)
        add_dirs = [str(ws.cfg.cache_dir)]
        if thread["project_id"]:
            from ..projects import get_project
            add_dirs.insert(0, get_project(ws, thread["project_id"])["root_dir"])
        cmd = [b, "-p", text, "--output-format", "stream-json", "--verbose", "--mcp-config", cfg_path,
               "--strict-mcp-config", "--allowedTools", *ALLOWED, "--disallowedTools", *DENIED,
               "--append-system-prompt", ctx, "--add-dir", *add_dirs]
        if ws.cfg.get("ai.claude_model"):
            cmd += ["--model", ws.cfg.get("ai.claude_model")]
        try:
            res = self._stream(cmd + (["--resume", resume] if resume else []), agent, ev, cancel)
            if resume and res.get("resume_failed"):
                ev.status("previous Claude session unavailable — continuing from workspace memory")
                ctx = build_context(ws, thread["id"], include_history=True)
                cmd[cmd.index("--append-system-prompt") + 1] = ctx
                res = self._stream(cmd, agent, ev, cancel)
            return res
        finally:
            os.unlink(cfg_path)

    def _stream(self, cmd, cwd, ev: TurnEvents, cancel) -> dict:
        proc = subprocess.Popen(cmd, cwd=cwd, env=self._env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
        out = {"session_id": None, "text": "", "is_error": False, "cost_usd": None, "turns": None}
        texts: list[str] = []

        def killer():
            while proc.poll() is None:
                if cancel.wait(0.3):
                    try:
                        os.killpg(proc.pid, 15)
                    except OSError:
                        pass
                    return
        threading.Thread(target=killer, daemon=True).start()
        assert proc.stdout
        for line in proc.stdout:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = e.get("type")
            if e.get("session_id"):
                out["session_id"] = e["session_id"]
            if t == "assistant":
                for c in e.get("message", {}).get("content", []):
                    if c.get("type") == "text" and c.get("text"):
                        texts.append(c["text"])
                        ev.text(c["text"])
                    elif c.get("type") == "tool_use":
                        ev.tool(c.get("name", ""), c.get("input", {}))
            elif t == "user":
                for c in e.get("message", {}).get("content", []) if isinstance(e.get("message", {}).get("content"), list) else []:
                    if c.get("type") == "tool_result":
                        content = c.get("content")
                        if isinstance(content, list):
                            content = " ".join(x.get("text", "") for x in content if isinstance(x, dict))
                        ev.tool_result(c.get("tool_use_id", ""), str(content)[:4000])
            elif t == "result":
                out.update({"is_error": bool(e.get("is_error")), "cost_usd": e.get("total_cost_usd"),
                            "turns": e.get("num_turns"), "final": e.get("result")})
        proc.wait()
        err = proc.stderr.read() if proc.stderr else ""
        if cancel.is_set():
            out["cancelled"] = True
        if proc.returncode != 0 and not texts:
            if "No conversation found" in err or "session" in err.lower() and "not found" in err.lower():
                out["resume_failed"] = True
            else:
                out["is_error"] = True
                out["error"] = err.strip()[-800:] or f"claude exited with {proc.returncode}"
        out["text"] = "\n\n".join(texts) or (out.get("final") or "")
        return out


class OllamaProvider(Provider):
    """Optional local model. Tool calling through Ollama's /api/chat 'tools' field. Off by default.
    Quality, tool-use reliability and vision support depend on the model and hardware — measure before relying."""
    name = "ollama"

    def available(self, ws) -> dict:
        if not ws.cfg.get("ai.ollama_enabled"):
            return {"ok": False, "reason": "Ollama provider is off (optional)", "fix": "Enable it in Connections."}
        import urllib.request
        try:
            with urllib.request.urlopen(ws.cfg.get("ai.ollama_url") + "/api/tags", timeout=3) as r:
                models = [m["name"] for m in json.loads(r.read())["models"]]
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": f"Ollama not reachable: {e}", "fix": "Start Ollama (`ollama serve`)."}
        m = ws.cfg.get("ai.ollama_model")
        return {"ok": bool(m and m in models), "models": models,
                "reason": None if m in models else "configured model not pulled"}

    def run_turn(self, ws, thread, text, ev, cancel):
        import urllib.request
        schemas = tool_schemas()
        msgs = [{"role": "system", "content": build_context(ws, thread["id"], include_history=True)},
                {"role": "user", "content": text}]
        scope = {"project_id": thread["project_id"], "thread_id": thread["id"], "origin": "chat"}
        final = ""
        for _ in range(8):
            if cancel.is_set():
                break
            body = json.dumps({"model": ws.cfg.get("ai.ollama_model"), "messages": msgs, "tools": schemas,
                               "stream": False}).encode()
            req = urllib.request.Request(ws.cfg.get("ai.ollama_url") + "/api/chat", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600) as r:
                msg = json.loads(r.read())["message"]
            msgs.append(msg)
            calls = msg.get("tool_calls") or []
            if msg.get("content"):
                final = msg["content"]
                ev.text(final)
            if not calls:
                break
            for c in calls:
                fn = c["function"]["name"]
                args = c["function"].get("arguments") or {}
                ev.tool(fn, args)
                res = T.call(ws, fn, args, scope=scope)
                ev.tool_result(fn, json.dumps(res, default=str)[:4000])
                msgs.append({"role": "tool", "content": json.dumps(res, default=str)[:8000]})
        return {"text": final, "session_id": None}


def tool_schemas() -> list[dict]:
    """JSON schemas for all workspace tools (same definitions the MCP server publishes)."""
    import inspect
    import typing
    out = []
    for fn in T.TOOLS:
        hints = typing.get_type_hints(fn)
        props, req = {}, []
        for p in list(inspect.signature(fn).parameters.values())[1:]:
            h = hints.get(p.name)
            typ = "string"
            s = str(h)
            if "int" in s and "str" not in s:
                typ = "integer"
            elif "float" in s:
                typ = "number"
            elif "bool" in s:
                typ = "boolean"
            elif "list" in s:
                typ = "array"
            elif "dict" in s:
                typ = "object"
            props[p.name] = {"type": typ}
            if p.default is inspect.Parameter.empty:
                req.append(p.name)
        out.append({"type": "function", "function": {"name": fn.__name__, "description": (fn.__doc__ or "").strip()[:600],
                                                     "parameters": {"type": "object", "properties": props, "required": req}}})
    return out


class OfflineProvider(Provider):
    """No AI. Deterministic commands that run the same jobs. Writing scripts needs an AI provider (or paste one)."""
    name = "offline"
    HELP = ("**Offline mode (no AI connected).** Commands run real jobs:\n"
            "- `download <url> [from-to]` · `import <path>` · `analyze` · `analyze <asset_id>`\n"
            "- `find <words>` (footage) · `search <words>` (everything) · `sfx <description>`\n"
            "- `approve <asset_id> [purpose]` · `reject <asset_id> [reason]` · `lock` (latest narration take)\n"
            "- `script` + pasted text (each paragraph = one section; keep [tags] inline) · `status`\n"
            "- `new project <title>` · `plan` · `preview` · `qc` · `package`")

    def available(self, ws) -> dict:
        return {"ok": True, "note": "deterministic commands only"}

    def run_turn(self, ws, thread, text, ev, cancel):
        scope = {"project_id": thread["project_id"], "thread_id": thread["id"], "origin": "chat"}
        call = lambda n, **a: T.call(ws, n, a, scope=scope)  # noqa: E731
        s = text.split("\n\n[Retrieved for this turn")[0].split("\n\n[Workspace already did")[0].strip()
        low = s.lower()
        reply = None
        m = re.match(r"(?:download|get)\s+(https?://\S+)(?:\s+(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?))?", s, re.I)
        if m:
            r = call("assets_download", url=m.group(1), start=float(m.group(2)) if m.group(2) else None,
                     end=float(m.group(3)) if m.group(3) else None)
            reply = f"Download job {r['job_ids']} queued." if r["status"] != "error" else f"Error: {r['warnings']}"
        elif low.startswith("import "):
            r = call("assets_import", paths=[s[7:].strip()])
            reply = f"Import job {r['job_ids']} queued."
        elif low.startswith("analyze"):
            arg = s[7:].strip()
            ids = [arg] if arg else [a["id"] for a in call("assets_list")["data"] if a["kind"] == "video"]
            jobs = [call("video_analyze", asset_id=i)["job_ids"] for i in ids]
            reply = f"Analysis queued for {len(ids)} file(s): {jobs}"
        elif low.startswith("find "):
            r = call("footage_find", query=s[5:])
            reply = "\n".join(f"- {h['asset_id']} @ {h['start']}s — {h['basis']}: {h['evidence']}" for h in r["data"]) or "No matches."
        elif low.startswith("search "):
            r = call("search", query=s[7:], scope="all")
            reply = "\n".join(f"- [{h['kind']}] {h['title']} ({h['project_title'] or 'channel'}): {h['snippet']}" for h in r["data"]) or "No matches."
        elif low.startswith(("sfx ", "music ")):
            r = call("audio_find", request=s.split(" ", 1)[1])
            reply = "\n".join(f"- {h['asset_id']} {h['title']} score {h['score']} ({h['license']})" for h in r["data"]) or "No matches."
        elif low.startswith(("approve ", "reject ")):
            parts = s.split(" ", 2)
            r = call("audio_decide", asset_id=parts[1], status="approved" if low.startswith("approve") else "rejected",
                     purpose=parts[2] if len(parts) > 2 else "")
            reply = f"Saved: {r['data']['status'] if r['data'] else r['warnings']}"
        elif low.startswith("new project "):
            r = call("project_create", title=s[12:].strip())
            reply = f"Created project {r['data']['title']} ({r['data']['project_id']})."
        elif low.startswith("script"):
            body = s[6:].strip()
            if not body:
                reply = "Writing a new script needs an AI provider. Paste your script after `script` to save it."
            else:
                secs = [{"tagged": p.strip()} for p in re.split(r"\n\s*\n", body) if p.strip()]
                r = call("script_save", sections=secs, change_note="pasted in offline mode")
                d = r["data"] or {}
                reply = (f"Saved script v{d.get('version')} ({d.get('word_count')} words). Warnings: {r['warnings']}\n\n"
                         f"**Copy for ElevenLabs:**\n\n{d.get('copy_for_elevenlabs', '')}")
        elif low in ("lock", "lock narration"):
            takes = call("narration_status")["data"] or []
            if takes:
                r = call("narration_lock", take_id=takes[-1]["id"])
                reply = f"Locked take {takes[-1]['take_no']}: {r['data']}"
            else:
                reply = "No narration take yet."
        elif low in ("plan", "preview", "qc", "package"):
            name = {"plan": "edit_plan_create", "preview": "preview_render", "qc": "qc_run", "package": "upload_package_create"}[low]
            r = call(name)
            reply = f"{name}: {r['status']} {r['job_ids'] or ''} {r['warnings'] or ''}"
        elif low in ("status", "where are we", "where did we stop") and thread["project_id"]:
            w = call("project_context")["data"]["where_we_stopped"]
            reply = f"{w['summary']}\n\n**Next:** {w['next_action']}"
        if reply is None:
            reply = self.HELP
        ev.text(reply)
        return {"text": reply, "session_id": None}


PROVIDERS = {"claude_code": ClaudeCodeProvider, "ollama": OllamaProvider, "offline": OfflineProvider}


def get_provider(ws, name: str | None = None) -> Provider:
    return PROVIDERS[name or ws.cfg.get("ai.provider", "claude_code")]()
