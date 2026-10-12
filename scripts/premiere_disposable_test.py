#!/usr/bin/env python3
"""Live Premiere integration test on a DISPOSABLE project (run on the Mac).

Prerequisites: Premiere open with a project whose name contains "disposable" (e.g. shiftws-disposable.prproj),
the bridge panel running (Window > Extensions > MCP Bridge), and the workspace venv.

    .venv/bin/python scripts/premiere_disposable_test.py [--footage FILE] [--export-preset /path/preset.epr]

It refuses to run against any other project. Each step records ok/failed + the raw bridge answer into
docs/local/premiere_test_results.json. Nothing here is reported as working unless Premiere answered and the result
was read back (and, for export, the file was independently probed).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--footage", help="a short local video (default: first video in ~/Movies)")
    ap.add_argument("--export-preset", help=".epr export preset path (export step is skipped without it)")
    a = ap.parse_args()
    from shiftws.workspace import open_workspace
    from shiftws.premiere.adapter import BridgeClient, bridge_command
    from shiftws.premiere.fcpxml import plan_to_xmeml
    from shiftws.premiere.jobs import reconcile
    from shiftws import tools as T

    tmp = Path(tempfile.mkdtemp(prefix="shiftws-premiere-test-"))
    os.environ["SHIFTWS_PROJECTS_ROOT"] = str(tmp / "projects")
    ws = open_workspace(tmp / "home")
    cmd = bridge_command(ws)
    results = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "bridge_command": cmd, "steps": []}
    out = REPO / "docs" / "local" / "premiere_test_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    def step(name, fn):
        t = time.time()
        try:
            r = fn()
            ok = not (isinstance(r, dict) and r.get("is_error"))
            results["steps"].append({"step": name, "ok": ok, "seconds": round(time.time() - t, 2), "result": r})
            print(("✓ " if ok else "✗ ") + name)
            return r
        except Exception as e:  # noqa: BLE001
            results["steps"].append({"step": name, "ok": False, "error": repr(e)})
            print("✗ " + name + ": " + repr(e))
            return None
        finally:
            out.write_text(json.dumps(results, indent=1, default=str))

    if not cmd:
        sys.exit("No Premiere bridge found (configure premiere.mcp_command).")
    cli = BridgeClient(cmd, timeout=120)
    v = step("verify_premiere_connection (read-only)", lambda: cli.call("verify_premiere_connection", {"launchIfNeeded": False}, via_catalog=False))
    info = step("get_project_info", lambda: cli.call("get_project_info", {}))
    name = json.dumps((info or {}).get("data", "")).lower()
    if "disposable" not in name:
        sys.exit("Refusing: the open Premiere project's name must contain 'disposable'. Nothing was changed.")

    footage = Path(a.footage) if a.footage else next(iter(sorted((Path.home() / "Movies").glob("*.mp4"))), None)
    if not footage or not footage.exists():
        sys.exit("Provide --footage with a short local video.")
    # build a tiny real plan through the workspace (no-commentary preset → no narration required)
    pid = T.call(ws, "project_create", {"title": "premiere disposable test", "format_preset": "short_no_commentary",
                                        "length_seconds": 9})["data"]["project_id"]
    sc = {"project_id": pid, "origin": "test"}
    from shiftws import jobs as J
    J.run_inline(ws, J.get(ws, T.call(ws, "assets_import", {"paths": [str(footage)]}, scope=sc)["job_ids"][0]))
    plan = T.call(ws, "edit_plan_create", {}, scope=sc)["data"]
    from shiftws.editplan import get_plan
    prow = get_plan(ws, pid)
    seq_name = f"shiftws disposable test {int(time.time())}"
    xml = tmp / "plan.xml"
    xml.write_text(plan_to_xmeml(ws, prow["plan"], seq_name))

    step("save_project (before mutation)", lambda: cli.call("save_project", {}))
    step("import_media", lambda: cli.call("import_media", {"filePath": str(footage)}))
    step("list_project_items", lambda: cli.call("list_project_items", {}))
    step("import_fcp_xml (plan → new sequence)", lambda: cli.call("import_fcp_xml", {"filePath": str(xml)}))
    seqs = step("list_sequences", lambda: cli.call("list_sequences", {}))
    data = (seqs or {}).get("data") or {}
    lst = data.get("sequences", data) if isinstance(data, dict) else data
    seq = next((s for s in (lst or []) if isinstance(s, dict) and s.get("name") == seq_name), None)
    results["sequence_found"] = bool(seq)
    if seq:
        sid = seq.get("id") or seq.get("sequenceId")
        tr = step("list_sequence_tracks (readback)", lambda: cli.call("list_sequence_tracks", {"sequenceId": sid}))
        results["readback"] = reconcile(prow["plan"], (tr or {}).get("data"))
        print("  readback:", results["readback"]["summary"])
        step("add_marker", lambda: cli.call("add_marker", {"sequenceId": sid, "time": 1.0, "name": "shiftws test marker"}))
        clips = (((tr or {}).get("data") or {}).get("audioTracks") or [{}])
        aclip = next((c for t in clips for c in (t.get("clips") or []) if isinstance(c, dict)), None)
        if aclip:
            step("adjust_audio_levels", lambda: cli.call("adjust_audio_levels", {"clipId": aclip.get("id") or aclip.get("clipId"), "level": -6}))
        srt = tmp / "caps.srt"
        srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nSHIFTWS TEST CAPTION\n")
        step("import_media (SRT)", lambda: cli.call("import_media", {"filePath": str(srt)}))
        found = step("find_project_item_by_name (SRT)", lambda: cli.call("find_project_item_by_name", {"name": "caps"}))
        item = json.dumps((found or {}).get("data", ""))
        import re
        m = re.search(r'"(?:id|nodeId|projectItemId)":\s*"([^"]+)"', item)
        if m:
            step("create_caption_track (SRT — conventional subtitles, not kinetic)", lambda: cli.call("create_caption_track", {"sequenceId": sid, "projectItemId": m.group(1)}))
        step("save_project (after)", lambda: cli.call("save_project", {}))
        if a.export_preset:
            dest = tmp / "export.mp4"
            step("export_sequence", lambda: cli.call("export_sequence", {"sequenceId": sid, "outputPath": str(dest), "presetPath": a.export_preset}))
            for _ in range(180):
                if dest.exists() and dest.stat().st_size > 0:
                    break
                time.sleep(2)
            if dest.exists():
                pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,width,height",
                                     "-of", "json", str(dest)], capture_output=True, text=True)
                results["export_probe"] = json.loads(pr.stdout or "{}")
                print("  export probed:", results["export_probe"])
            else:
                results["export_probe"] = "no file appeared — export not verified"
    results["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    out.write_text(json.dumps(results, indent=1, default=str))
    print(f"\nResults: {out}\nSend me this file; only steps marked ok AND read back count as tested.")


if __name__ == "__main__":
    main()
