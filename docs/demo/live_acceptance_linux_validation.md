# Live acceptance script — validation run in the Linux build container

Real Claude Code CLI, real media, engine API. No Premiere here, so the Premiere steps are BLOCKED by design. The final run happens on your Mac (docs/PENDING_MAC_CHECKS.md #0).


- [passed] prerequisite: Claude Code CLI logged in
- [failed] prerequisite: Premiere bridge + host connected (read-only check)
- [passed] prerequisite: export preset exists
- [passed] gameplay → linked Markdown report in the same chat
- [passed] same-chat planning used the report (frames inspected, tagged script approved)
- [passed] narration imported, aligned, locked
- [passed] approved audio recorded for this project
- [passed] edit plan valid and contains the approved SFX
- [blocked] Premiere edit applied and read back
- [blocked] Premiere export verified by file probe + QC

## Transcript
# Live acceptance run — 2026-10-13 03:24


**[PASSED] prerequisite: Claude Code CLI logged in** — {"version": "2.1.296 (Claude Code)"}

**[FAILED] prerequisite: Premiere bridge + host connected (read-only check)** — {"server": "ok", "host": "not_connected", "panel": "not_running", "export_backend": "unverified (requires a disposable export test on the Mac)"}

**[PASSED] prerequisite: export preset exists** — {"preset": "<run>/fake preset.epr"}

**You:** Create a narrated Short project titled 'Live acceptance — gameplay to export'. Defaults.

_tools: ToolSearch, project_create, memory_save · 16s_

**Claude Code:** I created the project as a narrated Short.

- **Project:** `prj_c7aa1ab3e64a`, "Live acceptance — gameplay to export"
- **Main thread:** `th_d07cd3e25751`
- **Folder:** `…/scratchpad/live2/projects/live-acceptance-gameplay-to-export-b3e64a`
- **Settings:** the standard narrated-Short setup. Spoilers default to "avoid major spoilers". Game, topic, promise, audience and length are blank. Music and sound effects stay suggestions until you pick them.
- **Open tasks:** write and approve the script, generate the narration in ElevenLabs and import it, and import or download footage.

I saved this setup to the project's memory (`dec_d349207cbda5`).

To start the script and footage search, I need:
1. The game and footage. Do you have gameplay to import, or a link I should download?
2. The topic or promise: what the viewer should get from the Short.
3. Optionally, a target length. If you don't give one, the default applies.

**You:** Here's my gameplay. Analyze it (first 3 minutes) and keep the report linked here.  
_attached: freedoom2_attract.mp4_

_tools: ToolSearch, job_wait, job_status, job_wait, report_sections, report_sections, Grep, ToolSearch, scene_frames, Grep, Read, Read, Read, Read, Read, Read, Read, Read, Read, Read, Read, Read, Read, Read, Read, Read, Read, Read, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, Read, scene_describe, scene_describe, scene_describe, ToolSearch, report_refresh, memory_save · 213s_

**Claude Code:** Halfway done. I'll wait in shorter steps so the call doesn't time out.

The report finished, but its 140 frames haven't been looked at yet. Every section says "NOT YET INSPECTED". I'll look at a spread of frames now so the report contains real descriptions.

Scene ids follow the section start times. I'll read a sample of frames across the clip, covering the brightness spikes and the long sections.

I've looked at 16 frames across the clip, so I'll save what I saw into the report.

I made one mistake while recording: I wrote the description for frame 080 (82.0s) under the 83.03s section. I'll check the right frame and correct both entries.

The analysis is finished and the report is linked to this chat. The automatic pass only extracted frames and didn't describe any of them, so I looked at 19 frames spread across the clip myself and added what I saw to a second version of the report.

- **Report:** `rep_0dc21f0f8887` (version 2), at `projects/live-acceptance-gameplay-to-export-b3e64a/analysis/analysis_report_r02.md`. Version 1 (`rep_c3238bdfb28e`) is kept.
- **Length:** the clip is only 150s, so "first 3 minutes" covers the whole thing.

**What it shows:** this doesn't look like you playing. It's **Freedoom: Phase 2 (v0.13.0-1) running its own built-in demo loop**: the game's recorded demos playing back, with a credits screen in between.

| Time | What's on screen | Confidence |
|---|---|---|
| 0–~9s | Title screen with key art. It has small black bars on the sides. | high |
| ~10–87s | **Demo 1**, in a tech base: picking up items, enemies in a corridor (~25s), a melee fight (~55s), a shot fired down a hall (~68s), and **a point-blank hit with a big blood spray at ~80.2s**, the strongest action frame I sampled | medium |
| ~87–99s | Credits for the game engine (PrBoom), with a melting screen transition at each end. No gameplay. | high |
| ~99–134s | **Demo 2**, in brown rock caves: mostly walking and picking up items | medium |
| ~133.9s | A full-screen green teleport burst, and the health, ammo and armour readout resets to starting values. Probably a new demo starting. It would make a punchy transition. | medium |
| ~134–150s | Rock corridors. Armour reads about 100% at ~147s. | medium |

**Caveats:**
- The health and ammo numbers are read off low-resolution frames, so treat them as approximate. At ~82–83s I can't tell whether health is 19% or 79%.
- The pickup messages at the top of the screen are mostly unreadable.
- 121 of the 140 sampled frames are still unchecked. The samples are also spread out, with an 11s gap over the credits. Before we use any specific moment, I'll check closely spaced frames around it.

I saved this summary to project memory.

**Questions before the script:**
1. Is this the footage you want to use, or do you have your own recording? Demo playback isn't your play, and 30s would be tight once the title and credits are cut out.
2. If we keep it, what's the topic and the promise to the viewer, and should I set the game to Freedoom: Phase 2?

**[PASSED] gameplay → linked Markdown report in the same chat** — {"reports": ["<run>/projects/live-acceptance-gameplay-to-export-b3e64a/analysis/analysis_report_r02.md"], "card_in_chat": true}

**You:** From the saved report, pick the strongest 30-second Short idea. Inspect the frames you rely on, then write the script with my ElevenLabs tags and approve it.

_tools: Read, Read, Read, Read, Read, Read, Read, Read, Read, Read, ToolSearch, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, scene_describe, Read, Read, scene_describe, scene_describe, scene_describe, scene_describe, project_update_brief, script_save, script_save, script_approve, memory_save, ToolSearch, task_done · 171s_

**Claude Code:** I'm going with the busiest stretch of demo 1, about 55–85s. I've checked the frames in that window, and I'll record what they show before writing.

Two of those entries are off by one frame: what I saw in frame_067 and frame_071 actually belongs to the 71s and 75s sections. I'll check frames 068 and 072, then correct the entries.

All the frames the script relies on are now checked and recorded. Next I'll fill in the brief and save the script.

The script is written, saved with your ElevenLabs tags and approved. The idea is **"Nobody is playing this"**: the busiest fight in the clip turns out to be Freedoom's own demo, replaying on the title screen.

**Why this stretch:** about 55–88s of demo 1 has the most action, in order: four enemies grouped in one room (~59s), a close-range fight with two monsters filling the screen (~63s, the strongest frame), a full red screen flash (~67s), a shot down a long hall (~68s), a shotgun pickup (~81s), and a close-range hit with a blood spray (~80s). The demo-loop twist is the only real angle this footage has, so the script leads with it instead of hiding it.

**Frames checked:** I read every frame the script relies on (55–88s, plus the title card) and recorded what each shows in the report. Two entries had been saved under the wrong timestamps, one frame off; I spotted that and corrected them before writing.

**What the claims rest on:**
- **Seen directly in the frames:** the enemies, the close-range fight, the red flash, the shotgun pickup message (small, so read with about 70% confidence).
- **Inferred:** that it's a replayed demo. That comes from how the clip loops (title, gameplay, credits, gameplay) and the file name `freedoom2_attract`.
- **General knowledge, not measured:** that leaving the menu idle plays the demo.
- The health and ammo numbers are too blurry to trust, so the narration doesn't mention any.

**Paste into ElevenLabs:**
```
[curious] Nobody is touching the controls right now. Not one button.

This is a recording Freedoom replays on its own title screen.

[excited] Four enemies waiting in one room. A point-blank firefight. The whole screen flashes red.

Then it picks up a shotgun, and walks straight into the next one. [whispers] Leave the menu alone long enough, and it plays this for you.

[thoughtful] Would you have played it any better?
```
It's 68 words, about 27 seconds at a default pace. The tags fall on the hook, the build-up, the reveal and the closing question.

**What needs your input:**
- **Your verdict line.** In the first save, the save tool deleted my placeholder for it and left a stray "." in the spoken text, so I saved a v2 that ends on the question alone. If you want to add your own take, put one short line before the question; I won't write it for you.
- **Your ElevenLabs voice.** The model and voice aren't confirmed yet, so the tags assume eleven_v3. Tell me which voice you're using, and which tags it reads out loud or overdoes.
- **No music or sound effects** are picked yet. Choose some, or tell me to pick for this project.
- **Your channel profile is still blank,** so the wording is neutral. Your audience and how you talk aren't filled in yet in `voice.md`.

**Saved:**
- Approved script: `scr_23cfd1d8f128` (v2), in `projects/live-acceptance-gameplay-to-export-b3e64a/scripts/v02/`. v1 (`scr_98d6807a5809`) is kept.
- Brief updated: game is Freedoom: Phase 2, plus the topic and the promise to the viewer.
- The choice of idea is saved to project memory.

Next, generate the narration in ElevenLabs and drop the audio in. After that I'll build the edit plan from the timestamps listed for each part of the script.

**[PASSED] same-chat planning used the report (frames inspected, tagged script approved)** — {"frames_described": 29, "script": "scr_23cfd1d8f128", "tags": ["curious", "excited", "thoughtful", "whispers"], "no_reupload": true}

**You:** Here's the narration — a local TTS stand-in, not ElevenLabs. Import, align and lock it if it matches.  
_attached: shiftws_live_standin.wav_

_tools: ToolSearch, narration_status, narration_lock, memory_save · 25s_

**Claude Code:** The take matches the script, so I'll lock it.

The stand-in narration matched the script, so it's imported, aligned and locked.

**How well it matches:** speech recognition heard all 68 words, a 98.5% match with script `scr_23cfd1d8f128`.
- The only difference is "Freedoom" heard as "freedom". The two sound the same, so that's expected and not an error.
- None of the audio tags were spoken aloud.
- All five sections play in order: hook 0.16–4.1s, setup 4.1–7.7s, build-up 7.7–13.6s, reveal 13.6–21.3s, ending 21.3–23.2s.
- Speech recognition can't judge delivery, so the whisper and the excited read are untested.

**Locked take:** `take_f1b7d1c57792` is at `voiceover/locked/take01_81b9d63801.wav`. The file's fingerprint was checked after locking and the audio is unchanged.

**Things to know before the edit:**
- **It's 23.3s, not 30s.** This voice reads at 177 words per minute, faster than the ~150 the script was timed for. I'd cut the Short to about 24–25s rather than pad it. Say if you'd rather I hold extra footage at the end instead.
- **It's quieter than the review target:** −20.9 LUFS against −14, with a true peak of −1.1 dB. Since the take is locked, I'll bring the overall level up in the mix, never by editing the take.
- **The ElevenLabs task stays open** because this is a stand-in. When the real ElevenLabs take arrives, I'll import it as take 2 against the same script. I'm also not recording any feedback on which tags work, since this wasn't your ElevenLabs voice.

Next is the edit plan, using the footage timestamps from each part of the script. It will have no music or sound effects unless you pick some or hand that choice to me.

**[PASSED] narration imported, aligned, locked** — {"takes": [[1, "locked", 1]], "match": 0.985}

**You:** Add my SFX folder <run>/My SFX to the channel catalog, suggest three hook impacts with audible midrange, approve the best for 'hook impact' and tell me why.

_tools: ToolSearch, catalog_add_folder, audio_find, audio_preview, audio_preview, audio_preview, job_wait, job_wait, job_wait, Read, Read, Read, audio_decide · 62s_

**Claude Code:** The three previews are rendered. I'm checking their waveforms before I pick one.

I couldn't tell the three apart from the waveforms: the narration dominates all three images, so they look identical. I'll pick on the measured numbers instead.

Your SFX folder is in the channel catalog (9 sounds), and **`pingus_splash` is approved as the hook impact**. I picked it on measurements, not by ear: I can't listen to audio, and the waveform images couldn't tell the three apart. Please play its preview before we build around it.

The catalog watches the folder, so it will pick up sounds you add or remove later.

**The three suggestions**, each previewed against your locked narration with the hit at 0.1s and the volume at −16 dB:

| | Sound | Midrange share | Impact confidence | Hit point | Length | Licence |
|---|---|---|---|---|---|---|
| ✅ | **pingus_splash** `ast_c62463df6749` | **0.91** | 0.60 | 0.07s | 0.42s | GPL-2.0+ |
| | fd_device-added `ast_4e38bbf1e77b` | 0.77 | 0.85 | 0.16s | 0.22s | CC-BY-SA-3.0 |
| | pingus_chink `ast_9b1239fa7f24` | 0.82 | 0.60 | 0.00s | 0.09s | GPL-2.0+ |

Midrange share is the fraction of the sound's energy between 250 Hz and 4 kHz. Impact confidence is the catalog's own guess that it's a hit sound.

I left out the camera shutter (mostly high frequencies), the 1.1s "complete" chime, the bass-heavy plop, and the bell and warning tones, which are notification beeps rather than hits.

**Why pingus_splash:**
- **It has the most midrange of the three (91%).** That's the range phone speakers actually play, so it should still be heard on a phone.
- **It's the only one with a real loudness reading** (−10.7 LUFS). The other two read −70, most likely because they're too short to measure, but they may simply be very quiet.
- **Its hit lands fast** (0.07s in) and it lasts long enough to register as a hit without running over the first word, which starts at 0.16s.
- **Why not the others:** `fd_device-added` scores higher on impact, but it's a desktop "device connected" tone with a later hit, so it would probably sound like an interface beep over a gunfight. `pingus_chink` is a tick under 0.1s long and would likely disappear under the voice.

**Before you lock it in:**
- **The name says "splash".** If it sounds watery, pick `fd_device-added` instead and I'll switch the approval.
- **The licence is GPL-2.0 or later.** Check that it works for your channel. The CC-BY-SA alternative needs a credit.
- **The approval is for this project only**, not channel-wide. Say if you want it as your channel default.

**Previews** are in `previews/`:
- `audio_ast_c62463df6749_100.wav` (splash)
- `audio_ast_4e38bbf1e77b_100.wav` (device-added)
- `audio_ast_9b1239fa7f24_100.wav` (chink)

The edit plan will put the splash on the opening close-range shot (~63s in the footage), at the start of the Short.

**[PASSED] approved audio recorded for this project** — {"approved": ["SFX approved: pingus_splash"]}

**You:** Build the edit plan with the approved hook sound, render a preview and check it.

_tools: ToolSearch, edit_plan_create, Read, edit_plan_revise, Grep, preview_render, job_wait, job_wait, ToolSearch, Glob, Glob, ToolSearch, assets_import, job_wait, video_analyze, job_wait, scene_frames, Read, Read, Read, Read, Read, Read, Read, Read, Read, edit_plan_revise, preview_render, job_wait, job_wait, assets_import, job_wait, video_analyze, job_wait, scene_frames, Read, Read, Read, Read, Read, memory_save, SendUserFile · 356s_

**Claude Code:** Next I'll confirm the source timings landed as intended.

The timings are right and nothing overlaps. Rendering the preview now.

The checks pass apart from loudness. Before reporting, I also want to look at the rendered frames.

The renderer doesn't keep frame images, so I'll register the preview as a review-only file to sample frames from it. It's not footage and won't be used in the edit.

The preview mostly looks right. One sync problem: the red screen flash plays about 1.5s before the narration says "flashes red", so I'm moving that clip later and re-rendering.

Re-rendered; now sampling the fixed section (2.5–14s) to confirm the red flash lands on the words.

The edit plan is built with the approved hook sound, and the preview is rendered and checked. The picture and timing work. One issue is still open: **the mix is 7 LU too quiet**, and fixing it needs your decision (see below).

**What I changed:** the automatically built plan didn't follow the script, so I corrected it before rendering:
- The hook opened on the 45s keypad shot instead of the close-range fight at 63s.
- Source footage from 80–84s appeared three times, and the ending reused unrelated footage from 11–14s.
- The splash was placed at 4.07s instead of at the hook.

After the first render, I found the red screen flash landing about 1.5s before the narration says "flashes red". I moved it and re-rendered. The current plan is v3.

**Timeline** (all clips confirmed by reading frames from the render):

| Time | Narration | On screen |
|---|---|---|
| 0–3s | "Nobody is touching the controls…" | Close-range fight with two monsters; **splash hits at 0.1s**, before the first word |
| 3–4s | "Not one button." | Firing at a monster in a room with a door |
| 4–7.7s | "…replays on its own title screen" | Freedoom: Phase 2 title card |
| 7.7–10.7s | "Four enemies waiting in one room" | Two soldiers in the middle, two tentacled monsters at the edges |
| 10.7–13.6s | "…firefight. The whole screen flashes red." | Red flash at about 13s, now on "flashes red" |
| 13.6–19.6s | "picks up a shotgun… walks into the next one" | Shotgun room, then the close-range hit |
| 19.6–21.3s | "…it plays this for you" | Title card again |
| 21.3–23.9s | "Would you have played it any better?" | Ending, into the screen-melt transition |

**Automated checks:** no errors, no silent gaps, no black frames. Captions stay inside the safe area, and the locked narration is confirmed unchanged. There's no music. The checker also flagged "point-blank" as differing from the script, but it's the same word, so that needs no action.

**The loudness issue:** the mix is −21.0 LUFS against your −14 target, with a −4.2 dB true peak. It's quiet because the voice track itself sits at about −21 LUFS. I can't edit the locked take, and turning up the game audio and splash would bury the voice. Your options:
1. **Accept it for this preview.** The take is only a stand-in, and the real ElevenLabs take will come in at its own level anyway.
2. **Allow a level change on the voice track in the final export.** The original file stays untouched; only the playback level changes. That breaks the current "fix it on other tracks only" rule, so I need your go-ahead.
3. **Lower the target** for this project.

**Limitations of the vertical crop:**
- The edges of the Freedoom logo are cut off.
- The game's pickup messages are cropped out, including "Got the pump-action shotgun!", so the shotgun line depends on the weapon itself being visible.

**Files:**
- Preview v3: `previews/Live_acceptance_—_gameplay_to_export_v03_preview.mp4` (`exp_e01823e5f09e`), sent above.
- Edit plan: `edit-plans/plan_v03.json`.
- To sample frames, I registered both preview renders as review-only files (`ast_b7634422ceac`, `ast_8b498806b636`). They aren't footage and won't be used in the edit.

Tell me which loudness option you want, and I'll do the final export and packaging.

**[PASSED] edit plan valid and contains the approved SFX** — {"plan": "plan_2925e7212e80", "sfx": [{"id": "sfx1", "asset_id": "ast_c62463df6749", "track": "A4", "at_frame": 3, "hit_offset_s": 0.07, "gain_db": -16.0, "purpose": ""}]}

**[BLOCKED] Premiere edit applied and read back** — {"reason": "Premiere not connected"}

**[BLOCKED] Premiere export verified by file probe + QC** — {"reason": "Premiere not connected"}