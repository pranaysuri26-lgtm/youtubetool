"""Importing this module registers every job handler."""
import importlib

for _m in ("acquire", "analysis", "audio_catalog", "narration", "render", "qc", "packaging", "reports",
           "premiere.jobs"):
    importlib.import_module(f"shiftws.{_m}")
