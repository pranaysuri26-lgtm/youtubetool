"""Adapter for the vendored yt-* skills (Jakeschincariol/youtube-agent-skill, MIT, pinned commit).

Skills provide instructions + small stdlib Python tools. They don't own state: results are stored by the
workspace. For Claude Code the skills are installed into the workspace's own agent folder
(<SHIFTWS_HOME>/agent/.claude/skills), never into ~/.claude/skills.
"""
from __future__ import annotations

import json
import re
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


# Real utilities shipped by the skills (all stdlib, all support --json). Inputs are files; project data can be
# supplied by reference (asset transcript / latest retention CSV / final captions) instead of hand-made files.
SKILL_TOOLS = {
    ("yt-script", "hookscore.py"): {"input": "hooks", "args": ["--json"]},
    ("yt-package", "title.py"): {"input": "title", "args": ["--json"]},
    ("yt-chapters", "chapters.py"): {"input": "srt", "args": ["--json"]},
    ("yt-edit", "deadair.py"): {"input": "srt", "args": ["--json"]},
    ("yt-retention", "retention.py"): {"input": "csv", "args": ["--json"]},
    ("yt-viral", "swipe.py"): {"input": "json", "args": ["--json"]},
}


def list_skills() -> list[dict]:
    out = []
    for d in sorted((VENDOR / "skills").iterdir()):
        md = (d / "SKILL.md").read_text()
        desc = re.search(r"description:\s*>-?\s*\n((?:\s{2,}.*\n)+)", md)
        out.append({"skill": d.name, "description": " ".join((desc.group(1) if desc else "").split())[:400],
                    "tools": [t for (s, t) in SKILL_TOOLS if s == d.name],
                    "instructions_only": not any(s == d.name for (s, _) in SKILL_TOOLS)})
    return out


def read_skill(skill: str) -> str:
    return (skill_dir(skill) / "SKILL.md").read_text()


def run_and_save(ws, project_id: str, skill: str, tool: str, *, text: str | None = None, data=None,
                 asset_id: str | None = None, extra_args: list[str] | None = None, thread_id: str | None = None,
                 deliverable_id: str | None = None) -> dict:
    """Run a skill utility on project data and save the result as a versioned artifact linked to the chat."""
    from .projects import get_project
    from .util import new_id, now
    spec = SKILL_TOOLS.get((skill, tool))
    if not spec:
        raise ValueError(f"{skill}/{tool} is not a runnable skill utility; available: "
                         f"{[f'{s}/{t}' for s, t in SKILL_TOOLS]}")
    root = Path(get_project(ws, project_id)["root_dir"]) / "research" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    rev = len(list(root.glob(f"{skill}_{tool[:-3]}_r*.json"))) + 1
    inp, args = None, list(spec["args"]) + list(extra_args or [])
    kind = spec["input"]
    if kind == "hooks":
        lines = data if isinstance(data, list) else [l for l in (text or "").splitlines() if l.strip()]
        inp = root / f"{skill}_{tool[:-3]}_r{rev:02d}_input.txt"
        inp.write_text("\n".join(lines) + "\n")
    elif kind == "title":
        args = ["--title", text or ""] + args
    elif kind == "srt":
        inp = _srt_for(ws, project_id, asset_id, deliverable_id, root / f"{skill}_{tool[:-3]}_r{rev:02d}_input.srt", text)
    elif kind == "csv":
        if text:
            inp = Path(text).expanduser()
        else:
            r = ws.db.one("SELECT csv_path FROM performance_imports WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                          (project_id,))
            if not r:
                raise ValueError("no Studio CSV imported for this project yet")
            inp = Path(r["csv_path"])
    elif kind == "json":
        inp = root / f"{skill}_{tool[:-3]}_r{rev:02d}_input.json"
        inp.write_text(json.dumps(data if data is not None else json.loads(text or "[]"), indent=1))
    cmd_args = ([str(inp)] if inp else []) + args
    result = run_skill_tool(skill, tool, cmd_args)
    out = root / f"{skill}_{tool[:-3]}_r{rev:02d}.json"
    out.write_text(json.dumps({"skill": skill, "tool": tool, "revision": rev, "input": str(inp) if inp else text,
                               "deliverable_id": deliverable_id, "created_at": now(), "result": result}, indent=1))
    aid = new_id("art")
    ws.db.insert("artifacts", {"id": aid, "project_id": project_id, "kind": "skill_output",
                               "cache_key": f"{skill}/{tool}:{rev}", "path": str(out),
                               "depends_json": [f"asset:{asset_id}"] if asset_id else [], "created_at": now()})
    ws.index("artifact", aid, f"{skill} {tool} r{rev}", json.dumps(result)[:4000], project_id=project_id,
             thread_id=thread_id, locator={"artifact_id": aid, "path": str(out), "thread_id": thread_id})
    if thread_id:
        from .chat.threads import add_message
        add_message(ws, thread_id, "system", f"{skill} · {tool} result saved (r{rev}).",
                    meta={"artifact": str(out), "skill": skill})
    ws.automation(project_id, f"skill:{skill}", "automated", tool)
    note = None
    if skill == "yt-edit":
        note = ("Edit suggestions only — nothing was cut. Convert accepted items into edit_plan_revise operations; "
                "a locked narration take is never trimmed.")
    return {"artifact_id": aid, "path": str(out), "revision": rev, "result": result, "note": note}


def _srt_for(ws, project_id, asset_id, deliverable_id, dest: Path, text: str | None) -> Path:
    """Transcript source for SRT-based tools: an explicit path, an asset's local transcript, or the current
    plan's final-timeline captions (so chapters use FINAL sequence timing, not raw source timestamps)."""
    if text and Path(text).expanduser().exists():
        return Path(text).expanduser()
    from .transcribe import to_srt
    if asset_id:
        segs = ws.db.all("SELECT start, end, text FROM transcript_segments WHERE asset_id=? ORDER BY start", (asset_id,))
        if not segs:
            raise ValueError("that asset has no transcript yet (analyze it first)")
        dest.write_text(to_srt(segs))
        return dest
    from .editplan import get_plan
    from .packaging import captions_srt
    plan = get_plan(ws, project_id, deliverable_id=deliverable_id)["plan"]
    if not plan["captions"]["cues"]:
        raise ValueError("the current plan has no caption timing; pass asset_id for a source transcript")
    dest.write_text(captions_srt(plan))
    return dest
