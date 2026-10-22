"""The HTTP human boundary is mandatory even when the model asks to write."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from packages.ports import atlas_ports
from services.orchestrator.chat_api import app
from services.orchestrator import mcp_client as mcp
from services.orchestrator import mcp_confirmation as confirmation
from test_mcp import SERVER


class ConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.ledger = self.root / "writes"
        script = SERVER.replace(
            "else:\n  r=",
            "else:\n  open(os.environ['LEDGER'],'a').write('write\\n')\n  r=",
        )
        self.config.write_text(
            json.dumps(
                {
                    "gitlab": {
                        "command": sys.executable,
                        "args": ["-c", script],
                        "env": {"LEDGER": str(self.ledger)},
                        "tools": {
                            "create_issue": {
                                "effect": "write",
                                "fields": ["title"],
                                "fixed": {"project_id": "1"},
                            }
                        },
                    }
                }
            )
        )
        env = patch.dict(
            os.environ,
            {"ATLAS_MCP_CONFIG": str(self.config), "ATLAS_MCP_LOG_DIR": str(self.root)},
        )
        env.start()
        self.addCleanup(env.stop)
        confirmation.pending.clear()
        self.addCleanup(confirmation.pending.clear)
        self.client = TestClient(
            app, base_url=atlas_ports().url("adapter", "localhost")
        )
        self.addCleanup(self.client.close)
        self.origin = {"origin": atlas_ports().url("adapter", "localhost")}

    def prepare(self) -> tuple[str, str]:
        result = asyncio.run(
            mcp.execute(
                json.dumps(
                    {
                        "server": "gitlab",
                        "tool": "create_issue",
                        "arguments": {"title": "<script>synthetic</script>"},
                    }
                ),
                3,
            )
        )
        url = str(result["confirmation_url"])
        preview = self.client.get(url)
        self.assertEqual(preview.status_code, 200)
        self.assertIn("&lt;script&gt;", preview.text)
        self.assertNotIn("<script>", preview.text)
        token = re.search('name="csrf" value="([a-f0-9]+)"', preview.text)
        assert token is not None
        return url, token.group(1)

    def test_no_write_until_human_post_and_no_replay(self) -> None:
        url, token = self.prepare()
        self.assertFalse(self.ledger.exists())
        self.assertEqual(self.client.post(url, data={"csrf": token}).status_code, 403)
        self.assertEqual(
            self.client.post(
                url, data={"csrf": "wrong"}, headers=self.origin
            ).status_code,
            400,
        )
        self.assertFalse(self.ledger.exists())
        result = self.client.post(url, data={"csrf": token}, headers=self.origin)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["trust"], "untrusted")
        self.assertEqual(self.ledger.read_text(), "write\n")
        self.assertEqual(
            self.client.post(
                url, data={"csrf": token}, headers=self.origin
            ).status_code,
            400,
        )
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.ledger.read_text(), "write\n")
        self.assertIn("write_ok", (self.root / "actions.jsonl").read_text())

    def test_expiry_drift_csrf_and_host(self) -> None:
        url, token = self.prepare()
        key = url.rsplit("/", 1)[1]
        self.client.cookies.clear()
        self.assertEqual(
            self.client.post(
                url, data={"csrf": token}, headers=self.origin
            ).status_code,
            400,
        )
        self.client.get(url)
        self.assertEqual(
            self.client.get(url, headers={"host": "evil.example"}).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                url, data={"csrf": token}, headers={"origin": "http://evil.example"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                url, json={"csrf": token}, headers=self.origin
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                url, data={"csrf": "x" * 1100}, headers=self.origin
            ).status_code,
            400,
        )
        confirmation.pending[key] = replace(confirmation.pending[key], expires=0)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(
            self.client.post(
                url, data={"csrf": token}, headers=self.origin
            ).status_code,
            400,
        )
        url, token = self.prepare()
        config = json.loads(self.config.read_text())
        config["gitlab"]["env"]["MODE"] = "error"
        self.config.write_text(json.dumps(config))
        self.assertEqual(
            self.client.post(
                url, data={"csrf": token}, headers=self.origin
            ).status_code,
            400,
        )
        self.assertFalse(self.ledger.exists())
        url, token = self.prepare()
        self.assertEqual(
            self.client.post(
                url, data={"csrf": token}, headers=self.origin
            ).status_code,
            400,
        )
        self.assertEqual(self.ledger.read_text(), "write\n")
        self.assertIn(
            "write_failed_or_uncertain", (self.root / "actions.jsonl").read_text()
        )

    def test_concurrent_confirmation_creates_once(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        url, token = self.prepare()

        def post(_: int) -> int:
            return int(
                self.client.post(
                    url, data={"csrf": token}, headers=self.origin
                ).status_code
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(post, range(2))), [200, 400])
        self.assertEqual(self.ledger.read_text(), "write\n")

    def test_configuration_snapshot_is_the_one_executed(self) -> None:
        url, token = self.prepare()
        original = mcp.configuration()
        changed = {
            name: server.model_copy(deep=True) for name, server in original.items()
        }
        other = self.root / "unconfirmed-project"
        changed["gitlab"].env["LEDGER"] = str(other)
        with patch.object(mcp, "configuration", side_effect=[original, changed]):
            response = self.client.post(url, data={"csrf": token}, headers=self.origin)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(other.exists())
        self.assertEqual(self.ledger.read_text(), "write\n")
