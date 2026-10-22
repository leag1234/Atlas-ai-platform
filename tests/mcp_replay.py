"""MCP tool response replay, only executable from the test harness."""

import json
import os
from pathlib import Path
import sys

records = json.loads(Path("tests/fixtures/mcp.json").read_text())
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}
    else:
        match = next((r for r in records if r["request"] == request["params"]), None)
        if match is None:
            raise ValueError("unrecorded_mcp_request")
        result = match["result"]
        if request["params"]["name"] == "create_issue":
            with Path(os.environ["MCP_LEDGER"]).open("a") as stream:
                stream.write("write\n")
    print(
        json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}),
        flush=True,
    )
