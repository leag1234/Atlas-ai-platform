"""Cancellation, bounded backpressure, error finality and citation boundaries."""

import asyncio
from collections.abc import AsyncGenerator
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context


def request() -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "stream": True,
            "reasoning_effort": "low",
            "messages": [{"role": "user", "content": "Bonjour"}],
        }
    )


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_reader_is_bounded_and_cancel_closes_producer(self) -> None:
        produced = 0
        closed = asyncio.Event()

        async def process(payload: ChatRequest, item: Interaction) -> None:
            nonlocal produced
            sink = sink_context.get()
            assert sink is not None
            try:
                for _ in range(100):
                    await sink({"delta": {"content": "mot "}})
                    produced += 1
                await asyncio.Event().wait()
            finally:
                closed.set()

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
        ):
            item = Interaction()
            stream = response(request(), item, time.monotonic(), process).body_iterator
            assert isinstance(stream, AsyncGenerator)
            await stream.__anext__()
            await asyncio.sleep(0.02)
            self.assertLessEqual(produced, 9)
            await stream.aclose()
            self.assertTrue(closed.is_set())
            rows = [
                json.loads(line)
                for p in Path(directory).glob("*.jsonl")
                for line in p.read_text().splitlines()
            ]
            self.assertEqual(len(rows), 1)
            self.assertIn("cancelled", rows[0]["erreurs"])

    async def test_final_status_reports_completion_time_without_reshowing_spinner(
        self,
    ) -> None:
        async def process(payload: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            assert sink is not None
            await sink({"delta": {"content": "Useful complete answer.\n"}})
            item.state = "done"

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
        ):
            chunks = [
                str(x)
                async for x in response(
                    request(), Interaction(), time.monotonic() - 2, process
                ).body_iterator
            ]
        events = [json.loads(x[6:]) for x in chunks if x.startswith("data: {")]
        final = next(
            e
            for e in events
            if e.get("choices", [{}])[0].get("finish_reason") == "stop"
        )
        self.assertTrue(final["event"]["data"]["done"])
        self.assertTrue(final["event"]["data"]["hidden"])
        self.assertGreaterEqual(final["event"]["data"]["elapsed_seconds"], 2)

    async def test_file_failure_discloses_authorized_ceiling(self) -> None:
        async def process(payload: ChatRequest, item: Interaction) -> None:
            item.cout_eur = 0.3
            raise RuntimeError("private-provider-detail")

        payload = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "Create a PDF"}]}
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
        ):
            chunks = [
                str(x)
                async for x in response(
                    payload, Interaction(), time.monotonic(), process
                ).body_iterator
            ]
        text = "".join(chunks)
        self.assertIn("reserved cost 0.300000 EUR (limit 0.30 EUR)", text)
        self.assertNotIn("private-provider-detail", text)

    async def test_failure_after_delta_never_claims_success(self) -> None:
        async def process(payload: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            assert sink is not None
            await sink({"delta": {"reasoning_content": "étape synthétique"}})
            await sink({"delta": {"content": "début "}})
            raise RuntimeError("private-provider-detail")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
        ):
            chunks = [
                x
                async for x in response(
                    request(), Interaction(), time.monotonic(), process
                ).body_iterator
            ]
            text = "".join(str(x) for x in chunks)
            self.assertIn("reasoning_content", text)
            self.assertIn("stream_error", text)
            self.assertNotIn("private-provider-detail", text)
            self.assertNotIn('"finish_reason": "stop"', text)
            self.assertTrue(text.endswith("data: [DONE]\n\n"))

    async def test_intermediate_text_is_labeled_in_the_ui(self) -> None:
        async def process(payload: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            assert sink is not None
            await sink({"turn": 1, "phase": "generating"})
            await sink({"delta": {"content": "Recherche en cours."}})
            await sink({"turn": 1, "phase": "intermediate"})
            await sink({"turn": 2, "phase": "generating"})
            await sink({"delta": {"content": "Réponse finale."}})
            await sink({"turn": 2, "phase": "final"})
            item.state = "done"

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
        ):
            events = [
                str(x)
                async for x in response(
                    request(), Interaction(), time.monotonic(), process
                ).body_iterator
            ]
            content = "".join(
                json.loads(x[6:])["choices"][0]["delta"].get("content", "")
                for x in events
                if x != "data: [DONE]\n\n"
            )
            self.assertIn("Étape intermédiaire terminée", content)
            self.assertLess(
                content.index("Recherche"), content.index("Étape intermédiaire")
            )
            self.assertLess(
                content.index("Étape intermédiaire"), content.index("Réponse finale")
            )

    async def test_fragmented_citation_is_resolved_before_link(self) -> None:
        key = "a" * 64
        source: dict[str, object] = {
            "chunk_id": key,
            "doc_id": "doc",
            "source": "synthetic.md",
            "text": "preuve",
        }
        item = Interaction()
        item.chunks_recuperes = [source]

        async def process(payload: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            assert sink is not None
            for part in ["Réponse [", key[:30], key[30:], "] suite"]:
                await sink({"delta": {"content": part}})
            await sink({"turn": 1, "phase": "final"})
            item.state = "done"

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
            patch(
                "services.orchestrator.chat_pipeline.source",
                new=AsyncMock(return_value=source),
            ),
        ):
            chunks = [
                str(x)
                async for x in response(
                    request(), item, time.monotonic(), process
                ).body_iterator
            ]
            content = "".join(
                json.loads(x[6:])["choices"][0]["delta"].get("content", "")
                for x in chunks
                if x != "data: [DONE]\n\n"
            )
            self.assertIn("/sources/" + key, content)
            self.assertNotIn("[" + key + "]", content)
            self.assertIn("suite", content)
