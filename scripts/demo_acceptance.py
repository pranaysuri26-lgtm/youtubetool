#!/usr/bin/env python3
"""Acceptance demo through the real engine API (what the desktop UI calls) with the real AI provider.

Steps (from the spec): 1 create project via chat · 2 import + analyze media · 3 script with ElevenLabs tags ·
4 remember an approved asset and a rejected alternative · 5 close + reopen the app · 6 resume without repeating
context · 7 retrieve an asset from another project through search · 8 targeted revision preserving other work.

    .venv/bin/python scripts/demo_acceptance.py --home /tmp/demo-home --media /root/shiftws-testmedia

Writes docs/demo/acceptance_run.md (transcript, tool calls, jobs, measurements). Local TTS (espeak-ng / macOS `say`)
stands in for ElevenLabs narration and is labeled as such.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable
LOG: list[str] = []


def log(s=""):
    print(s)
    LOG.append(s)


class Engine:
    def __init__(self, home, port, projects):
        self.home, self.port, self.projects, self.p = Path(home), port, projects, None

    def start(self):
        env = {**os.environ, "SHIFTWS_PROJECTS_ROOT": self.projects, "PYTHONPATH": str(REPO)}
        self.p = subprocess.Popen([PY, "-m", "shiftws.cli", "--home", str(self.home), "serve", "--port", str(self.port)],
                                  cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=open(self.home / "engine.stderr", "a"),
                                  start_new_session=True)
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/ping", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        self.token = (self.home / "api_token").read_text().strip()

    def stop(self):
        os.killpg(self.p.pid, signal.SIGTERM)
        self.p.wait(timeout=30)

    def api(self, path, body=None, method=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                     method=method or ("POST" if body is not None else "GET"),
                                     headers={"x-shiftws-token": self.token, "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())


def turn(eng, thread, text, attachments=None, timeout=900):
    log(f"\n**You:** {text}" + (f"  \n_attached: {', '.join(Path(a).name for a in attachments)}_" if attachments else ""))
    t0 = time.time()
    r = eng.api(f"/api/threads/{thread}/send", {"text": text, "attachments": attachments or []})
    seen = set()
    while time.time() - t0 < timeout:
        msgs = eng.api(f"/api/threads/{thread}/messages")["messages"]
        new = [m for m in msgs if m["id"] not in seen and m["created_at"] >= "" and m["meta"].get("turn_id") == r["turn_id"]
               and m["role"] == "assistant"]
        if new or any(m["role"] == "system" and "failed" in m["content"] and m["id"] not in seen for m in msgs[-3:]):
            break
        time.sleep(2)
    msgs = eng.api(f"/api/threads/{thread}/messages")["messages"]
    tools = [m["meta"]["tool"] for m in msgs if m["role"] == "tool" and m["meta"].get("turn_id") == r["turn_id"]]
    reply = next((m for m in reversed(msgs) if m["role"] == "assistant" and m["meta"].get("turn_id") == r["turn_id"]), None)
    log(f"\n_tools: {', '.join(tools) or 'none'} · {time.time() - t0:.0f}s_\n")
    log("**Claude Code:** " + (reply["content"] if reply else "(no reply)"))
    return reply, tools


def wait_jobs(eng, pid, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        js = eng.api(f"/api/jobs?project_id={pid}")["jobs"]
        if not any(j["status"] in ("queued", "running") for j in js):
            return js
        time.sleep(3)
    return eng.api(f"/api/jobs?project_id={pid}")["jobs"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="/tmp/shiftws-demo/home")
    ap.add_argument("--projects", default="/tmp/shiftws-demo/projects")
    ap.add_argument("--media", default=os.environ.get("SHIFTWS_TESTMEDIA", "/root/shiftws-testmedia"))
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--fresh", action="store_true")
    a = ap.parse_args()
    home = Path(a.home)
    if a.fresh and home.exists():
        shutil.rmtree(home.parent, ignore_errors=True)
    home.mkdir(parents=True, exist_ok=True)
    media = Path(a.media)
    lib = home.parent / "audio-library"
    if not lib.exists():
        shutil.copytree(media / "sfx", lib / "sfx")
        shutil.copytree(media / "music", lib / "music")
        shutil.copy(media / "provenance.json", lib / "provenance.json")
    eng = Engine(home, a.port, a.projects)
    eng.start()
    log(f"# Acceptance run — {time.strftime('%Y-%m-%d %H:%M')}\n")
    conn = eng.api("/api/connections")
    log(f"AI provider: claude_code → {json.dumps(conn['providers']['claude_code'])}")

    log("\n## 1. Create a video project through chat")
    turn(eng, "th_channel", "Create a narrated Short project titled 'Freedoom: Ambush Short' about the Freedoom attract-mode "
         "demo. Use the channel defaults. Reply with the project id.")
    proj = next(p for p in eng.api("/api/projects")["projects"] if "Ambush" in p["title"])
    pid = proj["id"]
    th = next(t["id"] for t in proj["threads"] if t["kind"] == "main")
    log(f"\n→ project {pid}, main thread {th}, length {proj['brief']['length_seconds']}s ({proj['brief'].get('length_source')})")

    log("\n## 2. Import media and analyze it")
    turn(eng, th, "Here is the gameplay folder. Register it and analyze the Freedoom Phase 2 recording (first 60 seconds).",
         attachments=[str(media / "footage" / "freedoom2_attract.mp4")])
    js = wait_jobs(eng, pid)
    log("→ jobs: " + ", ".join(f"{j['kind']}={j['status']}" for j in js))
    turn(eng, th, "Look at the first four uninspected gameplay frames after the title screen (view the images) and record "
         "what you see. Then tell me in two sentences what the strongest hook moment is, citing timestamps.")

    log("\n## 3. Save a script with ElevenLabs audio tags")
    reply, tools = turn(eng, th, "Write the script for this Short in five short sections (hook, setup, progression, payoff, "
                        "ending), about 25 seconds, based only on what you saw. Use my ElevenLabs tags. Approve it and show "
                        "me the text to paste into ElevenLabs.")
    ctx = eng.api(f"/api/projects/{pid}/context")
    s = ctx["latest_script"]
    sc = eng.api("/api/tools/script_get", {"_scope": {"project_id": pid}})["data"]
    log(f"\n→ script v{s['version']} {s['status']}, {s['word_count']} words; tagged starts: {sc['tagged_text'][:90]!r}")
    log(f"→ tags used: {sc['compat'].get('tags_used')}")
    # narration stand-in (labeled): local TTS of the clean text, dropped into the chat like a downloaded ElevenLabs file
    take = home.parent / "elevenlabs_download_standin.wav"
    tts = ["say", "-o", str(take.with_suffix(".aiff")), sc["clean_text"]] if shutil.which("say") else \
        ["espeak-ng", "-v", "en-us", "-s", "165", "-w", str(take), sc["clean_text"].replace("\n", " ")]
    subprocess.run(tts, check=True)
    if shutil.which("say"):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(take.with_suffix(".aiff")), str(take)], check=True)
    turn(eng, th, "Here's the narration I generated (this one is a local TTS stand-in, not ElevenLabs — mark it that way). "
         "Import it, check the alignment, and lock it if it matches.", attachments=[str(take)])
    wait_jobs(eng, pid)

    log("\n## 4. Remember an approved asset and a rejected alternative")
    turn(eng, th, f"Add my sound library at {lib} to the channel catalog. Then find a short impact with audible midrange for "
         "the hook, approve the best one for 'hook impact' and reject the runner-up as too thin.")
    mem = eng.api(f"/api/memory?project_id={pid}")["items"]
    for m in mem:
        if m["kind"] in ("approval", "rejection") and m["ref_type"] == "asset":
            log(f"→ memory: [{m['kind']}] {m['subject']} — {m['content']}")

    log("\n## 5. Close and reopen the app")
    eng.stop()
    log("→ engine stopped (app closed)")
    eng.start()
    log("→ engine restarted (app reopened)")

    log("\n## 6. Resume the same project without repeating context")
    rs = eng.api(f"/api/threads/{th}/resume")
    log(f"→ resume banner: {rs['summary']}  \n→ next: {rs['next_action']}")
    turn(eng, th, "Continue. Build the edit plan with the approved hook sound and render a preview.")
    wait_jobs(eng, pid)

    log("\n## 7. Retrieve an asset from another project through search")
    turn(eng, "th_channel", "Create a second narrated Short project titled 'Freedoom: Second Short'.")
    p2 = next(p for p in eng.api("/api/projects")["projects"] if "Second Short" in p["title"])
    th2 = next(t["id"] for t in p2["threads"] if t["kind"] == "main")
    sr = eng.api(f"/api/search?q={urllib.request.quote('impact approved Ambush Short')}&scope=all")["results"]
    for r in sr[:4]:
        log(f"→ search result: [{r['kind']}] {r['title']} · project {r['project_title']} · locator {r['locator']}")
    turn(eng, th2, "Find the impact sound we approved for the Ambush Short and tell me where it came from. Don't add it to "
         "anything yet.")

    log("\n## 8. Targeted revision, preserving unaffected work")
    before = eng.api(f"/api/projects/{pid}/context")
    take_before = before["latest_narration"]
    turn(eng, th, "Add a tense ambient underscore from the catalog under the narration, then replace that music with a "
         "different track without touching the narration. Render and check the result.")
    wait_jobs(eng, pid)
    after = eng.api(f"/api/projects/{pid}/context")
    plans = eng.api("/api/tools/edit_plan_get", {"_scope": {"project_id": pid}})["data"]
    jobs = eng.api(f"/api/jobs?project_id={pid}")["jobs"]
    renders = [j for j in jobs if j["kind"] == "render_preview" and j["status"] == "done"]
    log(f"→ plan v{plans['version']}: {plans['change_summary']} · changed {plans['changed_ids']}")
    for j in renders[:3]:
        log(f"→ render {j['id']}: reused {j['result'].get('reused_stages')} · {j['result'].get('summary')}")
    analysis = [j for j in jobs if j["kind"] in ("analysis_report", "analyze_video", "transcribe")]
    log(f"→ analysis jobs in project (unchanged by revisions): {len(analysis)}")
    qc = [j for j in jobs if j["kind"] == "qc" and j["status"] == "done"]
    if qc:
        log(f"→ latest QC: {qc[0]['result'].get('summary')} · narration bytes unchanged: "
            f"{qc[0]['result']['checks'].get('narration_lock', {}).get('bytes_unchanged')}")
    log(f"→ narration take before/after: {take_before} / {after['latest_narration']}")
    auto = eng.api(f"/api/automation?project_id={pid}")
    log(f"\n## Automation measured for this project\n{json.dumps(auto, indent=1)}")
    eng.stop()
    out = REPO / "docs" / "demo" / "acceptance_run.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(LOG) + "\n")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
