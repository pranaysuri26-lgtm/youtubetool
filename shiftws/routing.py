"""Routing for audio dropped into a chat (or granted from a folder).

Decides narration take vs music vs SFX from evidence, in this order:
  1. project format — a no-commentary deliverable never receives narration;
  2. the user's message ("new take", "voiceover", "song", "impact"…) — explicit wording wins;
  3. the filename ("elevenlabs", "narration", "vo_", "sfx", "whoosh"…);
  4. workspace state — an open "generate narration" handoff, or an existing take of similar length;
  5. duration — very short → SFX; otherwise, if still ambiguous, ask ONE concise question.

Narration goes through the take pipeline (import → align → review → lock; an existing locked take is never
replaced automatically). Music/SFX go into the channel-wide catalog (measured, license evidence, notification)
and are suggestion-only: nothing is placed on a timeline by importing it.
"""
from __future__ import annotations

import re
from pathlib import Path

NARRATION_WORDS = r"\b(narration|voice ?over|voice-over|\bvo\b|take|elevenlabs|eleven labs|regenerat\w*|re-?record\w*|my read)\b"
MUSIC_WORDS = r"\b(music|song|track|underscore|bgm|soundtrack|score|beat)\b"
SFX_WORDS = r"\b(sfx|sound effects?|impact|whoosh|hit|riser|stinger|swoosh|boom|click)\b"
STANDIN_WORDS = r"stand-?in|not elevenlabs|local tts|placeholder|espeak|\bsay\b|temp(orary)? (vo|narration)"


def _narrated(ws, project_id: str, deliverable_id: str | None) -> bool:
    from .projects import get_deliverable
    d = get_deliverable(ws, deliverable_id, project_id)
    return bool(ws.db.one("SELECT narrated FROM format_presets WHERE id=?", (d["format_preset"],))["narrated"])


def classify(ws, project_id: str, path: str, text: str = "", deliverable_id: str | None = None) -> dict:
    from .probe import summarize
    name = Path(path).stem.lower().replace("_", " ").replace("-", " ")
    t = (text or "").lower()
    narrated = _narrated(ws, project_id, deliverable_id)
    standin = bool(re.search(STANDIN_WORDS, t, re.I))
    try:
        dur = float(summarize(path).get("duration") or 0)
    except Exception:  # noqa: BLE001 - unreadable file: let the import report it
        dur = 0.0

    def out(route, reason):
        return {"route": route, "reason": reason, "standin": standin and route == "narration", "duration": dur,
                "narrated_project": narrated}

    says_narr = bool(re.search(NARRATION_WORDS, t)) or standin
    says_music = bool(re.search(MUSIC_WORDS, t))
    says_sfx = bool(re.search(SFX_WORDS, t))
    if says_narr and not (says_music or says_sfx):
        if not narrated:
            return out("ask", "you called it narration, but this deliverable is no-commentary")
        return out("narration", "your message calls it narration")
    if says_music and not (says_narr or says_sfx):
        return out("music", "your message calls it music")
    if says_sfx and not (says_narr or says_music):
        return out("sfx", "your message calls it a sound effect")
    if narrated and re.search(r"elevenlabs|narration|voice ?over|\bvo\b|take ?\d", name):
        return out("narration", "the filename looks like a narration take")
    if re.search(r"\b(sfx|whoosh|impact|hit|riser|boom|swoosh|click)\b", name) or (0 < dur <= 4.0):
        return out("sfx", "short sound / SFX-like filename")
    if re.search(r"\b(music|song|track|underscore|bgm|ost)\b", name):
        return out("music", "the filename looks like music")
    if narrated:
        waiting = ws.db.one("SELECT 1 FROM tasks WHERE project_id=? AND status='open' AND title LIKE 'Generate narration%'",
                            (project_id,))
        if waiting:
            return out("narration", "this project is waiting for its narration take")
        last = ws.db.one("SELECT duration FROM narration_takes WHERE project_id=? AND status!='replaced' "
                         "ORDER BY created_at DESC LIMIT 1", (project_id,))
        if last and last["duration"] and abs(dur - last["duration"]) / last["duration"] <= 0.35:
            return out("narration", "similar length to the current take — treated as a new take (not locked)")
    if not narrated and dur >= 20:
        return out("music", "long audio in a no-commentary project")
    return out("ask", "could be narration or music")


def route_audio(ws, project_id: str, thread_id: str | None, path: str, route: str, *, standin: bool = False,
                origin: str = "chat", deliverable_id: str | None = None) -> dict:
    """Execute a routing decision. Narration → take pipeline. Music/SFX → channel catalog (suggest-only)."""
    from .assets import import_file
    from .audio_catalog import analyze_audio_asset
    from .catalog import notify
    if route == "narration":
        from .narration import import_take
        had_locked = ws.db.one("SELECT take_no FROM narration_takes WHERE project_id=? AND deliverable_id IS ? AND "
                               "status='locked'", (project_id, deliverable_id or _default_dlv(ws, project_id)))
        t = import_take(ws, project_id, path, synthetic_standin=standin, thread_id=thread_id, origin=origin,
                        deliverable_id=deliverable_id)
        note = (f"Imported {Path(path).name} as narration take {t['take_no']}" + (" (local TTS stand-in)" if standin else "")
                + (f"; take {had_locked['take_no']} stays locked until you lock the new one" if had_locked and
                   not t.get("duplicate") else "") + (" — same file as an existing take (labels updated)" if t.get("duplicate") else ""))
        return {"route": "narration", "take_id": t["id"], "take_no": t["take_no"], "job_id": t.get("align_job"), "note": note}
    if route not in ("music", "sfx"):
        raise ValueError(f"unknown route {route}")
    a = import_file(ws, path, project_id=project_id, role=route, library="chat imports", tags=[route])
    ws.db.update("media_assets", {"id": a["id"]}, {"catalog_state": "present"})
    an = analyze_audio_asset(ws, a["id"])
    labels = ", ".join(f"{l['label'].replace('_', ' ')} {l['confidence']:.0%}" for l in an["classification"]["labels"])
    if not a.get("duplicate"):
        notify(ws, "catalog.added", f"New {route}: {a['title']}",
               f"{route} · {labels} · {a['duration'] or 0:.1f}s · {a['license']['license']} · added from a chat; "
               "available in every project (suggestion only — not placed anywhere)", ref_id=a["id"], project_id=project_id)
    return {"route": route, "asset_id": a["id"], "duplicate": a.get("duplicate", False), "labels": an["classification"]["labels"],
            "note": f"Added {Path(path).name} to the channel catalog as {route} ({labels}); not placed on any timeline."}


def _default_dlv(ws, project_id):
    from .projects import get_deliverable
    return get_deliverable(ws, None, project_id)["id"]
