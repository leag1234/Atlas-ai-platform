"""Download links and strategy disclosures survive fragmented public SSE."""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context


class FileStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_link_is_canonical_and_strategy_visible(self) -> None:
        url = "/api/v1/terminals/atlas-files/files/serve/home/user/requests/test/report.docx"

        async def process(request: ChatRequest, item: Interaction) -> None:
            item.files = [url]
            item.file_strategies = ["regenerated"]
            item.terminal_commands = 2
            sink = sink_context.get()
            assert sink is not None
            for part in (
                "Here is [Report]",
                "(https://invented.invalid/",
                "report.docx)",
            ):
                await sink({"delta": {"content": part}})
            item.state = "done"

        request = ChatRequest.model_validate(
            {
                "stream": True,
                "ui_locale": "en",
                "messages": [
                    {"role": "user", "content": "Please correct this document."}
                ],
            }
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
        ):
            events = [
                json.loads(str(event)[6:])
                async for event in response(
                    request, Interaction(), time.monotonic(), process
                ).body_iterator
                if str(event).startswith("data: {")
            ]
        text = "".join(
            event.get("choices", [{}])[0].get("delta", {}).get("content", "")
            for event in events
        )
        self.assertIn(url, text)
        self.assertNotIn("invented.invalid", text)
        self.assertIn("Regenerated document", text)
        self.assertEqual(events[-1]["atlas"]["files"], [url])
        self.assertEqual(events[-1]["atlas"]["terminal_commands"], 2)
