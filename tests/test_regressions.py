"""Regression tests for issues found in the real-AI acceptance run and review:
audio-import routing, skill/research execution, deliverable scoping end to end, script-worded captions.
Real media and real tools; the only stub is the clearly labeled Premiere bridge stub for the Premiere-scoping check."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

MEDIA = Path(os.environ.get("SHIFTWS_TESTMEDIA", "/root/shiftws-testmedia"))
pytestmark = pytest.mark.skipif(not (MEDIA / "provenance.json").exists(), reason="test media missing")

SCRIPT = [{"label": "hook", "tagged": "Freedoom's demo walks right into a shotgun fight."},
          {"label": "setup", "tagged": "It starts easy, with armor and health on the floor."},
          {"label": "progression", "tagged": "Then an enemy appears, and health drops into the nineties."},
          {"label": "payoff", "tagged": "A grey hall waits with three more enemies."},
          {"label": "ending", "tagged": "And it is only the title screen."}]


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("regr")
    os.environ["SHIFTWS_PROJECTS_ROOT"] = str(root / "projects")
    from shiftws.workspace import open_workspace
    ws = open_workspace(root / "home")
    return {"root": root, "ws": ws, "s": {}}


def call(ws, name, pid=None, thread=None, **a):
    from shiftws import tools as T
    r = T.call(ws, name, a, scope={"origin": "test", "project_id": pid, "thread_id": thread})
    assert r["status"] != "error", (name, r["warnings"])
    return r


def run(ws, r):
    from shiftws import jobs as J
    j = J.get(ws, r if isinstance(r, str) else r["job_ids"][0])
    if j["status"] == "queued":
        j = J.run_inline(ws, j)
    assert j["status"] == "done", (j["kind"], j["error"], j["error_hint"])
    return j


def drain(ws):
    from shiftws import jobs as J
    while (j := J.claim(ws)):
        J.run_job(ws, j)


def tts(path: Path, text: str, rate=160):
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", str(rate), "-w", str(path), text], check=True)
    return path


def tone(path: Path, seconds: float, freq=220):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                    "-ac", "2", str(path)], check=True)
    return path


# ---------------------------------------------------------------- setup: narrated project with footage + script

def test_setup_project(env):
    ws = env["ws"]
    r = call(ws, "project_create", title="Routing Short", format_preset="short_narrated")
    pid, th = r["data"]["project_id"], r["data"]["main_thread_id"]
    env["s"].update(pid=pid, th=th)
    run(ws, call(ws, "analysis_report", pid, paths=[str(MEDIA / "footage" / "freedoom2_attract.mp4")], mode="quick", end=40.0))
    sv = call(ws, "script_save", pid, sections=SCRIPT, title="Routing test", approve=True)["data"]
    env["s"]["clean"] = Path(sv["files"]["script_spoken_clean.txt"]).read_text()


# ---------------------------------------------------------------- audio routing

def test_route_standin_narration_from_message_wording(env):
    ws, pid, th = env["ws"], env["s"]["pid"], env["s"]["th"]
    from shiftws.chat.engine import send
    wav = tts(env["root"] / "download_take.wav", env["s"]["clean"].replace("\n", " "))
    send(ws, th, "Here's my narration — a local TTS stand-in, not ElevenLabs.", attachments=[str(wav)], provider="offline", wait=True)
    take = ws.db.one("SELECT * FROM narration_takes WHERE project_id=?", (pid,))
    assert take and take["is_synthetic_standin"] == 1
    att = ws.db.one("SELECT handled_as FROM attachments WHERE path=?", (str(wav),))
    assert att["handled_as"] == "narration"
    drain(ws)
    env["s"]["take"] = take["id"]
    env["s"]["take_dur"] = take["duration"]


def test_reimport_same_file_corrects_labels_not_audio(env):
    ws, pid = env["ws"], env["s"]["pid"]
    ws.db.update("narration_takes", {"id": env["s"]["take"]}, {"is_synthetic_standin": 0})   # simulate the old bug
    r = call(ws, "narration_import", pid, path=str(env["root"] / "download_take.wav"), synthetic_standin=True)
    assert r["data"]["id"] == env["s"]["take"]                                      # no second take
    assert ws.db.one("SELECT is_synthetic_standin FROM narration_takes WHERE id=?", (env["s"]["take"],))["is_synthetic_standin"] == 1
    assert ws.db.one("SELECT COUNT(*) n FROM narration_takes WHERE project_id=?", (pid,))["n"] == 1


def test_replacement_take_routed_without_wording_and_lock_kept(env):
    ws, pid, th = env["ws"], env["s"]["pid"], env["s"]["th"]
    call(ws, "narration_lock", pid, take_id=env["s"]["take"])
    from shiftws.chat.engine import send
    wav2 = tts(env["root"] / "Untitled 42.wav", env["s"]["clean"].replace("\n", " "), rate=150)   # similar length, no hint
    send(ws, th, "here", attachments=[str(wav2)], provider="offline", wait=True)
    rows = ws.db.all("SELECT take_no, status FROM narration_takes WHERE project_id=? ORDER BY take_no", (pid,))
    assert [r["take_no"] for r in rows] == [1, 2]
    assert rows[0]["status"] == "locked"                                           # never auto-replaced
    assert rows[1]["status"] in ("imported", "aligned")
    drain(ws)


def test_short_sfx_goes_to_channel_catalog_not_timeline(env):
    ws, pid, th = env["ws"], env["s"]["pid"], env["s"]["th"]
    from shiftws.chat.engine import send
    sfx = env["root"] / "chat_drop" / "punch.wav"
    sfx.parent.mkdir()
    shutil.copy(MEDIA / "sfx" / "pingus_splash.wav", sfx)
    send(ws, th, "", attachments=[str(sfx)], provider="offline", wait=True)
    a = ws.db.one("SELECT * FROM media_assets WHERE current_path=?", (str(sfx.resolve()),))
    assert a and json.loads(a["tags_json"]) == ["sfx"] and a["library"] == "chat imports" and a["catalog_state"] == "present"
    assert ws.db.one("SELECT 1 FROM notifications WHERE kind='catalog.added' AND ref_id=?", (a["id"],))
    assert not ws.db.one("SELECT 1 FROM narration_takes WHERE asset_id=?", (a["id"],))
    plan = call(ws, "edit_plan_create", pid)["data"]
    assert plan["sfx"] == 0 and plan["music"] == 0                                  # suggest-only
    env["s"]["sfx_asset"] = a["id"]


def test_song_wording_routes_to_music(env):
    ws, th = env["ws"], env["s"]["th"]
    from shiftws.chat.engine import send
    song = env["root"] / "chat_drop" / "Track 07.ogg"
    shutil.copy(MEDIA / "music" / "pingus_sorcerer.ogg", song)
    send(ws, th, "here's a song I like for this channel", attachments=[str(song)], provider="offline", wait=True)
    a = ws.db.one("SELECT tags_json FROM media_assets WHERE current_path=?", (str(song.resolve()),))
    assert json.loads(a["tags_json"]) == ["music"]


def test_ambiguous_audio_asks_one_question(env):
    ws, pid, th = env["ws"], env["s"]["pid"], env["s"]["th"]
    from shiftws.chat.engine import send
    f = tone(env["root"] / "chat_drop" / "audio file.wav", 60.0)                     # 60 s, no hints, unlike the take
    send(ws, th, "", attachments=[str(f)], provider="offline", wait=True)
    q = ws.db.one("SELECT meta_json FROM messages WHERE thread_id=? AND meta_json LIKE '%audio_route%' ORDER BY created_at DESC", (th,))
    meta = json.loads(q["meta_json"])
    assert meta["routes"] == ["narration", "music", "sfx"] and meta["file"] == str(f)
    assert not ws.db.one("SELECT 1 FROM media_assets WHERE current_path=?", (str(f.resolve()),))   # not imported yet
    r = call(ws, "audio_route", pid, thread=th, path=str(f), route="music")             # the user's answer
    assert r["data"]["route"] == "music"


def test_no_commentary_never_gets_narration(env):
    ws = env["ws"]
    r = call(ws, "project_create", title="Story cut", format_preset="long_no_commentary")
    pid, th = r["data"]["project_id"], r["data"]["main_thread_id"]
    from shiftws.chat.engine import send
    f = tone(env["root"] / "chat_drop" / "vo maybe.wav", 25.0, 330)
    send(ws, th, "this is the voiceover", attachments=[str(f)], provider="offline", wait=True)
    assert not ws.db.one("SELECT 1 FROM narration_takes WHERE project_id=?", (pid,))
    q = json.loads(ws.db.one("SELECT meta_json FROM messages WHERE thread_id=? AND meta_json LIKE '%audio_route%'", (th,))["meta_json"])
    assert q["routes"] == ["music", "sfx"]


# ---------------------------------------------------------------- skills and research

def test_skill_list_and_run_saved_as_chat_artifacts(env):
    ws, pid, th = env["ws"], env["s"]["pid"], env["s"]["th"]
    skills = {s["skill"]: s for s in call(ws, "skill_list")["data"]}
    assert skills["yt-plan"]["instructions_only"] and "hookscore.py" in skills["yt-script"]["tools"]
    assert "yt-viral" in call(ws, "skill_read", skill="yt-viral")["data"]
    r = call(ws, "skill_run", pid, thread=th, skill="yt-script", tool="hookscore.py",
             text="Freedoom's demo walks right into a shotgun fight.\nNobody checks behind them.")
    assert Path(r["data"]["path"]).exists() and r["data"]["result"]
    assert ws.db.one("SELECT 1 FROM messages WHERE thread_id=? AND meta_json LIKE ?", (th, f'%{r["data"]["path"]}%'))
    v = call(ws, "skill_run", pid, thread=th, skill="yt-viral", tool="swipe.py",
             data=[{"channel": "A", "title": "x", "views": 900000, "url": "u1", "duration": 60},
                   {"channel": "A", "title": "y", "views": 30000, "url": "u2", "duration": 60},
                   {"channel": "A", "title": "z", "views": 28000, "url": "u3", "duration": 60}])["data"]
    assert v["revision"] == 1 and v["result"]
    asset = ws.db.one("SELECT asset_id FROM narration_takes WHERE project_id=? ORDER BY take_no LIMIT 1", (pid,))["asset_id"]
    d = call(ws, "skill_run", pid, skill="yt-edit", tool="deadair.py", asset_id=asset)
    assert "nothing was cut" in d["warnings"][0]
    hits = ws.search("hookscore", scope="project", project_id=pid, kinds=["artifact"])
    assert hits
    from shiftws import tools as T
    bad = T.call(ws, "skill_run", {"skill": "yt-seo", "tool": "seo.py"}, scope={"project_id": pid})
    assert bad["status"] == "error"


def test_research_sources_validated_and_listed(env):
    ws, pid = env["ws"], env["s"]["pid"]
    call(ws, "sources_register", pid, title="Freedoom 0.13 release notes", url="https://freedoom.github.io/",
         category="reported", published="2024-01-01", visibility="public", notes="version on the title card")
    from shiftws import tools as T
    assert T.call(ws, "sources_register", {"title": "x", "category": "rumor"}, scope={"project_id": pid})["status"] == "error"
    rows = call(ws, "research_list", pid)["data"]
    assert rows[0]["observed"] == "reported"
    assert ws.search("release notes title card", scope="project", project_id=pid, kinds=["source"])


# ---------------------------------------------------------------- captions use script words

def test_captions_use_approved_script_words(env):
    ws, pid = env["ws"], env["s"]["pid"]
    call(ws, "narration_lock", pid, take_id=env["s"]["take"])
    call(ws, "audio_decide", pid, asset_id=env["s"]["sfx_asset"], status="approved", purpose="hook impact")
    plan = call(ws, "edit_plan_create", pid)["data"]
    from shiftws.editplan import get_plan
    p = get_plan(ws, pid)["plan"]
    words = [w["w"] for c in p["captions"]["cues"] for w in c["words"]]
    assert words[0] == "Freedoom's"                         # the recognizer hears "freedom's"
    assert "nineties." in words and "90s" not in " ".join(words)
    assert "approved script words" in p["captions"]["source"]
    rv = call(ws, "edit_plan_revise", pid, operations=[{"op": "caption_fix", "find": "grey", "replace": "gray"}],
              reason="US spelling")["data"]
    assert rv["invalidates"] == ["picture"]
    p2 = get_plan(ws, pid)["plan"]
    assert "gray" in [w["w"] for c in p2["captions"]["cues"] for w in c["words"]]


# ---------------------------------------------------------------- deliverables end to end

def test_deliverables_scoped_through_render_qc_package_and_premiere(env, tmp_path):
    ws, pid = env["ws"], env["s"]["pid"]
    from shiftws.projects import get_deliverable
    d_short = get_deliverable(ws, None, pid)["id"]
    d_long = call(ws, "deliverable_create", pid, title="Long cut", format_preset="long_no_commentary", length_seconds=20)["data"]["id"]
    call(ws, "edit_plan_create", pid, deliverable_id=d_long)
    from shiftws.editplan import get_plan
    pl_s, pl_l = get_plan(ws, pid, deliverable_id=d_short), get_plan(ws, pid, deliverable_id=d_long)
    assert pl_s["deliverable_id"] == d_short and pl_l["deliverable_id"] == d_long
    assert call(ws, "edit_plan_get", pid, deliverable_id=d_long)["data"]["id"] == pl_l["id"]
    assert call(ws, "edit_plan_validate", pid, deliverable_id=d_long)["data"]["valid"]
    # render + QC per deliverable
    exp = {}
    for d in (d_short, d_long):
        j = run(ws, call(ws, "preview_render", pid, deliverable_id=d))
        run(ws, j["result"]["qc_job"])
        exp[d] = j["result"]["export_id"]
    rows = {e["id"]: e for e in ws.db.all("SELECT * FROM exports WHERE project_id=?", (pid,))}
    assert rows[exp[d_short]]["deliverable_id"] == d_short and rows[exp[d_long]]["deliverable_id"] == d_long
    info_l = json.loads(rows[exp[d_long]]["probe_json"])
    assert (info_l["width"], info_l["height"]) == (1920, 1080)
    q = call(ws, "qc_run", pid, deliverable_id=d_long)
    from shiftws import jobs as J
    assert J.get(ws, q["job_ids"][0])["params"]["export_id"] == exp[d_long]
    # packages are separate and refer to their own export/deliverable
    pk_l = run(ws, call(ws, "upload_package_create", pid, deliverable_id=d_long))["result"]
    pk_s = run(ws, call(ws, "upload_package_create", pid, deliverable_id=d_short))["result"]
    assert pk_l["export_id"] == exp[d_long] and pk_l["deliverable_id"] == d_long
    assert pk_s["export_id"] == exp[d_short] and Path(pk_s["folder"]).parent != Path(pk_l["folder"]).parent
    assert pk_l["chapters"]["lines"] == [] and (Path(pk_l["folder"]) / "captions.srt").read_text() == ""
    assert "Freedoom's demo" not in (Path(pk_l["folder"]) / "description.txt").read_text()
    # Premiere: dry runs name sequences per deliverable; a (STUB) apply records the deliverable on the sequence
    names = {d: call(ws, "premiere_apply_plan", pid, deliverable_id=d)["data"]["sequence_name"] for d in (d_short, d_long)}
    assert names[d_short] != names[d_long] and "Long cut" in names[d_long]
    os.environ.update({"STUB_STATE": str(tmp_path / "stub.json")})
    os.environ.pop("STUB_SLOW_IMPORT", None)
    ws.cfg.settings["premiere"]["mcp_command"] = [sys.executable, str(Path(__file__).with_name("stub_premiere_mcp.py"))]
    ws.cfg.settings["premiere"]["timeout"] = 30
    a = run(ws, call(ws, "premiere_apply_plan", pid, dry_run=False, deliverable_id=d_long))
    assert a["result"]["deliverable_id"] == d_long and a["result"]["plan_id"] == pl_l["id"]
    from shiftws.projects import project_context
    st = {d["id"]: d for d in project_context(ws, pid)["deliverables"]}
    assert st[d_long]["sequence"]["sequence_id"] == a["result"]["sequence_id"] and st[d_short]["sequence"] is None
    assert st[d_long]["export"]["id"] == exp[d_long] and st[d_short]["narration"]["status"] == "locked"
    assert st[d_short]["takes_awaiting_review"] == [2] and st[d_long]["narration"] is None


def test_premiere_export_verified_by_file_and_qc_STUB(env):
    """LOGIC test with the STUB bridge: the export call returns before the file exists; the job must wait for a
    complete file, probe it, register it under the right deliverable/plan, and QC the actual file."""
    ws, pid = env["ws"], env["s"]["pid"]
    from shiftws.projects import get_deliverable
    d_long = ws.db.one("SELECT id FROM deliverables WHERE project_id=? AND title='Long cut'", (pid,))["id"]
    seq = ws.db.one("SELECT ref_id FROM decisions WHERE ref_type='sequence' ORDER BY created_at DESC")["ref_id"]
    src = ws.db.one("SELECT path FROM exports WHERE deliverable_id=? AND kind='preview'", (d_long,))["path"]
    os.environ["STUB_EXPORT_SOURCE"] = src
    j = run(ws, call(ws, "premiere_export", pid, sequence_id=seq, preset_path="/stub/preset.epr", deliverable_id=d_long))
    e = ws.db.one("SELECT * FROM exports WHERE id=?", (j["result"]["export_id"],))
    assert e["kind"] == "premiere_export" and e["deliverable_id"] == d_long
    q = run(ws, j["result"]["qc_job"])["result"]
    assert q["checks"]["decode_errors"] == [] and q["checks"]["probe"]["width"] == 1920
    from shiftws import tools as T
    again = T.call(ws, "premiere_export", {"sequence_id": seq, "preset_path": "/stub/p.epr", "deliverable_id": d_long,
                                           "output_path": e["path"]}, scope={"project_id": pid})
    from shiftws import jobs as J
    assert J.run_inline(ws, J.get(ws, again["job_ids"][0]))["status"] == "failed"     # never overwrites
