"""Recorded requests must be snapshots of their individual conversation turns."""

import gzip
import json
import unittest
from services.orchestrator.tools import declarations
from services.orchestrator.image_tool import declaration
from services.orchestrator.model import Configuration, GatewayModel
from pathlib import Path


class ChatRecordingTests(unittest.TestCase):
    def test_initial_request_precedes_tool_results(self) -> None:
        archive = json.loads(
            gzip.decompress(Path("tests/cassettes/chat.json.gz").read_bytes())
        )
        calls = archive["calls"]
        model = GatewayModel(
            "http://unused", Configuration.model_validate(archive["configuration"])
        )
        model.tools = declarations() + [declaration()]
        for call in calls:
            self.assertEqual(
                call["request"]["tools"],
                model.available_tools(call["request"]["messages"]),
            )
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(
            [m["role"] for m in calls[0]["request"]["messages"]],
            ["system", "user"],
        )
        for previous, current in zip(calls, calls[1:]):
            before = previous["request"]["messages"]
            after = current["request"]["messages"]
            self.assertGreater(len(after), len(before))
            self.assertEqual(after[: len(before)], before)
            self.assertEqual(after[-1]["role"], "tool")
