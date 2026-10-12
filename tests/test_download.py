"""yt-dlp acquisition with an authorized public sample (Big Buck Bunny, (c) Blender Foundation, CC-BY 3.0, hosted in
the mediaelement-files GitHub repository). Uses yt-dlp's generic extractor; site extractors (e.g. YouTube) are a
pending Mac test because video sites are blocked in the build container."""
import os
import urllib.request

import pytest

URL = "https://raw.githubusercontent.com/mediaelement/mediaelement-files/master/big_buck_bunny.mp4"


def reachable():
    try:
        urllib.request.urlopen(urllib.request.Request(URL, method="HEAD"), timeout=15)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not reachable(), reason="sample host unreachable")


def test_download_dedupe_range_and_errors(tmp_path):
    os.environ["SHIFTWS_PROJECTS_ROOT"] = str(tmp_path / "projects")
    from shiftws import jobs as J
    from shiftws import tools as T
    from shiftws.workspace import open_workspace
    ws = open_workspace(tmp_path / "home")
    pid = T.call(ws, "project_create", {"title": "Download test"})["data"]["project_id"]
    sc = {"project_id": pid, "origin": "test"}

    def run(r):
        return J.run_inline(ws, J.get(ws, r["job_ids"][0]))

    j1 = run(T.call(ws, "assets_download", {"url": URL}, scope=sc))
    assert j1["status"] == "done", j1["error"]
    a = ws.db.one("SELECT * FROM media_assets WHERE id=?", (j1["result"]["asset_id"],))
    assert a["duration"] and a["width"] and "/source-media/downloads/" in a["current_path"]
    src = ws.db.one("SELECT * FROM source_references WHERE asset_id=?", (a["id"],))
    assert src["url"] == URL and "yt-dlp" in src["metadata_json"]
    # same URL again → no second download (job cache), and a forced job also reuses the file on disk
    again = T.call(ws, "assets_download", {"url": URL}, scope=sc)
    assert again["data"]["reused"] is True
    forced = J.run_inline(ws, J.enqueue(ws, "download", {"url": URL}, project_id=pid, force=True))
    assert forced["result"]["reused"] is True
    # requested range: stored with its source offset
    j3 = run(T.call(ws, "assets_download", {"url": URL, "start": 10.0, "end": 16.0}, scope=sc))
    assert j3["status"] == "done", (j3["error"], j3["error_hint"])
    r = ws.db.one("SELECT * FROM source_references WHERE asset_id=?", (j3["result"]["asset_id"],))
    assert r["requested_range"] == "10.0-16.0" and r["source_offset"] == 10.0
    d = ws.db.one("SELECT duration FROM media_assets WHERE id=?", (j3["result"]["asset_id"],))["duration"]
    assert 4.0 <= d <= 8.5
    # unsupported/invalid source → actionable, non-retrying error
    bad = J.run_inline(ws, J.enqueue(ws, "download", {"url": "https://raw.githubusercontent.com/mediaelement/mediaelement-files/master/does-not-exist.mp4"},
                                     project_id=pid, force=True))
    assert bad["status"] == "failed" and bad["attempts"] == 1 and bad["error_hint"]
