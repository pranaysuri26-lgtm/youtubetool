"""Initial channel defaults for 5 to 9 Shift. These are seeds; later changes are stored as preference rows
with provenance, never by editing this file."""

CHANNEL_NAME = "5 to 9 Shift"

CHANNEL_DEFAULTS = {
    "audio_tags_default": True,
    "narration_generation": "manual_external",      # the user generates ElevenLabs audio themselves
    "footage_policy": "Prefer main-story footage. Do not add grinding footage unless the project asks for it.",
    "publishing": "package_and_open_studio",          # no automatic publishing
    "visual_policy": "Visuals demonstrate or develop the point; no unrelated gameplay wallpaper.",
    "structure_hint": "hook -> question -> payoff -> escalation, when it suits the format",
    "audience": None,                                 # intentionally unset: not invented
    "writing_style": None,                            # comes from voice.md, written by the user
    "words_per_minute": None,                         # learned from real narration takes
    # Catalog awareness never authorizes timeline placement: music/SFX enter a plan only after the user selects
    # an asset or delegates audio selection for that project/task.
    "audio_selection": "suggest_only_until_selected",
    # Named user methods whose definitions have not been provided yet. Never invented; filled in by the user.
    "named_methods": {
        "seo_5_5_4_1": {"status": "unresolved", "definition": None, "note": "user's SEO 5/5/4/1 structure"},
        "bucket_list_template": {"status": "unresolved", "definition": None, "note": "user's bucket-list template"},
        "formula_70": {"status": "unresolved", "definition": None, "note": "user's 70% formula"},
    },
    "opinion_policy": "Personal verdicts, emotions and private numbers come from the user; use [YOU: ...] placeholders.",
}

# Provisional: ElevenLabs v3 supports inline audio tags. Model and voice stay unconfirmed until the user picks.
NARRATION_PRESET = {
    "id": "eleven_default",
    "name": "ElevenLabs (provisional v3)",
    "model": "eleven_v3",
    "voice": None,
    "confirmed": False,
    "settings": {
        "supports_audio_tags": True,
        "tag_policy": "strategic",                    # hook, emotional shift, payoff — not every line
        # Starter directions from ElevenLabs' v3 audio-tag guidance; feedback below is learned per voice.
        "starter_tags": ["curious", "excited", "whispers", "sighs", "sarcastic", "serious", "thoughtful",
                         "surprised", "laughs", "short pause"],
        "tag_feedback": {"works": [], "excessive": [], "spoken_aloud": [], "rejected_styles": []},
        # Documented per-request character limits change; this is a provisional value to re-check.
        "max_chars_per_generation": 5000,
        "max_chars_source": "provisional — confirm against current ElevenLabs model docs",
    },
}

_SHORT_BASE = {
    "width": 1080, "height": 1920, "fps": "30/1",
    "bars": "none",                                   # zero unintentional bars: crop/reframe, never letterbox
    "reframe": "aspect_preserving_crop",
    "captions": {"style": "kinetic_word", "font": "Montserrat ExtraBold", "fallback_font": "DejaVu Sans",
                 "color": "#FFE600", "outline": "#000000", "outline_px": 8, "size_px": 92,
                 "position_y": 0.68, "max_words_on_screen": 3},
    "zoom": {"style": "restrained", "max_scale": 1.08},
    "sfx": {"enabled": True, "intensity": "subtle", "gain_db": -16},
    "music": {"enabled": False},                      # off unless requested
    "targets": {"integrated_lufs": -14.0, "true_peak_db": -1.0, "tolerance_lu": 2.0,
                "note": "configurable review target, not a platform requirement"},
    "target_seconds": 30,                             # starting target from the user's workflow, not a hard maximum
}
_LONG_BASE = {
    "width": 1920, "height": 1080, "fps": "30/1",
    "bars": "avoid", "reframe": "fit_or_crop",
    "captions": {"style": "srt"},
    "zoom": {"style": "restrained", "max_scale": 1.05},
    "sfx": {"enabled": True, "intensity": "moderate", "gain_db": -14},
    "music": {"enabled": True, "selection": "project_specific", "duck_under_vo_db": -12},
    "game_audio": "keep relevant dialogue; duck under narration",
    "targets": {"integrated_lufs": -14.0, "true_peak_db": -1.0, "tolerance_lu": 2.0,
                "note": "configurable review target, not a platform requirement"},
}

FORMAT_PRESETS = [
    {"id": "short_narrated", "name": "Short — narrated", "kind": "ranking_short", "narrated": True,
     "settings": {**_SHORT_BASE, "tracks": {"A1": "narration (locked)", "A2": "game audio", "A4": "sfx",
                                            "V1": "main footage", "V2": "overlays", "V3": "captions"}}},
    {"id": "short_no_commentary", "name": "Short — no commentary", "kind": "no_commentary_story", "narrated": False,
     "settings": {**_SHORT_BASE, "captions": {**_SHORT_BASE["captions"], "source": "game_dialogue"},
                  "tracks": {"A2": "game audio", "A4": "sfx", "V1": "main footage", "V3": "captions"}}},
    {"id": "long_narrated", "name": "Long-form — narrated essay/review", "kind": "narrated_essay", "narrated": True,
     "settings": {**_LONG_BASE, "tracks": {"A1": "narration (locked)", "A2": "game audio", "A3": "music",
                                           "A4": "sfx", "A5": "sfx", "V1": "main footage", "V2": "b-roll",
                                           "V3": "titles"}}},
    {"id": "long_no_commentary", "name": "Long-form — no-commentary story cut", "kind": "no_commentary_story",
     "narrated": False,
     "settings": {**_LONG_BASE, "music": {"enabled": False}, "tracks": {"A2": "game audio", "A3": "music",
                                                                        "A4": "sfx", "V1": "main footage"}}},
    {"id": "cinematic", "name": "Cinematic gameplay", "kind": "cinematic", "narrated": False,
     "settings": {**_LONG_BASE, "bars": "intentional_letterbox_allowed", "captions": {"style": "none"},
                  "tracks": {"A2": "game audio", "A3": "music", "V1": "main footage"}}},
]

PROJECT_FOLDERS = ["brief", "research", "scripts", "voiceover", "source-media", "proxies", "analysis", "music",
                   "sfx", "captions", "edit-plans", "premiere", "previews", "exports", "upload-package", "logs"]

VOICE_MD = """# voice.md — 5 to 9 Shift

Edit this file in your own words. The workspace and the yt-* skills read it before writing.
Nothing below has been filled in for you on purpose: audience and style are yours to define.

## Who I am talking to

(not set)

## How I actually talk

(paste transcripts of your own videos here)

## Words I never use

(not set)

## Words I do use

(not set)

## What I will not claim

(not set)

## My format

- Narrated and no-commentary videos are separate formats.
- Duration is chosen per project.

## ElevenLabs delivery notes

- Every new script and revision includes an ElevenLabs-ready version with inline audio tags.
- Tags that work with my voice: (learned)
- Tags that sound excessive or are read aloud: (learned)
"""


# Editable channel templates (written once into <SHIFTWS_HOME>/templates; the user owns them afterwards).
TEMPLATES = {
    "shorts.md": """# Short template — 5 to 9 Shift

- Starting target: 30 s (per-project override; never trim locked narration to hit it)
- Structure (when it suits): hook -> question -> payoff -> escalation
- 1080x1920, aspect-preserving crop, zero unintended bars
- Captions: bright yellow, black outline, word by word
- Restrained zooms; subtle SFX only when selected; music off unless requested
- Visuals must demonstrate the point (no unrelated gameplay wallpaper)
- [YOU: your opening style]
""",
    "ranking.md": """# Ranking video template

- Countdown order: [YOU: ascending or descending]
- Per entry: claim -> evidence clip -> verdict [YOU: your verdict]
- Evidence: every ranked claim needs a footage moment or dated source
""",
    "bucket_list.md": """# Bucket-list template

Definition: UNRESOLVED — the user's bucket-list template has not been provided yet.
The workspace will not invent it. Paste or describe it here, or point to an existing file.
""",
    "long_form.md": """# Long-form template

- Narrative pacing; relevant game audio kept; project-specific music (selected, not auto-placed)
- Chapters from final timing (>= 3 chapters, first at 0:00, each >= 10 s)
- SEO structure "5/5/4/1": UNRESOLVED until defined by the user
- "70% formula": UNRESOLVED until defined by the user
""",
}
