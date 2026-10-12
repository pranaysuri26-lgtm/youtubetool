"""Acceptance tests with REAL media (no mocked media/tools). Run: .venv/bin/pytest -q tests/
Requires the licensed test-media set: scripts/make_test_media.sh /root/shiftws-testmedia (or SHIFTWS_TESTMEDIA).
Network-dependent tests (yt-dlp) are skipped when the sample host is unreachable. The Premiere duplicate test uses
a clearly labeled STUB bridge to exercise the adapter's logic; it is not a Premiere integration test.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

MEDIA = Path(os.environ.get("SHIFTWS_TESTMEDIA", "/root/shiftws-testmedia"))
pytestmark = pytest.mark.skipif(not (MEDIA / "provenance.json").exists(), reason="test media missing")
BBB = "https://raw.githubusercontent.com/mediaelement/mediaelement-files/master/big_buck_bunny.mp4"  # CC-BY 3.0 Blender Foundation


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("shiftws")
    os.environ["SHIFTWS_PROJECTS_ROOT"] = str(root / "projects")
    from shiftws.workspace import open_workspace
    ws = open_workspace(root / "home")
    return {"root": root, "ws": ws, "state": {}}


def call(ws, name, pid=None, thread=None, **args):
    from shiftws import tools as T
    r = T.call(ws, name, args, scope={"origin": "test", "project_id": pid, "thread_id": thread})
    assert r["status"] != "error", (name, r["warnings"])
    return r


def run(ws, r_or_id):
    from shiftws import jobs as J
    jid = r_or_id if isinstance(r_or_id, str) else r_or_id["job_ids"][0]
    j = J.get(ws, jid)
    if j["status"] == "queued":
        j = J.run_inline(ws, j)
    assert j["status"] == "done", (j["kind"], j["error"], j["error_hint"])
    return j


def reopen(env):
    from shiftws.workspace import open_workspace
    env["ws"].db.close()
    env["ws"] = open_workspace(env["root"] / "home")
    return env["ws"]


# ---------------------------------------------------------------- 1. project, persistence, analysis

def test_create_project_and_persist_reopen(env):
    ws = env["ws"]
    r = call(ws, "project_create", title="Freedoom opening Short", format_preset="short_narrated", game="Freedoom")
    pid = r["data"]["project_id"]
    env["state"]["pid"] = pid
    env["state"]["main"] = r["data"]["main_thread_id"]
    call(ws, "project_set_preferences", pid, preferences={"analysis_mode": "standard"}, reason="test: project preference")
    ws = reopen(env)
    from shiftws.projects import get_project, pref, project_context
    p = get_project(ws, pid)
    assert p["brief"]["length_seconds"] == 30 and p["brief"]["length_source"].startswith("preset default")
    assert pref(ws, pid, "analysis_mode") == "standard"           # project > preset > channel
    assert pref(ws, pid, "audio_tags_default") is True             # channel default
    assert pref(ws, pid, "audio_selection") == "suggest_only_until_selected"
    ctx = project_context(ws, pid)
    assert ctx["where_we_stopped"]["next_action"].startswith("Write the script")


def test_import_dedupe_and_license_from_provenance(env):
    ws, pid = env["ws"], env["state"]["pid"]
    j = run(ws, call(ws, "assets_import", pid, paths=[str(MEDIA / "footage")]))
    assert len(j["result"]["imported"]) == 2
    j2 = run(ws, call(ws, "assets_import", pid, paths=[str(MEDIA / "footage" / "freedoom2_attract.mp4")]))
    assert list(j2["result"]["imported"].values())[0]["duplicate"] is True     # same bytes → same asset
    from shiftws.assets import project_assets
    vids = project_assets(ws, pid, kind="video")
    assert all(a["license"]["license"].startswith("BSD-3-Clause") and a["license"]["verified"] for a in vids)
    a = next(a for a in vids if "freedoom2" in a["title"])
    assert (a["fps_num"], a["fps_den"]) == (30, 1) and a["duration"] == pytest.approx(150.0, abs=0.1)
    env["state"]["fd2"] = a["id"]
    env["state"]["fd1"] = next(x["id"] for x in vids if "freedoom1" in x["title"])


def test_analysis_report_linked_to_chat(env):
    ws, pid = env["ws"], env["state"]["pid"]
    j = run(ws, call(ws, "analysis_report", pid, thread=env["state"]["main"], asset_ids=[env["state"]["fd2"]],
                     mode="standard", end=60.0))
    rep = ws.db.one("SELECT * FROM reports WHERE id=?", (j["result"]["report_id"],))
    assert rep["thread_id"] == env["state"]["main"] and Path(rep["path"]).exists()
    assert json.loads(rep["source_hashes_json"])[env["state"]["fd2"]]
    text = Path(rep["path"]).read_text()
    assert "SECTION 1" in text and "NOT YET INSPECTED" in text and "NOT exhaustive" in text
    msgs = ws.db.all("SELECT meta_json FROM messages WHERE thread_id=? AND meta_json LIKE '%report_id%'", (env["state"]["main"],))
    assert msgs, "report artifact card must be posted to the originating chat"


def test_recover_interrupted_analysis(env):
    """Crash mid-job: the first asset finished (checkpoint), the process died. After restart the job is requeued
    and resumes from the checkpoint without redoing the finished asset."""
    ws, pid = env["ws"], env["state"]["pid"]
    from shiftws import jobs as J
    j = J.enqueue(ws, "analysis_report", {"asset_ids": [env["state"]["fd2"], env["state"]["fd1"]], "mode": "quick",
                                          "end": 40.0}, project_id=pid, origin="test")
    ws.db.x("UPDATE jobs SET status='running', heartbeat_at='2020-01-01T00:00:00.000000Z', checkpoint_json=? WHERE id=?",
            (json.dumps({"done": [env["state"]["fd2"]], "notes": {}}), j["id"]))
    before = ws.db.one("SELECT COUNT(*) n FROM artifacts WHERE kind='crv' AND asset_id=?", (env["state"]["fd2"],))["n"]
    ws = reopen(env)
    assert j["id"] in J.recover(ws)
    done = J.run_inline(ws, J.get(ws, j["id"]))
    assert done["status"] == "done"
    after = ws.db.one("SELECT COUNT(*) n FROM artifacts WHERE kind='crv' AND asset_id=?", (env["state"]["fd2"],))["n"]
    assert after == before, "finished asset must not be re-analyzed after recovery"
    assert ws.db.one("SELECT COUNT(*) n FROM scenes WHERE asset_id=?", (env["state"]["fd1"],))["n"] > 0


def test_retrieve_scene_with_evidence(env):
    ws, pid = env["ws"], env["state"]["pid"]
    sc = call(ws, "scene_frames", pid, asset_id=env["state"]["fd2"], start=10, limit=1)["data"][0]
    # A vision model would write this after looking at the frame; the test writes it as that model would.
    call(ws, "scene_describe", pid, scene_id=sc["id"], description="first-person corridor with a green armor pickup",
         confidence=0.8, tags=["corridor", "armor"], rubric={"CATEGORY": "gameplay", "VISUAL QUALITY 1-5": "3"})
    hits = call(ws, "footage_find", pid, query="green armor corridor")["data"]
    h = next(x for x in hits if x["kind"] == "scene")
    assert h["asset_id"] == env["state"]["fd2"] and h["start"] is not None and h["confidence"] == 0.8
    assert h["basis"] == "vision description" and "armor" in h["evidence"]
    cov = call(ws, "coverage_status", pid, asset_id=env["state"]["fd2"])["data"]
    assert cov["frames_reviewed"] >= 1 and cov["complete"] is False


def test_no_evidence_investigation(env):
    ws, pid = env["ws"], env["state"]["pid"]
    with pytest.raises(AssertionError):   # confirmed without frame evidence is refused
        call(ws, "investigation_record", pid, question="Is there a Dead Eye-style slow-motion mechanic?",
             status="confirmed", evidence=[])
    r = call(ws, "investigation_record", pid, question="Is there a Dead Eye-style slow-motion mechanic?",
             status="no_evidence_in_inspected", note="Not seen in the inspected frames.")
    assert r["data"]["status"] == "no_evidence_in_inspected" and r["data"]["inspected"]
    rep = call(ws, "report_refresh", pid)["data"]
    assert "NO SUPPORTING EVIDENCE FOUND IN THE INSPECTED FOOTAGE (not proof the game lacks it)" in Path(rep["path"]).read_text()


def test_external_report_edit_reconciled(env):
    ws, pid = env["ws"], env["state"]["pid"]
    rep = ws.db.one("SELECT * FROM reports WHERE project_id=? AND status='current' AND kind='analysis'", (pid,))
    p = Path(rep["path"])
    p.write_text(p.read_text() + "\n### USER NOTE: the corridor ambush is the hook\n\n- edited outside the app\n")
    secs = call(ws, "report_sections", pid, query="corridor ambush hook")["data"]
    assert any("USER NOTE" in s["heading"] for s in secs)
    assert ws.db.one("SELECT status FROM reports WHERE id=?", (rep["id"],))["status"] == "superseded"


def test_multi_file_inventory(env):
    ws, pid = env["ws"], env["state"]["pid"]
    j = run(ws, call(ws, "analysis_report", pid, mode="inventory", asset_ids=[env["state"]["fd1"], env["state"]["fd2"]]))
    t = Path(j["result"]["path"]).read_text()
    assert "Recording inventory" in t and env["state"]["fd1"] in t and "Shorts candidates" in t


def test_dense_pass_and_skipped_cuts(env):
    ws, pid = env["ws"], env["state"]["pid"]
    cuts = call(ws, "skipped_cuts", pid, asset_id=env["state"]["fd2"], end=60.0)["data"]
    assert isinstance(cuts, list)
    j = run(ws, call(ws, "dense_pass", pid, asset_id=env["state"]["fd2"], start=10.0, end=12.0, fps=5))
    assert len(j["result"]["frames"]) == 10


# ---------------------------------------------------------------- 2. script + narration

SECTIONS = [{"label": "hook", "tagged": "The first room in this demo looks safe. It is not."},
            {"label": "setup", "tagged": "Watch the doorway on the left. [whispers] Nothing moves yet."},
            {"label": "progression", "tagged": "Then the player turns, and the corridor fills with enemies in seconds."},
            {"label": "payoff", "tagged": "That is the trick. The level teaches you to relax, then punishes you for it."},
            {"label": "ending", "tagged": "Next time you start a map, check behind you first. (source: freedoom wiki) 0:42"}]


def test_script_tags_by_default_and_exports(env):
    ws, pid = env["ws"], env["state"]["pid"]
    r = call(ws, "script_save", pid, sections=SECTIONS, title="The first room lies", approve=True)
    d = r["data"]
    tagged = Path(d["files"]["script_elevenlabs_tagged.txt"]).read_text()
    clean = Path(d["files"]["script_spoken_clean.txt"]).read_text()
    assert tagged.startswith("[curious]") and "[excited]" in tagged and "[whispers]" in tagged
    assert "[" not in clean and "0:42" not in tagged and "source:" not in tagged
    from shiftws.scripts_eleven import words, TAG_RE
    assert words(clean) == words(TAG_RE.sub(" ", tagged))
    for f in ("script_sections.json", "pronunciation_notes.md", "beat_plan.json"):
        assert Path(d["files"][f]).exists()
    env["state"]["clean"] = clean
    # revision without tags still gets tags automatically; feedback-marked tags are removed
    call(ws, "narration_tag_feedback", spoken_aloud=["whispers"])
    r2 = call(ws, "script_save", pid, sections=[{"label": s["label"], "tagged": s["tagged"]} for s in SECTIONS[:4]] +
              [{"label": "ending", "tagged": "Next time you start a map, check behind you first."}], change_note="revision")
    t2 = Path(r2["data"]["files"]["script_elevenlabs_tagged.txt"]).read_text()
    assert "[whispers]" not in t2 and t2.startswith("[curious]")
    call(ws, "script_approve", script_id=r2["data"]["script_id"])
    env["state"]["clean"] = Path(r2["data"]["files"]["script_spoken_clean.txt"]).read_text()


def test_narration_import_align_lock(env):
    ws, pid = env["ws"], env["state"]["pid"]
    wav = env["root"] / "standin.wav"
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "160", "-w", str(wav), env["state"]["clean"].replace("\n", " ")],
                   check=True)
    r = call(ws, "narration_import", pid, path=str(wav), synthetic_standin=True,
             generation_settings={"tool": "espeak-ng (local stand-in, NOT ElevenLabs)"})
    j = run(ws, r)
    assert j["result"]["match_ratio"] >= 0.9 and len(j["result"]["sections"]) == 5
    lk = call(ws, "narration_lock", pid, take_id=r["data"]["id"])["data"]
    assert lk["locked"] and lk["bytes_unchanged"]
    assert not os.access(lk["locked_path"], os.W_OK) or os.geteuid() == 0   # read-only (root bypasses perms)
    assert oct(os.stat(lk["locked_path"]).st_mode)[-3:] == "444"
    env["state"]["take"] = r["data"]["id"]


# ---------------------------------------------------------------- 3. audio catalog

def test_catalog_monitoring_and_suggest_only(env):
    ws, pid = env["ws"], env["state"]["pid"]
    lib = env["root"] / "library"
    shutil.copytree(MEDIA / "sfx", lib / "sfx")
    (lib / "music").mkdir()
    shutil.copy(MEDIA / "provenance.json", lib / "provenance.json")
    call(ws, "catalog_add_folder", path=str(lib), library="test-lib")
    shutil.copy(MEDIA / "music" / "pingus_sorcerer.ogg", lib / "music")      # user adds a song later
    from shiftws.catalog import scan_all
    res = scan_all(ws)[str(lib.resolve())]
    assert len(res["added"]) == 1
    n = ws.db.one("SELECT * FROM notifications WHERE kind='catalog.added' AND title LIKE '%pingus_sorcerer%'")
    assert n and "music" in n["body"] and "s ·" in n["body"]
    # visible from another project's chat
    p2 = call(ws, "project_create", title="Second video", format_preset="long_narrated")["data"]["project_id"]
    env["state"]["p2"] = p2
    found = call(ws, "audio_find", p2, request="music underscore", role="music")["data"]
    assert any("sorcerer" in f["title"] for f in found)
    # removal vs disconnected drive
    (lib / "sfx" / "pingus_plop.wav").unlink()
    assert len(scan_all(ws)[str(lib.resolve())]["removed"]) == 1
    # suggest-only: nothing enters a plan without selection
    plan = call(ws, "edit_plan_create", pid)["data"]
    assert plan["music"] == 0 and plan["sfx"] == 0


def test_approve_reject_and_cross_project_search(env):
    ws, pid = env["ws"], env["state"]["pid"]
    sfx = call(ws, "audio_find", pid, request="short bass impact with audible midrange", role="sfx")["data"]
    imp, alt = sfx[0]["asset_id"], sfx[1]["asset_id"]
    call(ws, "audio_decide", pid, asset_id=imp, status="approved", purpose="hook impact", note="punchy")
    call(ws, "audio_decide", pid, asset_id=alt, status="rejected", purpose="hook impact", note="too thin")
    env["state"]["impact"] = imp
    # from the other project: "find the impact we approved for the Freedoom Short"
    hits = ws.search("impact approved Freedoom Short", scope="all", kinds=["decision"])
    top = next(h for h in hits if h["locator"].get("ref_id") == imp)
    assert top["project_title"] == "Freedoom opening Short"
    lessons = call(ws, "lessons_find", env["state"]["p2"], query="hook impact sound")["data"]
    assert any(l["origin_project"] == "Freedoom opening Short" and "not a rule" in l["status"] for l in lessons)


# ---------------------------------------------------------------- 4. plan, render, QC, revisions

def test_plan_render_kinetic_captions_and_qc(env):
    ws, pid = env["ws"], env["state"]["pid"]
    plan = call(ws, "edit_plan_create", pid)["data"]
    assert plan["validation"]["valid"] and plan["sfx"] == 1 and plan["captions"] > 5
    j = run(ws, call(ws, "preview_render", pid))
    r = j["result"]
    assert (r["width"], r["height"]) == (1080, 1920) and r["captions"]["events"] > 10
    # yellow caption pixels at a word time inside the caption band
    from shiftws.editplan import get_plan
    p = get_plan(ws, pid)["plan"]
    cue = p["captions"]["cues"][2]
    t = (cue["words"][0]["start"] + 2) / 30
    png = env["root"] / "cap.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", r["path"], "-frames:v", "1", str(png)], check=True)
    from PIL import Image
    import numpy as np
    a = np.asarray(Image.open(png).convert("RGB").crop((0, int(1920 * 0.55), 1080, int(1920 * 0.72)))).astype(int)
    R, G, B = a[..., 0], a[..., 1], a[..., 2]
    yellow = int(((R > 220) & (G > 190) & (B < 60)).sum())
    black = int(((R < 25) & (G < 25) & (B < 25)).sum())
    assert yellow > 500 and black > 500, (yellow, black)
    q = run(ws, r["qc_job"])["result"]
    assert q["checks"]["decode_errors"] == [] and q["checks"]["narration_lock"]["bytes_unchanged"]
    assert not any(f["item"] == "bars" for f in q["flags"])
    env["state"]["export_id"] = r["export_id"]


def test_replace_song_preserves_vo_and_analysis(env):
    ws, pid = env["ws"], env["state"]["pid"]
    music = call(ws, "audio_find", pid, request="music", role="music")["data"][0]["asset_id"]
    n_analysis = ws.db.one("SELECT COUNT(*) n FROM jobs WHERE kind IN ('analyze_video','analysis_report','transcribe')")["n"]
    take_sha = ws.db.one("SELECT original_sha256 FROM narration_takes WHERE id=?", (env["state"]["take"],))["original_sha256"]
    call(ws, "audio_decide", pid, asset_id=music, status="approved", purpose="underscore")
    rv = call(ws, "edit_plan_revise", pid, operations=[{"op": "replace_music", "asset_id": music}], reason="add underscore")["data"]
    assert rv["invalidates"] == ["mix"] and rv["unchanged_clips"] > 0
    j = run(ws, call(ws, "preview_render", pid))
    assert j["result"]["reused_stages"] == ["picture"]
    from shiftws.editplan import get_plan
    p = get_plan(ws, pid)["plan"]
    assert p["narration"]["sha256"] == take_sha and p["narration"]["gain_db"] == 0.0 and p["narration"]["dst_in"] == 0
    assert ws.db.one("SELECT COUNT(*) n FROM jobs WHERE kind IN ('analyze_video','analysis_report','transcribe')")["n"] == n_analysis
    q = run(ws, j["result"]["qc_job"])["result"]
    assert q["checks"]["narration_lock"]["bytes_unchanged"]


def test_missed_commitment_reminder(env):
    ws, pid = env["ws"], env["state"]["pid"]
    call(ws, "commitment_record", pid, kind="hook", subject="Approved hook", content="User approved this opening",
         text="Nobody checks behind them in Freedoom.")
    rem = call(ws, "commitments_check", pid)["data"]
    assert rem and rem[0]["kind"] == "approved hook missing" and rem[0]["decision_id"]


def test_correction_supersedes(env):
    ws, pid = env["ws"], env["state"]["pid"]
    d = ws.remember(kind="fact", subject="Map name", content="The first map is E1M1", project_id=pid, provenance="test")
    call(ws, "correction_record", pid, memory_id=d["id"], correct="The map name is not known from the footage")
    active = [m["content"] for m in ws.memories(project_id=pid)]
    assert "The first map is E1M1" not in active
    ws = reopen(env)
    from shiftws.chat.engine import turn_retrieval
    from shiftws.chat.threads import get_thread
    txt = turn_retrieval(ws, get_thread(ws, env["state"]["main"]), "which map is this")
    assert "never repeat the wrong version" in txt and "E1M1" in txt   # listed only as the wrong version


def test_two_deliverables_share_analysis(env):
    ws, pid = env["ws"], env["state"]["pid"]
    d = call(ws, "deliverable_create", pid, title="Long cut", format_preset="long_no_commentary", length_seconds=40)["data"]
    n_scenes = ws.db.one("SELECT COUNT(*) n FROM scenes")["n"]
    plan = call(ws, "edit_plan_create", pid, deliverable_id=d["id"])["data"]
    assert plan["validation"]["valid"], plan["validation"]["errors"]
    from shiftws.editplan import get_plan
    p = get_plan(ws, pid, deliverable_id=d["id"])["plan"]
    assert (p["sequence"]["width"], p["sequence"]["height"]) == (1920, 1080) and p["narration"] is None
    assert ws.db.one("SELECT COUNT(*) n FROM scenes")["n"] == n_scenes   # no duplicate analysis


def test_upload_package_and_analytics(env):
    ws, pid = env["ws"], env["state"]["pid"]
    j = run(ws, call(ws, "upload_package_create", pid, tags=["freedoom"]))
    folder = Path(j["result"]["folder"])
    for f in ("description.txt", "title.txt", "captions.srt", "thumbnail_brief.md", "checklist.md", "package.json"):
        assert (folder / f).exists()
    assert "Credits:" in (folder / "description.txt").read_text()
    assert "never publishes" in (folder / "checklist.md").read_text()
    csvp = env["root"] / "retention.csv"
    csvp.write_text("Video position (%),Absolute audience retention (%)\n0,100\n25,70\n50,55\n75,40\n100,30\n")
    a = call(ws, "analytics_import", pid, csv_path=str(csvp))["data"]
    assert a["kind"] == "retention" and "views" in a["missing_fields"] and "ctr" in a["missing_fields"]
    rows = json.loads(ws.db.one("SELECT rows_json FROM performance_imports WHERE id=?", (a["import_id"],))["rows_json"])
    assert all("views" not in r for r in rows)     # never fabricated


def test_detect_disconnected_media_and_relink(env):
    ws, pid = env["ws"], env["state"]["pid"]
    ext = env["root"] / "external_drive"
    ext.mkdir()
    f = ext / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(MEDIA / "footage" / "freedoom1_attract.mp4"), "-t", "4",
                    "-c", "copy", str(f)], check=True)
    run(ws, call(ws, "assets_import", pid, paths=[str(f)]))
    moved = env["root"] / "moved"
    ext.rename(moved)                                      # "drive disconnected"
    from shiftws.assets import check_availability, relink_by_hash
    offline = check_availability(ws, pid)
    assert any(a["title"] == "clip" for a in offline)
    assert relink_by_hash(ws, [moved])
    assert not any(a["title"] == "clip" for a in check_availability(ws, pid))


def test_premiere_jobs_blocked_without_bridge_others_continue(env):
    ws, pid = env["ws"], env["state"]["pid"]
    ws.cfg.settings["premiere"]["mcp_command"] = ["definitely-not-installed-premiere-bridge"]
    from shiftws import jobs as J
    from shiftws.editplan import get_plan
    j = J.run_inline(ws, J.enqueue(ws, "premiere_apply", {"plan_id": get_plan(ws, pid)["id"]}, project_id=pid, force=True))
    assert j["status"] == "failed" and ("bridge" in j["error"].lower() or "not connected" in j["error"].lower())
    ok = run(ws, call(ws, "audio_preview", pid, asset_id=env["state"]["impact"], at_seconds=3.0))
    assert ok["status"] == "done"


def test_premiere_no_duplicate_after_timeout_STUB(env, tmp_path):
    """LOGIC test with a STUB bridge (not Premiere): the import succeeds but answers after the timeout.
    The job must inspect state, adopt the created sequence, and a retry must not import again."""
    ws, pid = env["ws"], env["state"]["pid"]
    state = tmp_path / "stub.json"
    os.environ.update({"STUB_STATE": str(state), "STUB_SLOW_IMPORT": "4"})
    ws.cfg.settings["premiere"]["mcp_command"] = [sys.executable, str(Path(__file__).with_name("stub_premiere_mcp.py"))]
    ws.cfg.settings["premiere"]["timeout"] = 2
    from shiftws import jobs as J
    from shiftws.editplan import get_plan
    pl = get_plan(ws, pid)
    j1 = J.run_inline(ws, J.enqueue(ws, "premiere_apply", {"plan_id": pl["id"]}, project_id=pid, force=True))
    j2 = J.run_inline(ws, J.enqueue(ws, "premiere_apply", {"plan_id": pl["id"]}, project_id=pid, force=True))
    s = json.loads(state.read_text())
    assert s["imports"] == 1, s
    assert len(s["sequences"]) == 1
    assert j1["status"] == "done" and j2["status"] == "done", (j1["error"], j2["error"])
    assert j1["result"]["readback"]["matches"]


def test_jobs_resume_after_restart_with_workers(env):
    ws, pid = env["ws"], env["state"]["pid"]
    from shiftws import jobs as J
    j = J.enqueue(ws, "audio_analyze", {"asset_id": env["state"]["impact"], "force": True}, project_id=pid, force=True)
    ws = reopen(env)
    w = J.Workers(ws, parallel=1)
    w.start()
    try:
        done = J.wait(ws, j["id"], timeout=60)
    finally:
        w.stop()
    assert done["status"] == "done"


# ---------------------------------------------------------------- 5. chat (deterministic offline provider)

def test_chat_operates_project_offline_provider(env):
    """No AI here on purpose: proves chat requests run real jobs via the shared tool layer. The Claude Code provider
    is exercised by tests/test_claude_provider.py (opt-in, uses the user's own CLI login)."""
    ws, pid = env["ws"], env["state"]["pid"]
    from shiftws.chat.engine import send
    from shiftws.chat.threads import messages
    th = env["state"]["main"]
    r = send(ws, th, "status", provider="offline", wait=True)
    assert "Next:" in r["text"]
    r = send(ws, th, "sfx short impact", provider="offline", wait=True)
    assert "fd_" in r["text"] or "pingus" in r["text"]
    r = send(ws, th, "find armor", provider="offline", wait=True)
    assert "vision description" in r["text"]
    assert any(m["role"] == "assistant" for m in messages(ws, th))
