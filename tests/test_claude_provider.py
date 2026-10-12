"""Opt-in: the chat's Claude Code provider against the user's own installed, logged-in CLI.
Run with SHIFTWS_TEST_CLAUDE=1 (uses the user's plan limits). Verifies tool execution, image inspection, skill
access and continuity across an app restart — not just a text reply."""
import os
from pathlib import Path

import pytest

MEDIA = Path(os.environ.get("SHIFTWS_TESTMEDIA", "/root/shiftws-testmedia"))
pytestmark = pytest.mark.skipif(os.environ.get("SHIFTWS_TEST_CLAUDE") != "1", reason="opt-in (uses Claude Code)")


def test_claude_code_operates_workspace(tmp_path):
    os.environ["SHIFTWS_PROJECTS_ROOT"] = str(tmp_path / "projects")
    from shiftws.workspace import open_workspace
    from shiftws.chat.engine import send
    from shiftws import jobs as J
    home = tmp_path / "home"
    ws = open_workspace(home)
    w = J.Workers(ws, parallel=2)
    w.start()
    # 1. create a project through chat (channel thread)
    r = send(ws, "th_channel", "Create a narrated Short project titled 'Freedoom corridor test' about Freedoom. "
             "Reply with the project id only.", wait=True)
    p = ws.db.one("SELECT * FROM projects WHERE title LIKE '%corridor test%'")
    assert p, r
    th = ws.db.one("SELECT id FROM threads WHERE project_id=? AND kind='main'", (p["id"],))["id"]
    # 2. grant gameplay from the chat → analysis report runs automatically
    send(ws, th, "Here is the gameplay. Analyze it (first 30 seconds is enough).",
         attachments=[str(MEDIA / "footage" / "freedoom2_attract.mp4")], wait=True)
    rep = None
    for _ in range(120):
        rep = ws.db.one("SELECT * FROM reports WHERE project_id=?", (p["id"],))
        if rep:
            break
        import time; time.sleep(2)
    assert rep, "analysis report should be created from the chat grant"
    # 3. image inspection: the model must look at frames and record descriptions
    send(ws, th, "Look at the first 2 uninspected scene frames (view the images) and record what you see with honest "
         "confidence. Short reply.", wait=True)
    assert ws.db.one("SELECT COUNT(*) n FROM scenes s JOIN project_assets pa ON pa.asset_id=s.asset_id WHERE "
                     "pa.project_id=? AND s.described_by IS NOT NULL", (p["id"],))["n"] >= 2
    # 4. skill access: hook scoring through the vendored yt-script tool
    send(ws, th, "Score this hook with the hook scorer and tell me the band: 'The first room is a lie.'", wait=True)
    assert ws.db.one("SELECT 1 FROM tool_events WHERE thread_id=? AND tool='script_score_hooks'", (th,))
    # 5. continuity across restart: new workspace object, new process-level state
    w.stop()
    ws.db.close()
    ws2 = open_workspace(home)
    r = send(ws2, th, "Without looking anything up again: what did you see in the first frame you described?", wait=True)
    first = ws2.db.one("SELECT description FROM scenes WHERE described_by IS NOT NULL ORDER BY start LIMIT 1")["description"]
    key = [w_ for w_ in first.lower().split() if len(w_) > 5][:3]
    assert any(k in (r["text"] or "").lower() for k in key), (r["text"], first)
