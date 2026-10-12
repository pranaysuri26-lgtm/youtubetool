"""Workspace locations and settings. Portable across macOS / Linux / Windows."""
from __future__ import annotations

import json
import os
import platform
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _default_home() -> Path:
    if env := os.environ.get("SHIFTWS_HOME"):
        return Path(env).expanduser()
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "5to9Shift"
    if system == "Windows":
        return Path(os.environ.get("APPDATA", Path.home())) / "5to9Shift"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "shiftws"


DEFAULT_SETTINGS = {
    # Where new project folders go. Large originals can stay where they are (referenced, not copied).
    "projects_root": str(Path.home() / "5to9Shift" / "Projects"),
    "watch_folders": [],                 # e.g. ["~/Downloads"] for ElevenLabs narration intake
    "audio_libraries": [],               # folders indexed into the music/SFX catalog
    "transcription": {
        "backend": "auto",               # auto | whisper_cpp | sherpa_onnx | faster_whisper
        "whisper_cpp_bin": "",           # reuse an existing install, e.g. /opt/homebrew/bin/whisper-cli
        "whisper_cpp_model": "",         # e.g. ~/models/ggml-base.en.bin
        "sherpa_model_dir": "",          # e.g. <home>/models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8
        "threads": 4,
    },
    "ai": {
        "provider": "claude_code",       # claude_code | ollama | offline
        "claude_bin": "claude",
        "claude_model": "",              # empty = the CLI's own default
        "ollama_url": "http://127.0.0.1:11434",
        "ollama_model": "",
        "ollama_enabled": False,
    },
    "premiere": {
        "bridge": "auto",                # auto | hetpatel_mcp | none
        "mcp_command": ["premiere-pro-mcp"],
        "telemetry": False,
    },
    "cache_limit_gb": 50,
    "media_parallelism": 2,
}


@dataclass
class Config:
    home: Path
    settings: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.home / "workspace.db"

    @property
    def models_dir(self) -> Path:
        return self.home / "models"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def projects_root(self) -> Path:
        return Path(self.settings["projects_root"]).expanduser()

    def get(self, dotted: str, default=None):
        cur = self.settings
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def save(self) -> None:
        (self.home / "settings.json").write_text(json.dumps(self.settings, indent=2))

    def api_token(self) -> str:
        """Per-install secret for the loopback API. Never logged."""
        p = self.home / "api_token"
        if not p.exists():
            p.write_text(secrets.token_urlsafe(32))
            try:
                p.chmod(0o600)
            except OSError:
                pass
        return p.read_text().strip()


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(home: str | Path | None = None) -> Config:
    h = Path(home).expanduser() if home else _default_home()
    for sub in ("", "models", "cache", "logs", "backups"):
        (h / sub).mkdir(parents=True, exist_ok=True)
    sp = h / "settings.json"
    user = json.loads(sp.read_text()) if sp.exists() else {}
    settings = _merge(DEFAULT_SETTINGS, user)
    if os.environ.get("SHIFTWS_PROJECTS_ROOT"):
        settings["projects_root"] = os.environ["SHIFTWS_PROJECTS_ROOT"]
    cfg = Config(home=h, settings=settings)
    if not sp.exists():
        cfg.save()
    return cfg
