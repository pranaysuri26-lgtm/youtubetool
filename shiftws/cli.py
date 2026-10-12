"""`shiftws` command line: engine server, health/discovery, chat, jobs, backup/restore, relink, MCP config proposal."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="shiftws", description="5 to 9 Shift workspace engine")
    ap.add_argument("--home", help="workspace data folder (default: platform app-data folder)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the local engine API (loopback) with workers and watchers")
    s.add_argument("--port", type=int, default=8765)
    sub.add_parser("doctor", help="health report with repairs (Markdown)")
    d = sub.add_parser("discover", help="local discovery: tools, skills, Premiere, MCP server names (redacted)")
    d.add_argument("--json", action="store_true")
    sub.add_parser("mcp-config", help="show the exact project-scoped .mcp.json change for registering the workspace MCP")
    c = sub.add_parser("chat", help="send a message to a thread and wait for the reply")
    c.add_argument("thread_id")
    c.add_argument("text")
    c.add_argument("--attach", action="append", default=[])
    c.add_argument("--provider")
    j = sub.add_parser("job", help="job control")
    j.add_argument("action", choices=["list", "resume", "cancel", "run"])
    j.add_argument("job_id", nargs="?")
    sub.add_parser("work", help="run workers in the foreground (no API)")
    b = sub.add_parser("backup")
    b.add_argument("--dest")
    r = sub.add_parser("restore")
    r.add_argument("backup_dir")
    rl = sub.add_parser("relink", help="relink offline media by content hash")
    rl.add_argument("folders", nargs="+")
    t = sub.add_parser("tool", help="call a workspace tool with JSON args")
    t.add_argument("name")
    t.add_argument("args", nargs="?", default="{}")
    t.add_argument("--project")
    a = ap.parse_args(argv)

    if a.cmd == "restore":
        from .config import load_config
        from .projects import restore
        restore(load_config(a.home), a.backup_dir)
        print("restored; start the app again")
        return
    if a.cmd == "discover":
        from .health import discover, report_markdown
        d_ = discover()
        print(json.dumps(d_, indent=2, default=str) if a.json else report_markdown(d_))
        return
    from .workspace import open_workspace
    ws = open_workspace(a.home)
    if a.cmd == "serve":
        from .api import serve
        print(f"engine on http://127.0.0.1:{a.port} (token in {ws.cfg.home / 'api_token'})", file=sys.stderr)
        serve(ws, a.port)
    elif a.cmd == "doctor":
        from .health import doctor, report_markdown
        print(report_markdown(doctor(ws)))
    elif a.cmd == "mcp-config":
        from .health import propose_mcp_registration
        p = propose_mcp_registration(Path.cwd(), sys.executable)
        print(f"Proposed change to {p['file']} (project scope; global config untouched). Not applied.\n")
        print(p["diff"] or "(already registered)")
    elif a.cmd == "chat":
        from .chat.engine import send
        r = send(ws, a.thread_id, a.text, attachments=a.attach, provider=a.provider, wait=True)
        print(r.get("text") or r)
    elif a.cmd == "job":
        from . import jobs as J
        if a.action == "list":
            for x in J.list_jobs(ws, None, None, 30):
                print(f"{x['id']}  {x['kind']:<18} {x['status']:<9} {x['progress']:.0%}  {x['message'][:60]}")
        elif a.action == "resume":
            print(J.resume(ws, a.job_id)["status"])
        elif a.action == "cancel":
            print(J.cancel(ws, a.job_id)["status"])
        elif a.action == "run":
            print(json.dumps(J.run_inline(ws, J.get(ws, a.job_id))["result"], indent=1, default=str)[:4000])
    elif a.cmd == "work":
        import time
        from .jobs import Workers
        Workers(ws).start()
        while True:
            time.sleep(3600)
    elif a.cmd == "backup":
        from .projects import backup
        print(backup(ws, a.dest))
    elif a.cmd == "relink":
        from .assets import check_availability, relink_by_hash
        check_availability(ws)
        print(json.dumps(relink_by_hash(ws, a.folders), indent=1))
    elif a.cmd == "tool":
        from .tools import call
        print(json.dumps(call(ws, a.name, json.loads(a.args), scope={"project_id": a.project, "origin": "cli"}),
                         indent=1, default=str)[:20000])


if __name__ == "__main__":
    main()
