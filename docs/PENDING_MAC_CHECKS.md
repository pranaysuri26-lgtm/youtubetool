# Pending checks on your Mac

The build container is x86_64 Linux. It has no Premiere and no GPU, and its network policy blocks video sites and
HuggingFace. Everything below is implemented, but none of it counts as tested until it runs on your Mac.
Send me the files these commands produce.

| # | Check | How | What counts as passing |
|---|---|---|---|
| 0 | **Primary journey, live** — gameplay → linked report → same-chat planning → approved audio → Premiere edit → verified export | `.venv/bin/python scripts/mac_live_acceptance.py --gameplay "<file>" --sfx-folder "<folder>" --export-preset "<preset.epr>" [--narration <ElevenLabs download>]` with a `*disposable*` Premiere project open, then send `docs/local/mac_live_acceptance.md` | Every step reads `[PASSED]`. Steps that depend on Premiere read `[BLOCKED]` (never "passed") if Premiere isn't connected. A step passes only on a state check (report card in the chat, frames described, take locked, approval recorded, readback matches, exported file probed + QC). |
| 1 | Discovery of your real setup (tools, skills, MCP server names, Premiere version, chip/RAM/GPU, drives) | `scripts/setup.sh`, then send `docs/local/discovery.md` + `docs/local/health.md` | Names and versions only, secrets redacted. Any existing Premiere bridge is detected and preserved. |
| 2 | Packaged desktop app launch, file permissions, drag-drop paths with spaces, external drives | `npm --prefix desktop start`; grant a folder on an external drive from a chat | The analysis report card appears in that chat; offline drives show as offline, not removed. |
| 3 | Premiere bridge identity and connection | Connections → Check Premiere, or `shiftws tool premiere_status` | Server, host, panel and export statuses all report correctly. |
| 4 | Live Premiere edit on a disposable project | `scripts/premiere_disposable_test.py --footage … --export-preset …` | import, XML → new sequence, readback matches the plan, marker, audio level, SRT caption track, save. The export file is probed independently. |
| 5 | Kinetic captions inside Premiere | Apply a plan with Shorts captions | The transparent ProRes 4444 overlay sits on V3 and lines up word by word; it is labeled as an overlay render. |
| 6 | Premiere repair mode | `premiere_inspect` on a sequence with a muted A1 | The diagnosis names mute/routing rather than source silence. |
| 7 | whisper.cpp reuse | Set `whisper_cpp_bin` + `whisper_cpp_model`, then import a narration take | Word timings come out; alignment shows ≥ 90% match on a real ElevenLabs take. |
| 8 | Real ElevenLabs take | Generate from **Copy for ElevenLabs**, drop the download into the chat | Tags are not read aloud; any that are get recorded via tag feedback. The take locks with verified bytes. |
| 9 | yt-dlp site extractors (e.g. YouTube) with optional browser cookies | "Download and analyze this trailer: <url>" | Progress, metadata, subtitles, dedupe on retry, and actionable errors for private/unsupported sources. |
| 10 | Ollama (optional) | Install a vision-capable model, enable it in settings | Measure tool-call reliability and image inspection before relying on it. |
| 11 | Electron packaging/signing | Out of scope until you choose a distribution method | — |
| 12 | Apple Silicon performance | Run a Deep Dive on a ~27 min 4K recording | Record wall time and frame/coverage counts in the report. |
