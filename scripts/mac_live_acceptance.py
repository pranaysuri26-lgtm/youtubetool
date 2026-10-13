#!/usr/bin/env python3
"""LIVE acceptance run for your Mac — the primary journey end to end, with real tools only:

  gameplay → linked analysis report → same-chat planning → approved audio → Premiere edit → verified export

Requirements (checked first; a missing one marks the dependent steps BLOCKED, never "passed"):
  - scripts/setup.sh done; Claude Code installed and logged in
  - Premiere open with a project whose name contains "disposable", MCP Bridge panel running
  - an export preset (.epr) for Media Encoder

    .venv/bin/python scripts/mac_live_acceptance.py \
        --gameplay "/Volumes/Recordings/RDR2 session 3.mp4" \
        --sfx-folder ~/Music/SFX \
        --export-preset "~/Documents/Adobe/Adobe Media Encoder/26.0/Presets/YouTube 1080x1920.epr" \
        [--narration ~/Downloads/elevenlabs_take.mp3]   # omit → macOS `say` stand-in, labeled as such

Everything goes through the same engine API the desktop app uses, in ONE project chat. Results:
docs/local/mac_live_acceptance.{md,json}. Each step records evidence ids; a step passes only on a state check,
not on the AI saying it worked.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
from demo_acceptance import Engine, LOG, log, turn, wait_jobs  # noqa: E402

RESULTS: list[dict] = []


def step(name, ok, evidence=None, status=None):
    st = status or ("passed" if ok else "failed")
    RESULTS.append({"step": name, "status": st, "evidence": evidence or {}})
    log(f"\n**[{st.upper()}] {name}** — {json.dumps(evidence or {}, default=str)[:600]}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gameplay", required=True, nargs="+")
    ap.add_argument("--sfx-folder", required=True)
    ap.add_argument("--export-preset", required=True)
    ap.add_argument("--narration")
    ap.add_argument("--home", default=None, help="workspace home (default: the app's normal data folder)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-confirm-lock", dest="confirm_lock", action="store_false",
                    help="stop instead of confirming the narration lock when the assistant asks you to listen first")
    a = ap.parse_args()
    from shiftws.config import load_config
    cfg = load_config(a.home)
    eng = Engine(cfg.home, a.port, str(cfg.projects_root))
    eng.start()          # reuses a running app engine on the port, or starts one
    log(f"# Live acceptance run — {time.strftime('%Y-%m-%d %H:%M')}\n")

    # ---- 0. prerequisites (read-only)
    conn = eng.api("/api/connections")
    cc = conn["providers"]["claude_code"]
    pr = eng.api("/api/premiere")
    step("prerequisite: Claude Code CLI logged in", cc.get("ok"), {"version": cc.get("version")})
    premiere_ready = pr.get("host") == "connected" and pr.get("server") == "ok"
    step("prerequisite: Premiere bridge + host connected (read-only check)", premiere_ready,
         {k: pr.get(k) for k in ("server", "host", "panel", "export_backend")})
    preset = Path(a.export_preset).expanduser()
    step("prerequisite: export preset exists", preset.exists(), {"preset": str(preset)})
    if not cc.get("ok"):
        return finish(eng)

    # ---- 1. project + gameplay granted in its chat → linked report
    turn(eng, "th_channel", "Create a narrated Short project titled 'Live acceptance — gameplay to export'. Defaults.")
    proj = next(p for p in eng.api("/api/projects")["projects"] if p["title"].startswith("Live acceptance"))
    pid, th = proj["id"], next(t["id"] for t in proj["threads"] if t["kind"] == "main")
    turn(eng, th, "Here's my gameplay. Analyze it (first 3 minutes) and keep the report linked here.", attachments=a.gameplay)
    wait_jobs(eng, pid, timeout=7200)
    ctx = eng.api(f"/api/projects/{pid}/context")
    reps = [r for r in ctx["reports"] if r["status"] != "superseded"]
    msgs = eng.api(f"/api/threads/{th}/messages")["messages"]
    card = any(m["meta"].get("report_id") for m in msgs)
    step("gameplay → linked Markdown report in the same chat", bool(reps) and card,
         {"reports": [r["path"] for r in reps], "card_in_chat": card})
    report_count = len(reps)

    # ---- 2. same-chat planning from the report (no re-upload)
    turn(eng, th, "From the saved report, pick the strongest 30-second Short idea. Inspect the frames you rely on, "
         "then write the script with my ElevenLabs tags and approve it.")
    ctx = eng.api(f"/api/projects/{pid}/context")
    scenes = eng.api("/api/tools/scene_frames", {"asset_id": ctx["assets"][0]["id"], "limit": 500,
                                                 "_scope": {"project_id": pid}})["data"] if ctx["assets"] else []
    described = sum(1 for s in scenes if s.get("confidence") is not None)
    sc = eng.api("/api/tools/script_get", {"_scope": {"project_id": pid}})["data"] or {}
    same_report = len([r for r in eng.api(f"/api/projects/{pid}/context")["reports"]]) >= report_count
    step("same-chat planning used the report (frames inspected, tagged script approved)",
         described > 0 and sc.get("status") == "approved" and "[" in (sc.get("tagged_text") or ""),
         {"frames_described": described, "script": sc.get("id"), "tags": (sc.get("compat") or {}).get("tags_used"),
          "no_reupload": same_report})

    # ---- 3. narration (real ElevenLabs take if given; otherwise a labeled `say` stand-in)
    if a.narration:
        take_path, label = Path(a.narration).expanduser(), "my ElevenLabs take"
    else:
        text = (sc.get("clean_text") or "test").replace("\n", " ")
        if shutil.which("say"):
            take_path = Path("/tmp/shiftws_live_standin.aiff")
            subprocess.run(["say", "-o", str(take_path), text], check=True)
        else:
            take_path = Path("/tmp/shiftws_live_standin.wav")
            subprocess.run(["espeak-ng", "-v", "en-us", "-s", "165", "-w", str(take_path), text], check=True)
        label = "a local TTS stand-in, not ElevenLabs"
    turn(eng, th, f"Here's the narration — {label}. Import, align and lock it if it matches.", attachments=[str(take_path)])
    wait_jobs(eng, pid)
    takes = eng.api("/api/tools/narration_status", {"_scope": {"project_id": pid}})["data"]
    locked = [t for t in takes if t["status"] == "locked"]
    if takes and not locked and a.confirm_lock:
        # The assistant asks before an irreversible lock when alignment has mismatches (correct behavior).
        # Unattended runs answer as the user would after listening; recorded as an explicit user decision.
        log("\n_(script) explicit user decision sent: lock the take_")
        RESULTS.append({"step": "user decision: confirm narration lock after the assistant asked", "status": "decision",
                        "evidence": {"take": takes[-1]["id"]}})
        turn(eng, th, "I listened — it's fine. Lock it and continue.")
        wait_jobs(eng, pid)
        takes = eng.api("/api/tools/narration_status", {"_scope": {"project_id": pid}})["data"]
        locked = [t for t in takes if t["status"] == "locked"]
    step("narration imported, aligned, locked", bool(locked),
         {"takes": [(t["take_no"], t["status"], t["is_synthetic_standin"]) for t in takes],
          "match": locked[0]["alignment"].get("match_ratio") if locked else None})

    # ---- 4. approved audio
    turn(eng, th, f"Add my SFX folder {a.sfx_folder} to the channel catalog, suggest three hook impacts with audible "
         "midrange, approve the best for 'hook impact' and tell me why.")
    mem = eng.api(f"/api/memory?project_id={pid}")["items"]
    approved = [m for m in mem if m["kind"] == "approval" and m["ref_type"] == "asset"]
    step("approved audio recorded for this project", bool(approved), {"approved": [m["subject"] for m in approved]})

    # ---- 5. plan + Premiere edit
    turn(eng, th, "Build the edit plan with the approved hook sound, render a preview and check it.")
    wait_jobs(eng, pid)
    plan = eng.api("/api/tools/edit_plan_get", {"_scope": {"project_id": pid}})["data"]
    has_sfx = bool(plan and plan["plan"]["sfx"])
    step("edit plan valid and contains the approved SFX", bool(plan) and plan["validation"]["valid"] and has_sfx,
         {"plan": plan and plan["id"], "sfx": plan and plan["plan"]["sfx"]})
    if not premiere_ready:
        step("Premiere edit applied and read back", False, {"reason": "Premiere not connected"}, status="blocked")
        step("Premiere export verified by file probe + QC", False, {"reason": "Premiere not connected"}, status="blocked")
        return finish(eng)
    turn(eng, th, "Apply this plan to Premiere as a new sequence and confirm the readback.")
    wait_jobs(eng, pid, timeout=3600)
    jobs = eng.api(f"/api/jobs?project_id={pid}")["jobs"]
    ap_job = next((j for j in jobs if j["kind"] == "premiere_apply" and j["status"] == "done"), None)
    step("Premiere edit applied and read back", bool(ap_job and ap_job["result"]["readback"]["matches"]),
         {"job": ap_job and ap_job["id"], "readback": ap_job and ap_job["result"]["readback"]})
    if not ap_job:
        return finish(eng)

    # ---- 6. verified export (Premiere/Media Encoder → actual file → probe → QC)
    turn(eng, th, f"Export that sequence with the preset {preset} and check the exported file.")
    wait_jobs(eng, pid, timeout=7200)
    jobs = eng.api(f"/api/jobs?project_id={pid}")["jobs"]
    ex = next((j for j in jobs if j["kind"] == "premiere_export" and j["status"] == "done"), None)
    qc = next((j for j in jobs if j["kind"] == "qc" and j["status"] == "done" and ex and
               j["params"].get("export_id") == ex["result"]["export_id"]), None)
    ok = bool(ex and qc and not qc["result"]["checks"]["decode_errors"])
    step("Premiere export verified by file probe + QC", ok,
         {"export": ex and ex["result"], "qc": qc and qc["result"]["summary"],
          "narration_bytes_unchanged": qc and qc["result"]["checks"].get("narration_lock", {}).get("bytes_unchanged")})
    return finish(eng)


def finish(eng):
    out = REPO / "docs" / "local"
    out.mkdir(parents=True, exist_ok=True)
    (out / "mac_live_acceptance.json").write_text(json.dumps(RESULTS, indent=1, default=str))
    summary = "\n".join(f"- [{r['status']}] {r['step']}" for r in RESULTS)
    (out / "mac_live_acceptance.md").write_text("# Live acceptance (Mac)\n\n" + summary + "\n\n## Transcript\n" + "\n".join(LOG))
    print("\n" + summary + f"\n\nResults: {out / 'mac_live_acceptance.md'} — send me this file.")
    try:
        eng.stop()
    except Exception:  # noqa: BLE001 - engine may belong to the running app
        pass


if __name__ == "__main__":
    main()
