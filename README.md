# 5 to 9 Shift — Video Workspace

A personal, local-first video production workspace for one creator. It is chat-first: each video project has a
persistent chat that runs real jobs, including analysis, scripts, narration intake, the music/SFX catalog, edit plans,
renders, QC and the upload package. The workspace remembers projects, decisions and progress across restarts.
The core workflow needs no cloud API keys.

- **The app owns state and media work:** a SQLite database (authoritative), project folders, a persistent job queue,
  and FFmpeg/Python workers.
- **The AI is your installed Claude Code CLI:** the app runs it in its documented non-interactive mode, scoped to the
  workspace's own MCP tools. Its login is the CLI's own; the app never reads, copies or stores Claude credentials.
  Ollama is an optional local alternative, off by default. An offline mode keeps deterministic commands working
  without any AI.
- **ElevenLabs is a deliberate manual step:** every script comes as an ElevenLabs-ready tagged version plus a clean
  version. You generate the audio and drop it in (or let the watched folder pick it up). The app aligns it, measures
  it and locks it.
- **Publishing is also manual:** you get a complete upload package and a button that opens YouTube Studio.
  Analytics come from Studio CSV exports.

## Quick start (Mac)

```bash
git clone <this repo> && cd youtubetool
scripts/setup.sh            # checks tools, builds .venv + desktop, writes docs/local/{discovery,health}.md
npm --prefix desktop start  # opens the app (starts the local engine on 127.0.0.1)
```

`setup.sh` never upgrades Premiere, installs a bridge, or edits `~/.claude.json`, `~/.claude/skills` or global MCP
config. It prints any proposed change instead. If whisper.cpp is already installed, set
`transcription.whisper_cpp_bin` and `transcription.whisper_cpp_model` in
`~/Library/Application Support/5to9Shift/settings.json`. Otherwise run `scripts/setup.sh --models` to get the
CPU model that was tested here.

Live acceptance of the whole journey (gameplay → linked report → planning in the same chat → approved audio →
Premiere edit → verified export). Steps that need Premiere are reported BLOCKED if it isn't connected:

```bash
.venv/bin/python scripts/mac_live_acceptance.py --gameplay "/Volumes/Rec/session.mp4" --sfx-folder ~/Music/SFX \
    --export-preset ~/path/to/YouTube.epr [--narration ~/Downloads/elevenlabs.mp3]
```

Premiere tool-level test (run on the Mac with a project named `*disposable*` open and the bridge panel running):

```bash
.venv/bin/python scripts/premiere_disposable_test.py --footage ~/Movies/clip.mp4 --export-preset /path/to/preset.epr
```

## Using it

- **Sidebar:** the channel workspace, your projects and their threads (project chat, research, script, footage,
  audio, edit, results), and a global search scoped to this thread, this project, or all projects.
- **Chat:** ask for work, for example:
  - "analyze these recordings" (use Grant folder, or drag and drop files)
  - "write the script with my ElevenLabs tags"
  - "use the SFX style from my previous Short"
  - "replace the music without touching narration"
  - "render and check it"

  Jobs appear as live cards with progress, cancel/resume, previews, waveforms and QC flags. Analysis reports appear
  as artifact cards linked to that chat.
- **Inspector:** stage and next action, script (with **Copy for ElevenLabs**), media and scene frames, edit plan,
  exports, memory (inspect, correct, delete), alerts, and Connections (AI provider, Premiere status, integrations,
  health report).

## Layout

```
shiftws/            engine (Python): db+migrations, projects/memory, jobs, analysis, reports, scripts, narration,
                    audio catalog, edit plans, render, QC, packaging, Premiere adapter, chat engine + providers,
                    workspace MCP server, loopback API, CLI
desktop/            Electron + React + TypeScript shell (chat-first UI)
vendor/             pinned yt-* skills (MIT) used through their real --json tools
integrations/       registry.json (pinned versions, licenses, checksums, statuses) + pinned Premiere bridge test install
scripts/            setup.sh, make_test_media.sh, premiere_disposable_test.py, demo_acceptance.py
tests/              acceptance tests with real media (+ opt-in real Claude Code provider test)
docs/               REPORT.md (phase demos + limitations), PENDING_MAC_CHECKS.md, AI_CONNECTION.md, ARCHITECTURE.md
```

## Tests

```bash
scripts/make_test_media.sh /root/shiftws-testmedia     # Linux: licensed test media with provenance
.venv/bin/pytest -q tests/                              # acceptance + regression + unit tests, yt-dlp download test
SHIFTWS_TEST_CLAUDE=1 .venv/bin/pytest -q tests/test_claude_provider.py   # uses your Claude Code login
cd desktop && OUT=shots node test/smoke.mjs             # headless Electron UI smoke test (needs a display/Xvfb)
```
