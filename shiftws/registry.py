"""Integration registry (pinned versions, licenses, checksums, statuses)."""
from __future__ import annotations

import json
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "integrations" / "registry.json"


def load_registry() -> list[dict]:
    return json.loads(PATH.read_text())["integrations"]
