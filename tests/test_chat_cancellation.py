"""A browser cancellation must stop the producer and settle its reservation."""

import asyncio
from collections.abc import AsyncIterator
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from uuid import uuid4
from unittest.mock import patch

from services.orchestrator.cancellation import active_count, cancel
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context


class ChatCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_cancel_stops_provider_settles_budget_and_leaves_no_task(
        self,
    ) -> None:
        token = uuid4()
        started, provider_cancelled, settled = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        item = Interaction()

        async def provider(request: ChatRequest, result: Interaction) -> None:
            stream = sink_context.get()
            assert stream is not None
            await stream({"delta": {"content": "Visible answer. "}})
            result.cout_eur = 0.012
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                provider_cancelled.set()
                raise

        async def budgeted(request: ChatRequest, result: Interaction) -> None:
            try:
                await provider(request, result)
            finally:
                # This is the same finally-boundary used by chat_api's ledger.
                self.assertEqual(result.cout_eur, 0.012)
                settled.set()

        request = ChatRequest.model_validate(
            {
                "stream": True,
                "cancel_id": str(token),
                "messages": [{"role": "user", "content": "Keep answering"}],
            }
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
        ):
            stream: AsyncIterator[str | bytes | memoryview[int]] = response(
                request, item, time.monotonic(), budgeted
            ).body_iterator.__aiter__()
            first = await stream.__anext__()
            self.assertIn("chat.completion.chunk", first)
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue(cancel(str(token)))
            frames = [frame async for frame in stream]
            self.assertTrue(provider_cancelled.is_set())
            self.assertTrue(settled.is_set())
            self.assertEqual(active_count(), 0)
            self.assertEqual(item.state, "cancelled_by_user")
            self.assertIn("cancelled_by_user", item.erreurs)
            self.assertTrue(any("[DONE]" in frame for frame in frames))
            rows = [
                json.loads(line)
                for path in Path(directory).glob("*.jsonl")
                for line in path.read_text().splitlines()
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["state"], "cancelled_by_user")
            self.assertEqual(rows[0]["cout_eur"], 0.012)

    async def test_unknown_or_completed_token_cannot_cancel_another_turn(self) -> None:
        self.assertFalse(cancel(str(uuid4())))
        self.assertEqual(active_count(), 0)

    async def test_cancelled_empty_assistant_does_not_block_the_next_turn(self) -> None:
        request = ChatRequest.model_validate(
            {
                "messages": [
                    {"role": "user", "content": "Start a long answer"},
                    {"role": "assistant", "content": ""},
                    {"role": "user", "content": "What is 2 + 2?"},
                ]
            }
        )
        self.assertEqual(request.user_text, "What is 2 + 2?")
        self.assertEqual(len(request.messages), 2)
