"""Adapter for the vendored yt-* skills (Jakeschincariol/youtube-agent-skill, MIT, pinned commit).

Skills provide instructions + small stdlib Python tools. They don't own state: results are stored by the
workspace. For Claude Code the skills are installed into the workspace's own agent folder
(<SHIFTWS_HOME>/agent/.claude/skills), never into ~/.claude/skills.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "youtube-agent-skill"


def skill_dir(name: str) -> Path:
    d = VENDOR / "skills" / name
    if not d.exists():
        raise FileNotFoundError(f"skill {name} not vendored")
    return d


def run_skill_tool(skill: str, tool: str, args: list[str], timeout: float = 60):
    """Run a skill's Python tool with --json (argument array, isolated interpreter)."""
    script = skill_dir(skill) / tool
    p = subprocess.run([sys.executable, "-I", str(script), *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{skill}/{tool} failed: {(p.stderr or p.stdout)[-500:]}")
    out = p.stdout.strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"text": out}
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        data = data[0]
    if isinstance(data, dict) and "verdict" in data and "total" not in data:
        data["total"] = data["verdict"]
    return data


def install_agent_skills(agent_dir: Path) -> list[str]:
    """Copy the pinned yt-* skills into the workspace agent folder (project scope, not global)."""
    dest = agent_dir / ".claude" / "skills"
    dest.mkdir(parents=True, exist_ok=True)
    installed = []
    for s in sorted((VENDOR / "skills").iterdir()):
        target = dest / s.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(s, target)
        installed.append(s.name)
    return installed
