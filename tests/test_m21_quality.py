"""M21 public profiles, whole evidence, expertise and prompt activity."""

import asyncio
from collections.abc import AsyncGenerator
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from packages.evidence import estimated_tokens, whole_chunks
from services.orchestrator.cache import Cache
from services.orchestrator.chat_api import app
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction
from services.orchestrator.tools import Fetch, Runtime


class QualityTests(unittest.IsolatedAsyncioTestCase):
    def test_three_profiles(self) -> None:
        with TestClient(app) as client:
            names = {x["id"] for x in client.get("/v1/models").json()["data"]}
        self.assertEqual(names, {"atlas-qwen", "atlas-glm", "atlas-deepseek"})
        for model, effort in [
            (name, "none") for name in ("atlas-qwen", "atlas-glm", "atlas-deepseek")
        ]:
            request = ChatRequest.model_validate(
                {"model": model, "messages": [{"role": "user", "content": "Explain."}]}
            )
            self.assertEqual(request.reasoning_effort, effort)

    def test_expertise_and_safety(self) -> None:
        for name in ("chat", "vision", "web-chat", "rag"):
            prompt = Path("prompts/" + name + ".txt").read_text().lower()
            self.assertIn("expert", prompt)
            self.assertIn("conclude", prompt)
            self.assertIn("derivation", prompt)
        vision = Path("prompts/vision.txt").read_text().lower()
        self.assertNotIn("describe only what is visible", vision)
        self.assertIn("untrusted", vision)
        self.assertIn("do not invent", vision)

    async def test_full_extraction_before_size_decision(self) -> None:
        article = "Introduction.\n" + "Construction details.\n" * 200
        article += "Ilford Multigrade, cider, Bayfordbury."
        with tempfile.TemporaryDirectory() as root:
            runtime = Runtime(
                Cache(Path(root) / "cache.sqlite"), Decimal(0), "Build it?"
            )
            with (
                patch.object(
                    runtime.web,
                    "fetch",
                    AsyncMock(
                        return_value=(
                            "https://example.org/article",
                            b"<html>" + b" " * 400000 + b"</html>",
                        )
                    ),
                ),
                patch("trafilatura.extract", return_value=article),
            ):
                result = await runtime.fetch(
                    Fetch(url="https://example.org/article"), 5
                )
            self.assertEqual(result["text"], article)
            self.assertFalse(result["truncated"])
            self.assertNotIn("synthesis", result)

    def test_token_budget_keeps_whole_chunks_and_tail(self) -> None:
        text = "Background. " * 60 + "Drying time: 73 months."
        chunks = [{"chunk_id": "a" * 64, "source": "manual", "text": text}]
        size = estimated_tokens(json.dumps(chunks[0], ensure_ascii=False))
        self.assertEqual(whole_chunks(chunks, size), chunks)
        self.assertEqual(whole_chunks(chunks, size - 1), [])
        self.assertGreater(text.index("73 months"), 400)
        with self.assertRaises(ValueError):
            whole_chunks(chunks, 0)

    async def test_activity_precedes_inference_and_cancellation(self) -> None:
        entered, closed = asyncio.Event(), asyncio.Event()

        async def slow(payload: ChatRequest, item: Interaction) -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()

        payload = ChatRequest.model_validate(
            {"stream": True, "messages": [{"role": "user", "content": "Explain."}]}
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
        ):
            stream = response(
                payload, Interaction(), time.monotonic(), slow
            ).body_iterator
            assert isinstance(stream, AsyncGenerator)
            try:
                first = await asyncio.wait_for(stream.__anext__(), 1)
                event = json.loads(str(first).removeprefix("data: "))
                self.assertEqual(event["event"]["type"], "status")
                self.assertFalse(event["event"]["data"]["done"])
                self.assertIn("description", event["event"]["data"])
                await asyncio.wait_for(entered.wait(), 1)
            finally:
                await stream.aclose()
            self.assertTrue(closed.is_set())

    async def test_tool_details_name_and_escape_consulted_urls(self) -> None:
        from services.orchestrator.stream_client import sink_context

        async def work(payload: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            assert sink is not None
            await sink({"phase": "intermediate", "turn": 1, "tools": ["web_fetch"]})
            await sink(
                {
                    "phase": "tool_finished",
                    "tool": "web_fetch",
                    "urls": ["https://example.org/?q=<script>"],
                }
            )
            item.reponse, item.state = "Answer.", "done"
            await sink({"delta": {"content": item.reponse}})

        payload = ChatRequest.model_validate(
            {
                "stream": True,
                "ui_locale": "fr",
                "messages": [{"role": "user", "content": "Read this source."}],
            }
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
        ):
            stream = response(payload, Interaction(), time.monotonic(), work)
            rows = [
                json.loads(str(row).removeprefix("data: "))
                async for row in stream.body_iterator
                if str(row).startswith("data: {")
            ]
        text = "".join(
            row.get("choices", [{}])[0].get("delta", {}).get("content", "")
            for row in rows
        )
        self.assertIn("<details>\n<summary>Lecture de la page", text)
        self.assertIn("https://example.org/?q=&lt;script&gt;", text)
        self.assertNotIn("<script>", text)
        self.assertNotIn("<details open", text)
        intermediate = [
            row for row in rows if row.get("atlas", {}).get("phase") == "intermediate"
        ]
        self.assertTrue(intermediate)
        for event in intermediate:
            self.assertNotIn("content", event["choices"][0]["delta"])

    async def test_provider_failure_has_measured_public_and_journal_error(self) -> None:
        async def fail(payload: ChatRequest, item: Interaction) -> None:
            item.cout_eur = 0.02
            raise RuntimeError("private provider body")

        payload = ChatRequest.model_validate(
            {"stream": True, "messages": [{"role": "user", "content": "Explain"}]}
        )
        item = Interaction()
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
        ):
            rows = [
                str(row)
                async for row in response(
                    payload, item, time.monotonic(), fail
                ).body_iterator
            ]
        text = "".join(rows)
        self.assertIn("0.020000 EUR", text)
        self.assertIn("limit 0.10 EUR", text)
        self.assertIn("limit 120s", text)
        self.assertNotIn("private provider body", text)
        self.assertIn("0.020000 EUR", item.rejection["message"])
