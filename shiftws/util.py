"""Shared helpers: ids, time, hashing, and the only way this app runs subprocesses (argument arrays)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:60] or "project"


def sha256_file(path: str | Path, chunk: int = 4 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def hash_obj(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:24]


def which(name: str) -> str | None:
    return shutil.which(name)


_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|cookie|authorization|bearer)([\"'\s:=]+)([^\s\"',}]+)"),
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
]


def redact(text: str) -> str:
    out = text
    out = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out)
    out = _SECRET_PATTERNS[1].sub("[REDACTED]", out)
    return out


class Cancelled(Exception):
    pass


class ToolError(Exception):
    def __init__(self, message: str, hint: str = "", output: str = ""):
        super().__init__(message)
        self.hint = hint
        self.output = output


def run(cmd: Sequence[str], *, cwd: str | Path | None = None, timeout: float | None = None,
        cancel: Callable[[], bool] | None = None, on_line: Callable[[str], None] | None = None,
        env: dict | None = None, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run an argument array (never a shell string). Streams stderr+stdout lines to on_line,
    polls cancel() and kills the process group when cancelled."""
    cmd = [str(c) for c in cmd]
    full_env = {**os.environ, **(env or {})}
    proc = subprocess.Popen(cmd, cwd=cwd, env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, errors="replace", bufsize=1,
                            start_new_session=True)
    lines: list[str] = []

    def pump():
        assert proc.stdout
        for line in proc.stdout:
            line = line.rstrip("\n")
            if capture:
                lines.append(line)
                if len(lines) > 4000:
                    del lines[:1000]
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    start = time.monotonic()
    while proc.poll() is None:
        if cancel and cancel():
            _kill(proc)
            raise Cancelled(" ".join(cmd[:2]))
        if timeout and time.monotonic() - start > timeout:
            _kill(proc)
            raise ToolError(f"{Path(cmd[0]).name} timed out after {timeout:.0f}s", output="\n".join(lines[-40:]))
        time.sleep(0.1)
    t.join(timeout=5)
    out = "\n".join(lines)
    if check and proc.returncode != 0:
        raise ToolError(f"{Path(cmd[0]).name} exited with {proc.returncode}", output=redact(out[-4000:]))
    return subprocess.CompletedProcess(cmd, proc.returncode, out, "")


def _kill(proc: subprocess.Popen):
    try:
        os.killpg(proc.pid, 15)
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(proc.pid, 9)
        except Exception:
            pass


def ensure_within(path: str | Path, roots: Sequence[str | Path]) -> Path:
    """Restrict file access to configured folders."""
    p = Path(path).expanduser().resolve()
    for r in roots:
        try:
            p.relative_to(Path(r).expanduser().resolve())
            return p
        except ValueError:
            continue
    raise PermissionError(f"{p} is outside the configured project folders")
