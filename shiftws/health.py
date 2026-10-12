"""Health report and local discovery (Mac-first, portable).

Rules: read-only by default; never print whole config or credential files; only names, versions and
status. MCP servers are reported by name + command basename; env values and args are never shown.
Proposed config changes are returned as an exact diff and only applied with explicit consent.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

from .util import redact


def _cmd(args: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def _version(binary: str | None, args=("--version",)) -> str | None:
    if not binary:
        return None
    rc, out = _cmd([binary, *args])
    if rc != 0 and not out:
        return None
    return redact(out.splitlines()[0][:120]) if out else None


def hardware() -> dict:
    sysname = platform.system()
    info = {"os": f"{sysname} {platform.release()}", "arch": platform.machine(), "cpu": platform.processor() or None,
            "cpu_count": os.cpu_count(), "ram_gb": None, "gpu": [], "disk_free_gb": None, "external_drives": []}
    if sysname == "Darwin":
        _, brand = _cmd(["sysctl", "-n", "machdep.cpu.brand_string"])
        info["cpu"] = brand or info["cpu"]
        _, mem = _cmd(["sysctl", "-n", "hw.memsize"])
        if mem.isdigit():
            info["ram_gb"] = round(int(mem) / 2**30, 1)
        rc, gpu = _cmd(["system_profiler", "SPDisplaysDataType", "-json"], timeout=40)
        if rc == 0:
            try:
                info["gpu"] = [d.get("sppci_model") for d in json.loads(gpu).get("SPDisplaysDataType", [])]
            except json.JSONDecodeError:
                pass
        vols = Path("/Volumes")
        if vols.exists():
            info["external_drives"] = [v.name for v in vols.iterdir() if v.name not in ("Macintosh HD",)]
        info["apple_silicon"] = platform.machine() == "arm64"
    elif sysname == "Linux":
        try:
            for line in open("/proc/meminfo"):
                if line.startswith("MemTotal"):
                    info["ram_gb"] = round(int(line.split()[1]) / 2**20, 1)
            for line in open("/proc/cpuinfo"):
                if line.startswith("model name"):
                    info["cpu"] = line.split(":", 1)[1].strip(); break
        except OSError:
            pass
        if shutil.which("nvidia-smi"):
            _, g = _cmd(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
            info["gpu"] = g.splitlines()
    elif sysname == "Windows":
        info["external_drives"] = [f"{d}:" for d in "DEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{d}:\\")]
    try:
        info["disk_free_gb"] = round(shutil.disk_usage(Path.home()).free / 2**30, 1)
    except OSError:
        pass
    return info


def tools() -> dict:
    out = {}
    for name, args in [("git", ("--version",)), ("node", ("--version",)), ("npm", ("--version",)),
                       ("python3", ("--version",)), ("ffmpeg", ("-version",)), ("ffprobe", ("-version",)),
                       ("yt-dlp", ("--version",)), ("deno", ("--version",)), ("whisper-cli", ("--help",)),
                       ("ollama", ("--version",)), ("claude", ("--version",)), ("brew", ("--version",))]:
        b = shutil.which(name)
        v = _version(b, args) if name != "whisper-cli" else ("installed" if b else None)
        out[name] = {"path": b, "version": v, "ok": bool(b),
                     "optional": name in ("yt-dlp", "deno", "whisper-cli", "ollama", "brew")}
    from importlib import metadata
    for name, dist in (("yt-dlp (workspace venv)", "yt-dlp"), ("claude-real-video (workspace venv)", "claude-real-video"),
                       ("sherpa-onnx (workspace venv)", "sherpa-onnx")):
        try:
            out[name] = {"path": None, "version": metadata.version(dist), "ok": True}
        except metadata.PackageNotFoundError:
            pass
    return out


def claude_code() -> dict:
    b = shutil.which("claude")
    if not b:
        return {"installed": False}
    info = {"installed": True, "version": _version(b)}
    rc, out = _cmd([b, "auth", "status"])
    try:
        st = json.loads(out[out.find("{"):]) if "{" in out else {}
        # Only the login state and method — never tokens, emails or org ids.
        info["logged_in"] = bool(st.get("loggedIn"))
        info["auth_method"] = st.get("authMethod")
    except json.JSONDecodeError:
        info["logged_in"] = None
    return info


def claude_skills(project_dir: str | Path | None = None) -> dict:
    res = {}
    for label, base in [("user", Path.home() / ".claude" / "skills"),
                        ("project", Path(project_dir or Path.cwd()) / ".claude" / "skills")]:
        res[label] = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.exists() else []
    plug = Path.home() / ".claude" / "plugins"
    res["plugins"] = sorted({p.parent.name for p in plug.rglob("plugin.json")})[:50] if plug.exists() else []
    res["yt_skills_present"] = sorted(s for s in res["user"] + res["project"] if s.startswith("yt-"))
    return res


def mcp_servers() -> dict:
    """Names + command basename + status. No env, no args, no file dumps."""
    found = {}
    for label, path in [("user", Path.home() / ".claude.json"), ("project", Path.cwd() / ".mcp.json"),
                        ("claude_desktop", Path.home() / "Library/Application Support/Claude/claude_desktop_config.json")]:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        servers = dict(data.get("mcpServers", {}) or {})
        for proj in (data.get("projects") or {}).values():   # per-project entries in ~/.claude.json
            servers.update(proj.get("mcpServers", {}) or {})
        for name, spec in servers.items():
            cmd = spec.get("command") or spec.get("url") or spec.get("type") or ""
            found[f"{label}:{name}"] = {"name": name, "source": label,
                                        "command": Path(str(cmd)).name if "://" not in str(cmd) else "remote",
                                        "premiere_candidate": bool(re.search(r"premiere|adobe", name + str(cmd), re.I))}
    status = {}
    if shutil.which("claude"):
        rc, out = _cmd(["claude", "mcp", "list"], timeout=60)
        for line in out.splitlines():
            m = re.match(r"^([\w.\-@/]+):\s.*?(✓ Connected|✗ Failed.*|Needs auth.*|Connected|Failed.*)$", line.strip())
            if m:
                status[m.group(1)] = m.group(2)
    for v in found.values():
        v["status"] = status.get(v["name"], "unknown")
    return {"servers": list(found.values()), "listed_by_cli": sorted(status)}


def premiere() -> dict:
    info = {"installed": False, "versions": [], "cep_extensions": [], "bridge_candidates": []}
    sysname = platform.system()
    if sysname == "Darwin":
        for app in sorted(Path("/Applications").glob("Adobe Premiere Pro*")):
            plist = next(app.glob("*.app/Contents/Info.plist"), None)
            ver = None
            if plist:
                rc, v = _cmd(["/usr/bin/defaults", "read", str(plist.with_suffix("")), "CFBundleShortVersionString"])
                ver = v if rc == 0 else None
            info["versions"].append({"app": app.name, "version": ver})
        cep_dirs = [Path.home() / "Library/Application Support/Adobe/CEP/extensions",
                    Path("/Library/Application Support/Adobe/CEP/extensions")]
    elif sysname == "Windows":
        pf = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Adobe"
        for app in sorted(pf.glob("Adobe Premiere Pro*")):
            info["versions"].append({"app": app.name, "version": None})
        cep_dirs = [Path(os.environ.get("APPDATA", "")) / "Adobe/CEP/extensions"]
    else:
        cep_dirs = []
    info["installed"] = bool(info["versions"])
    for d in cep_dirs:
        if d.exists():
            info["cep_extensions"] += sorted(p.name for p in d.iterdir())
    info["bridge_candidates"] = [e for e in info["cep_extensions"] if re.search(r"mcp|bridge|premiere", e, re.I)]
    info["hetpatel_cli"] = shutil.which("premiere-pro-mcp")
    return info


def discover(project_dir=None) -> dict:
    return {"hardware": hardware(), "tools": tools(), "claude_code": claude_code(),
            "claude_skills": claude_skills(project_dir), "mcp": mcp_servers(), "premiere": premiere()}


def doctor(ws) -> dict:
    """One health report with concrete repairs."""
    d = discover()
    issues = []
    t = d["tools"]
    for need, fix in [("ffmpeg", "brew install ffmpeg"), ("ffprobe", "brew install ffmpeg"), ("git", "xcode-select --install"),
                      ("node", "brew install node@22")]:
        if not t[need]["ok"]:
            issues.append({"severity": "error", "item": need, "repair": fix})
    if not (t.get("yt-dlp", {}).get("ok") or t.get("yt-dlp (workspace venv)", {}).get("ok")):
        issues.append({"severity": "error", "item": "yt-dlp", "repair": "scripts/setup.sh installs it into the workspace venv"})
    cc = d["claude_code"]
    if not cc.get("installed"):
        issues.append({"severity": "warn", "item": "Claude Code", "repair": "Install Claude Code; the chat uses it as the AI."})
    elif cc.get("logged_in") is False:
        issues.append({"severity": "warn", "item": "Claude Code login", "repair": "Run `claude` once and log in (Anthropic's own flow)."})
    try:
        from .transcribe import get_backend
        b = get_backend(ws.cfg)
        d["transcription"] = {"backend": b.name, "model": b.model, "ok": True}
    except Exception as e:  # noqa: BLE001
        d["transcription"] = {"ok": False, "error": str(e)}
        issues.append({"severity": "warn", "item": "transcription", "repair": getattr(e, "hint", "shiftws setup --models")})
    free = d["hardware"].get("disk_free_gb")
    if free is not None and free < 20:
        issues.append({"severity": "warn", "item": "disk space", "repair": f"Only {free} GB free; clear caches (`shiftws cache prune`)."})
    from .assets import check_availability
    offline = check_availability(ws)
    if offline:
        issues.append({"severity": "warn", "item": f"{len(offline)} offline media file(s)",
                       "repair": "Reconnect the drive, or `shiftws relink <folder>` to relink by hash."})
    pr = d["premiere"]
    if platform.system() == "Darwin" and not pr["installed"]:
        issues.append({"severity": "info", "item": "Premiere Pro", "repair": "Not found in /Applications."})
    stuck = ws.db.all("SELECT id, kind FROM jobs WHERE status='failed' ORDER BY created_at DESC LIMIT 5")
    for j in stuck:
        issues.append({"severity": "info", "item": f"failed job {j['kind']}", "repair": f"shiftws job resume {j['id']}"})
    d["issues"] = issues
    d["workspace"] = {"home": str(ws.cfg.home), "projects_root": str(ws.cfg.projects_root),
                      "db": str(ws.cfg.db_path), "cache_gb": _du_gb(ws.cfg.cache_dir)}
    return d


def _du_gb(p) -> float:
    total = 0
    for r, _, fs in os.walk(p):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return round(total / 2**30, 2)


def propose_mcp_registration(workspace_dir: str | Path, python: str) -> dict:
    """Project-scoped registration of the workspace MCP server. Returns the exact change; never writes it here."""
    target = Path(workspace_dir) / ".mcp.json"
    current = json.loads(target.read_text()) if target.exists() else {}
    new = json.loads(json.dumps(current)) if current else {}
    new.setdefault("mcpServers", {})["shiftws"] = {"command": python, "args": ["-m", "shiftws.mcp_server"]}
    import difflib
    diff = "\n".join(difflib.unified_diff(json.dumps(current, indent=2).splitlines(),
                                          json.dumps(new, indent=2).splitlines(), str(target), str(target), lineterm=""))
    return {"file": str(target), "diff": diff, "new": new, "changes_global_config": False}


def report_markdown(d: dict) -> str:
    L = ["# 5 to 9 Shift — health report", ""]
    h = d["hardware"]
    L += ["## Machine", f"- {h['os']} · {h['arch']} · {h.get('cpu')} · {h.get('cpu_count')} cores · RAM {h.get('ram_gb')} GB",
          f"- GPU: {', '.join(g for g in h.get('gpu') or [] if g) or 'not detected'} · free disk {h.get('disk_free_gb')} GB",
          f"- External drives: {', '.join(h.get('external_drives') or []) or 'none'}", "", "## Tools"]
    for k, v in d["tools"].items():
        mark = "✅" if v["ok"] else ("–" if v.get("optional") else "❌")
        L.append(f"- {mark} {k}: {v.get('version') or ('not installed (optional)' if v.get('optional') else 'missing')}")
    cc = d["claude_code"]
    L += ["", "## Claude Code", f"- installed: {cc.get('installed')} · version: {cc.get('version')} · logged in: {cc.get('logged_in')}"]
    s = d["claude_skills"]
    L += [f"- skills (user): {', '.join(s['user']) or 'none'}", f"- skills (project): {', '.join(s['project']) or 'none'}",
          "", "## MCP servers (names only)"]
    for m in d["mcp"]["servers"]:
        L.append(f"- {m['name']} ({m['source']}, {m['command']}) — {m['status']}" + (" ← Premiere candidate" if m["premiere_candidate"] else ""))
    if not d["mcp"]["servers"]:
        L.append("- none found")
    p = d["premiere"]
    L += ["", "## Premiere", f"- installed: {p['installed']} {p['versions']}", f"- CEP extensions: {', '.join(p['cep_extensions']) or 'none'}",
          f"- premiere-pro-mcp CLI: {p.get('hetpatel_cli') or 'not installed'}"]
    if "transcription" in d:
        L += ["", "## Transcription", f"- {d['transcription']}"]
    if "issues" in d:
        L += ["", "## Issues and repairs"] + [f"- [{i['severity']}] {i['item']}: {i['repair']}" for i in d["issues"]]
    return redact("\n".join(L)) + "\n"
