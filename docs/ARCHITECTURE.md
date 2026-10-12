# Architecture

```
 Desktop (Electron + React)                 Claude Code CLI (user's own login)
   chat · inspector · search                   │ claude -p … --mcp-config (workspace only)
        │ HTTP + SSE (127.0.0.1, token)        ▼
        ▼                                  shiftws MCP server (stdio, 68 tools)
 ┌──────────────────────── engine (Python, shiftws) ─────────────────────────┐
 │ tools.py — one tool layer used by UI buttons, chat, MCP, CLI              │
 │ chat/ (threads, context, providers: claude_code · ollama · offline)       │
 │ projects · memory (decisions, corrections, lessons) · search (FTS5)       │
 │ jobs (persistent queue: media lane ∥, premiere lane = single writer)      │
 │ workers: acquire(yt-dlp) · analysis(transcribe→crv→scenes) · reports      │
 │          narration · audio_catalog/catalog · render · qc · packaging      │
 │ premiere/ adapter → bridge MCP (hetpatel CEP) · fcpxml · apply/inspect    │
 └──────────── SQLite (WAL, migrations) + project folders + cache ───────────┘
```

## Data
- **Database:** `workspace.db` is authoritative. Project folders hold editable snapshots (`project.json`,
  `context.md`), reports, scripts, plans, renders and packages.
- **Assets:** identity is the sha256 of the file's bytes, so a duplicate import links the existing asset. Originals
  are referenced, not copied; narration is copied and then locked read-only.
- **Timing:** canonical positions are integer frames, with a rational source rate (e.g. 30000/1001) for source
  ranges and the sequence rate for timeline positions. Seconds are derived for display only. Premiere ticks are
  available via `Rate.frames_to_ticks`.
- **Preference precedence:** project > preset > channel. Every change is a row with provenance.
- **Memory:** structured decisions (approval / rejection / fact / technique / preference), with supersession and
  explicit corrections. Lessons are tagged as suggestion, channel rule, or hypothesis.
- **Reports:** versioned Markdown tied to the project, the originating thread, source hashes and settings. Sections
  are indexed so a chat turn retrieves only the relevant parts.
- **Coverage:** records which frames, contact sheets and dense passes were actually reviewed.
- **Deliverables:** several Shorts or long-form cuts can share one project's source analysis.

## Jobs
- **Claiming:** jobs are claimed atomically across processes (UI engine, MCP server, CLI).
- **Recovery:** a heartbeat plus `recover()` requeue jobs interrupted by a crash; resumable handlers continue from
  their checkpoint.
- **Caching:** a job whose kind and inputs match a finished one returns the cached result while its output files
  still exist.
- **Premiere lane:** serialized, with one writer. Every operation is logged with an id, preconditions and a
  verified postcondition. After a timeout, the actual state is inspected before anything is retried.

## Render and QC
- **Render:** the picture stage (clips, crop/zoom, ASS kinetic captions) and the mix stage (unprocessed locked VO,
  game audio, sidechain-ducked music, hit-aligned SFX) are cached separately. A revision re-renders only what it
  invalidated.
- **QC:** inspects the actual output file, never just the render tool's answer. It runs a full decode check, ffprobe,
  ebur128 loudness and true peak against configurable preset targets, silence/black detection, bar checks at every
  cut, a caption spelling/timing/safe-area check, narration byte and timeline integrity, a VO audibility check, and
  end-frame behavior.

## Security
- **Loopback API:** binds to 127.0.0.1 only and requires a per-install token. File serving is restricted to
  configured folders.
- **Subprocesses:** always argument arrays, never shell strings. Credentials and cookies are redacted from logs.
- **AI permissions:** the Claude CLI is allowed only the workspace MCP tools plus read-only file tools.
  Bash/Edit/Write are denied.
