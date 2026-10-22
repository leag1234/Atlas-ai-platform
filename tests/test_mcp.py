"""MCP protocol and policy adversaries are confined to this test process."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from services.orchestrator import mcp_client as mcp
from services.orchestrator.mcp_transport import invoke
from packages.limits import LimitError

SERVER = """import json,os,sys,time
mode=os.environ.get('MODE','ok')
for line in sys.stdin:
 q=json.loads(line)
 if 'id' not in q: continue
 if mode=='timeout': time.sleep(3)
 if mode=='giant': print('x'*300000,flush=True); continue
 if mode=='invalid': print('{}',flush=True); continue
 if q['method']=='initialize':
  r={'protocolVersion':'bad' if mode=='version' else '2025-11-25','capabilities':{'tools':{}}}
 else:
  r={'content':[{'type':'text','text':os.environ.get('CREDENTIAL','') if mode=='leak' else 'blue'}]}
  if mode=='error': r['isError']=True
 print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':r}),flush=True)
"""


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_line_reports_measured_bytes(self) -> None:
        with self.assertRaises(LimitError) as caught:
            await invoke(
                sys.executable, ["-c", SERVER], {"MODE": "giant"}, [], "read", {}, 3
            )
        self.assertGreater(caught.exception.measured, 262144)
        self.assertEqual(caught.exception.limit, 262144)
        self.assertIn(str(caught.exception.measured), caught.exception.detail)

    async def test_transport_bounds_errors_and_secret_echo(self) -> None:
        for mode in ("ok", "timeout", "giant", "invalid", "version", "leak", "error"):
            with self.subTest(mode=mode):
                operation = invoke(
                    sys.executable,
                    ["-c", SERVER],
                    {"MODE": mode, "CREDENTIAL": "test-credential"},
                    ["test-credential"],
                    "read",
                    {},
                    0.1 if mode == "timeout" else 3,
                )
                if mode == "ok":
                    self.assertEqual(
                        await operation, {"content": [{"type": "text", "text": "blue"}]}
                    )
                else:
                    with self.assertRaises((ValueError, TimeoutError)):
                        await operation

    async def test_policy_and_runtime(self) -> None:
        from services.orchestrator.tools import declarations, Runtime
        from services.orchestrator.loop import Call
        from services.orchestrator.cache import Cache
        from decimal import Decimal

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "gitlab": {
                            "command": sys.executable,
                            "args": ["-c", SERVER],
                            "secret_env": {"CREDENTIAL": "MCP_TEST_CREDENTIAL"},
                            "tools": {
                                "read": {
                                    "effect": "read",
                                    "fields": ["title"],
                                    "fixed": {"project_id": "1"},
                                },
                                "write": {"effect": "write", "fields": []},
                            },
                        }
                    }
                )
            )
            with patch.dict(
                os.environ,
                {
                    "ATLAS_MCP_CONFIG": str(path),
                    "ATLAS_MCP_LOG_DIR": directory,
                    "MCP_TEST_CREDENTIAL": "test-credential",
                },
            ):
                runtime = Runtime(Cache(Path(directory) / "cache.db"), Decimal("0"))
                for tool, arguments, expected in [
                    ("read", {}, "data"),
                    ("write", {}, "error"),
                    ("read", {"project_id": "2"}, "error"),
                    ("unknown", {}, "error"),
                    ("read", {"confirmed": True}, "error"),
                    ("read", {"title": "test-credential"}, "error"),
                ]:
                    result = await runtime.execute(
                        Call(
                            "1",
                            "mcp_call",
                            json.dumps(
                                {
                                    "server": "gitlab",
                                    "tool": tool,
                                    "arguments": arguments,
                                }
                            ),
                        ),
                        3,
                    )
                    self.assertIn(expected, result)
                self.assertEqual(
                    await mcp.execute("{}", 1), {"error": "mcp_unavailable_or_refused"}
                )
                self.assertEqual(len(declarations()), 5)
                self.assertNotIn("test-credential", json.dumps(declarations()))
                rows = (Path(directory) / "actions.jsonl").read_text()
                self.assertNotIn("test-credential", rows)
                self.assertIn("confirmation_required", rows)
                path.write_text("x" * 65537)
                with self.assertRaises(ValueError):
                    mcp.configuration()
            with patch.dict(os.environ, {"ATLAS_MCP_CONFIG": ""}):
                self.assertEqual(len(declarations()), 4)
                self.assertEqual(mcp.configuration(), {})
