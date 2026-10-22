"""Whitespace is not a visible answer token; keep named activity until real text."""

import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_whitespace_keeps_elapsed_activity_visible(self) -> None:
        request = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "Explain this document"}],
                "stream": True,
            }
        )
        item = Interaction()

        async def process(payload: ChatRequest, journal: Interaction) -> None:
            sink = sink_context.get()
            assert sink is not None
            await sink({"delta": {"content": "\n "}})
            await asyncio.sleep(2.1)
            await sink({"delta": {"content": "The document contains three sections."}})
            journal.reponse = "The document contains three sections."
            journal.state = "done"

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                "os.environ", {"ATLAS_INTERACTION_DIR": str(Path(root) / "logs")}
            ),
        ):
            stream = response(request, item, time.monotonic(), process)
            waiting_statuses = []
            visible = False
            async for chunk in stream.body_iterator:
                text = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
                if not text.startswith("data: ") or "[DONE]" in text:
                    continue
                event = json.loads(text[6:])
                delta = event.get("choices", [{}])[0].get("delta", {})
                visible |= bool(delta.get("content", "").strip())
                status = event.get("event", {}).get("data", {})
                if status and not visible:
                    waiting_statuses.append(status)
            self.assertTrue(visible)
            self.assertTrue(
                any(s.get("elapsed_seconds", 0) >= 1 for s in waiting_statuses)
            )
            self.assertTrue(
                all(
                    s.get("hidden") is not True and s["done"] is False
                    for s in waiting_statuses
                )
            )
