# Build report — demos, test evidence, known limitations

Status labels used throughout:
- **tested here:** ran in this build container (x86_64 Linux, 4 CPU, no GPU) with real media.
- **awaiting Mac:** implemented, but needs your Mac (Premiere, CEP, Apple hardware, blocked sites).
- **not installed:** optional and not used.

Test media is licensed, and every file carries a provenance record (`scripts/make_test_media.sh`):

| Media | Source | License |
|---|---|---|
| Gameplay | Freedoom Phase 1/2 attract-mode demos, screen-recorded with game audio via Xvfb/PulseAudio | BSD-3-Clause |
| Music | Pingus tracker modules | GPL-2.0-or-later |
| SFX | freedesktop sound theme (CC-BY-3.0 / CC-BY-SA-3.0) and Pingus (GPL) | per file |
| Download sample | Big Buck Bunny | CC-BY 3.0 |

Narration in tests is a **local TTS stand-in** (espeak-ng). It is labeled as such and is never counted as an
ElevenLabs test.

## Phase 1 — installed engine, memory, ingestion, health
**Demo:** create a project → import a folder → probe → analysis job → close and reopen → state intact.
- **Persistence:** projects, preferences (project > preset > channel, with provenance), decisions, threads and jobs
  survive a restart. Covered by `test_create_project_and_persist_reopen` and `test_jobs_resume_after_restart_with_workers`.
- **Ingestion:** duplicates are linked by sha256. Footage picks up its license automatically from the folder's
  `provenance.json`. Rational rates are stored (30/1 probed).
- **Recovery:** a crash mid-analysis is requeued by `recover()` and resumes from its checkpoint without
  re-analyzing the finished asset (`test_recover_interrupted_analysis`).
- **Offline media:** a disconnected file is detected and relinked by hash (`test_detect_disconnected_media_and_relink`).
- **Health:** `shiftws discover` / `shiftws doctor` produce a redacted report with repairs, and `scripts/setup.sh`
  ran end to end here.

## Phase 2 — chat workspace, AI connection, scripting, narration handoff
**AI connection (tested here):** the chat drives the user's installed Claude Code CLI (2.1.296, logged in) through
68 workspace MCP tools. Verified with the real provider (`tests/test_claude_provider.py`, `docs/demo/acceptance_run.md`):
- created a project through chat;
- a gameplay file granted in chat triggered the analysis report automatically;
- it viewed actual frame images and recorded descriptions with confidence;
- it called the yt-script hook scorer;
- it recalled earlier findings after a full app restart.

See `docs/AI_CONNECTION.md` for the authentication boundary.

**Scripts:**
- **Tags by default:** every save and every revision gets strategic default tags ([curious] hook, [excited] payoff,
  [thoughtful] ending) when the writer left none.
- **Clean TTS text:** timestamps, shot notes, citations and SSML are moved out of the spoken text into notes.
- **Tag feedback:** tags marked as read aloud or excessive are removed from future versions automatically.
- **Exports:** five files — `script_elevenlabs_tagged.txt`, `script_spoken_clean.txt`, `script_sections.json`,
  `pronunciation_notes.md`, `beat_plan.json`.
- **Model status:** model/voice are flagged provisional (v3) until you confirm them.
- **Narration impact:** a new version never touches a locked take; it reports whether the take now differs.

**Narration:**
- **Intake:** import by drag-drop or watched folder, waiting until the file is stable. Ambiguous matches ask one
  question.
- **Alignment:** local ASR alignment (100% match on the stand-in), per-section timing, WPM, silence, peak and LUFS.
- **Lock:** a read-only copy, verified by hash.
- **Replacement:** marks dependent artifacts stale.

## Phase 3 — video analysis and searchable audio catalog
**Analysis (tested here):**
- **Single transcription:** sherpa-onnx Parakeet-TDT 0.6B int8 runs on CPU (measured against zipformer, which was
  rejected). Its transcript is fed to claude-real-video 0.10.7 as a sidecar, so audio is transcribed once.
- **Scenes:** scene-aware frames and 3×3 contact sheets, with luma, black-frame and pillar/letterbox metrics.
  Sparse coverage is flagged.
- **Modes:** quick / standard / deep (the requested `max-frames 450 · scene 0.22 · dedup 6 · grid` settings,
  validated against crv's `--help`) / focused.
- **Reports:** versioned Markdown. "NOT YET INSPECTED" stays until a model looks at the frame. Reports auto-revise
  after new inspections, and external edits are reconciled into a new revision.
- **Coverage accounting:** tracks frames, sheets and dense passes. A partial pass is never called exhaustive.
- **Shot boundaries:** FFmpeg scene detection finds cuts with no kept frame; dense passes (with optional HUD crop)
  cover them.
- **Investigations:** confirmed / suggestive / speculation / "no supporting evidence found in the inspected footage".
- **Inventory:** multi-file index with duplicates/overlaps and ranked Shorts candidates with their rationale.

**Audio catalog (tested here):**
- **Channel-wide:** folders are monitored; additions are indexed automatically with a notification (name, type,
  confidence, duration, license).
- **Removed vs disconnected:** a missing file in a present folder is "removed"; a missing folder is "offline".
- **Measurements:** EBU R128 loudness and true peak, hit point, band energy, waveform PNG.
- **Classification:** heuristic, with its confidence shown.
- **Placement policy:** **suggest_only_until_selected** — a plan contains no music/SFX until you select it.
- **Search across projects:** approvals and rejections are memory, so you can find "the impact approved for the
  Freedoom Short" from another project. Its origin is cited, and it is a reminder there, not a rule.

## Phase 4 — edit plan and Premiere integration
**Edit plan (tested here):**
- **Timing:** integer frames with rational rates; every clip and cue has a stable id.
- **Evidence:** each clip records its basis (vision description, or "sequential/uninspected — review").
- **Validation:** offline media, ranges, overlaps, narration lock (sha, gain 0, position 0), bars, license.
- **Targeted revisions:** `replace_music`, `scale_sfx`, `replace_clip`, `replace_shot_under_sentence`,
  `remove_bars`, `set_caption_style` and more. Each creates a new plan version and invalidates only the affected
  render stage.

**Premiere (adapter tested here; editing awaiting Mac):**
- **Bridge:** pinned hetpatel `adobe-premiere-pro-mcp@1.2.8`.
- **Live schema contract:** all 16 normalized operations matched the live tool schemas.
- **Statuses:** server ok · host not_connected · panel not_running · export backend unverified, each reported
  separately.
- **Apply path:** the plan becomes FCP7 XML, imported as one new sequence, then read back and reconciled.
  Kinetic captions go on V3 as a transparent ProRes 4444 overlay (render verified here).
- **No duplicates after timeout:** the adapter re-checks Premiere's actual state after a timeout instead of
  retrying blindly. Proven with a clearly labeled **stub** bridge (`test_premiere_no_duplicate_after_timeout_STUB`);
  this is a logic test, not a Premiere test.
- **Isolation:** Premiere jobs are blocked when Premiere is unavailable; other jobs continue.
- **Live test:** `scripts/premiere_disposable_test.py` is ready for your Mac.

## Phase 5 — rough cut, review, QC, export (FFmpeg path, tested here)
- **Render:** 1080×1920, aspect-preserving fill crop (the 4:3 title card's bars are cropped away), restrained zoom,
  and yellow black-outlined word-by-word captions (pixel-verified in a test). Music is sidechain-ducked under
  unprocessed VO, and SFX are placed on their measured hit point.
- **QC:** a full decode and an independent probe of the actual file, loudness/true peak against preset targets,
  bar checks at every cut, caption checks, narration bytes and timeline unchanged, and VO audibility.
- **Example flag:** QC flagged −21 LUFS vs the −14 target because the espeak VO is quiet. It reports this rather
  than touching the locked VO.
- **Targeted revision:** replacing the song re-rendered only the mix. The picture was reused (1.8 s vs 47 s), no
  analysis job re-ran, and narration bytes were verified unchanged.

## Phase 6 — upload package, analytics, polish
- **Upload package:** captions SRT, chapters from final timing (validated, with an honest "not applicable" for
  short cuts), title lint (yt-package `title.py`), and a credits block built from license records. Also a
  thumbnail brief (text only), a checklist, and an Open YouTube Studio button. Nothing is published.
- **Analytics:** Studio CSV import with column mapping. Missing fields are listed as missing, never zero-filled.
  yt-retention findings are stored as hypotheses tied to the export version.
- **Automation measurement:** recorded per step as automated / handoff / user decision. It is a goal metric, not
  a dashboard claim.
- **Desktop:** Electron + React chat-first UI, smoke-tested headless with screenshots (`docs/demo/screenshots/`).

## Acceptance sequence (real AI provider)
See `docs/demo/acceptance_run.md`: the real Claude Code CLI, the engine API, real media.

| Step | Result |
|---|---|
| 1. Project created through chat | Worked (preset 30 s Shorts target applied). |
| 2. Gameplay granted in chat → linked report | Worked. Claude waited on the job, viewed 17 frames, described 14 with confidence, and noted the 9 s sampling gaps. |
| 3. Tagged script approved | Worked. It flagged that health numbers came from low-resolution frames, used no invented verdict, and noted the voice/model weren't confirmed. |
| 4. Approve / reject | Approval of `fd_device-added` for "hook impact" recorded with reasons. |
| 5–6. Close and reopen | Resumed from structured state. Claude then ran a dense frame check before keeping the "sixties" line, fixed shot placement, and rendered + QC'd the preview. |
| 7. Search across projects | The approved impact was found from a second project, with license provenance; it was not copied into the second project. |
| 8. Targeted revision | **Not completed in that run:** the CLI hit its plan's usage limit. Covered by `test_replace_song_preserves_vo_and_analysis`; to be re-shown in the Mac live run. |

**Defects the run found, now fixed with regression tests:**
- A dropped stand-in take wasn't flagged, and a re-import couldn't relabel it. Claude correctly refused to lock it
  until asked.
- Captions used speech-recognition spellings ("Freedom's", "then", "90s") instead of the approved script words.
- Audio routing only recognized narration while the VO task was open.

## Regression round (after the acceptance run)
`tests/test_regressions.py` (13 tests, real media):
- **Audio routing:**
  - stand-in wording is honored;
  - a re-import corrects labels only;
  - a replacement take never replaces the locked one;
  - SFX and songs go to the catalog as suggestion-only;
  - ambiguous files trigger one question;
  - no-commentary deliverables never get narration.
- **Skills:** the six real yt-* utilities run on project data and are saved as chat-linked, searchable artifacts.
  yt-edit output is stored as suggestions only.
- **Research:** sources are validated (date, category, public/private).
- **Captions:** they use the approved script's words, with a `caption_fix` op.
- **Deliverables:** two deliverables stay separate through plan get/validate, render, QC selection, packages,
  Premiere sequence naming and recorded sequence, and per-deliverable status.
- **Premiere export:** the job waits for a complete file, probes it, registers it under the right deliverable,
  QCs it and never overwrites. This is shown with the clearly labeled **stub** bridge, as a logic test only.

## Known limitations
- **Premiere editing and export:** not yet run against real Premiere (pending Mac checks #0, #3–6). The FCP7 XML
  scale and audio-level interpretation must be confirmed by readback. `premiere_export` is verified only against
  the stub bridge.
- **ElevenLabs:** no real take tested. Tag behavior with your voice is still unknown and will be learned through
  tag feedback. The character limit is provisional until you confirm the model.
- **yt-dlp site extractors:** YouTube etc. untested here (blocked sites). The generic extractor was tested with a
  CC-BY sample.
- **Ollama:** implemented but unmeasured (no model reachable here).
- **whisper.cpp:** backend implemented; untested here (HuggingFace blocked).
- **Scene understanding:** comes only from a vision model actually looking at frames. Metrics (luma, bars) are not
  descriptions.
- **Audio classification:** heuristic (duration, crest factor, band energy). Treat its labels as hints.
- **Attachment auto-analysis:** understands simple time hints ("first 60 seconds", "from 1:20 to 2:05"); anything
  more complex falls back to the full file.
- **Packaging/signing:** the Electron app runs from source (`npm --prefix desktop start`); it isn't packaged as a
  signed .app yet.
- **Not built:** optional integrations vidIQ, OpusClip, and browser-assisted Studio filling.
- **Named methods:** SEO 5/5/4/1, the bucket-list template and the 70% formula are stored as unresolved until you
  define them.
