"""STUB Premiere bridge (MCP stdio) for testing the adapter's timeout/idempotency LOGIC only.

It is not Premiere and proves nothing about Premiere itself. It mimics the hetpatel catalog surface
(get_tool_schema / invoke_tool / list_sequences / verify_premiere_connection) with state in a JSON file, and can
simulate the dangerous case: an import that SUCCEEDS but answers after the client's timeout.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

STATE = Path(os.environ["STUB_STATE"])
srv = MCPServer(name="stub-premiere")


def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {"sequences": [], "imports": 0, "saves": 0}


def save(s):
    STATE.write_text(json.dumps(s))


@srv.tool(name="verify_premiere_connection")
def verify_premiere_connection(launchIfNeeded: bool = True) -> str:
    return json.dumps({"ready": True, "success": True, "status": "ready", "stub": True})


@srv.tool(name="list_sequences")
def list_sequences() -> str:
    return json.dumps({"sequences": load()["sequences"]})


@srv.tool(name="get_capabilities")
def get_capabilities(checkConnection: bool = False) -> str:
    return json.dumps({"stub": True})


@srv.tool(name="get_tool_schema")
def get_tool_schema(name: str) -> str:
    return json.dumps({"name": name, "inputSchema": {"properties": {}}})


@srv.tool(name="invoke_tool")
def invoke_tool(name: str, arguments: dict | None = None) -> str:
    a = arguments or {}
    s = load()
    if name == "save_project":
        s["saves"] += 1
        save(s)
        return json.dumps({"saved": True})
    if name == "import_fcp_xml":
        s["imports"] += 1
        xml = Path(a["filePath"]).read_text()
        seq_name = re.search(r"<sequence[^>]*><name>(.*?)</name>", xml).group(1)
        clips = re.findall(r'<clipitem id="(c\d+)">.*?<start>(\d+)</start>', xml)
        rate = int(re.search(r"<timebase>(\d+)</timebase>", xml).group(1))
        s["sequences"].append({"id": f"seq{len(s['sequences']) + 1}", "name": seq_name,
                               "videoTracks": [{"clips": [{"name": c, "start": int(st) / rate} for c, st in clips]}],
                               "audioTracks": [{"clips": [{"name": "vo"}]} if "narration (locked)" in xml else {"clips": []}]})
        save(s)
        if os.environ.get("STUB_SLOW_IMPORT") and s["imports"] == 1:
            time.sleep(float(os.environ["STUB_SLOW_IMPORT"]))   # succeeds, but answers late
        return json.dumps({"imported": True})
    if name == "export_sequence":
        # mimic Media Encoder: the file appears a moment after the call returns
        import shutil, threading
        src = os.environ.get("STUB_EXPORT_SOURCE")
        def write():
            time.sleep(1.5)
            shutil.copy(src, a["outputPath"])
        threading.Thread(target=write).start()
        time.sleep(2.0)
        return json.dumps({"queued": True, "outputPath": a["outputPath"]})
    if name == "list_sequence_tracks":
        seq = next(x for x in s["sequences"] if x["id"] == a["sequenceId"])
        return json.dumps({"videoTracks": seq["videoTracks"], "audioTracks": seq["audioTracks"]})
    return json.dumps({"error": f"stub does not implement {name}"})


if __name__ == "__main__":
    srv.run("stdio")
