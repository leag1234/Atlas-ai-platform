"""Explicit UI presentation and clean API content, including tool-turn narration."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from services.orchestrator.chat_api import app
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call, Query, Reservation, Turn, run
from services.orchestrator.stream_client import sink_context
from decimal import Decimal


class AnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_separates_tool_narration_only_for_api(self) -> None:
        class Model:
            def __init__(self) -> None:
                self.turns = iter(
                    [
                        Turn(
                            "The page concerns a different area. I must search again. ",
                            (Call("one", "calculator", "{}"),),
                        ),
                        Turn("The answer is 56."),
                    ]
                )

            def estimate(self, messages: list[dict[str, object]]) -> Reservation:
                return Reservation(1, Decimal(0))

            async def complete(
                self, messages: list[dict[str, object]], timeout: float
            ) -> Turn:
                return next(self.turns)

        class Tools:
            def estimate(self, call: Call) -> Reservation:
                return Reservation(0, Decimal(0))

            async def execute(self, call: Call, timeout: float) -> dict[str, object]:
                return {"data": 56}

        for ui in (False, True):
            events: list[dict[str, object]] = []

            async def collect(event: dict[str, object]) -> None:
                events.append(event)

            token = sink_context.set(collect)
            try:
                result = await run(
                    Query(question="Compute seven times eight", lang="en"),
                    Model(),
                    Tools(),
                    "",
                    include_intermediate=ui,
                )
            finally:
                sink_context.reset(token)
            self.assertEqual(result.state, "done")
            if ui:
                self.assertIn("I must search again", result.text)
            else:
                self.assertEqual(result.text, "The answer is 56.")
                self.assertEqual(events[0]["phase"], "intermediate_narration")
                self.assertIn("I must search again", str(events[0]["text"]))

    async def test_http_stream_and_json_use_explicit_ui_signal(self) -> None:
        async def process(payload: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            assert sink
            await sink({"delta": {"content": "I must search again. "}})
            await sink({"phase": "intermediate", "tools": ["web_search"]})
            await sink(
                {
                    "phase": "tool_finished",
                    "tool": "web_search",
                    "urls": ["https://example.org/source"],
                }
            )
            await sink({"phase": "generating"})
            item.retrieved_urls = ["https://example.org/source"]
            item.reponse = (
                "Lyon has 520,774 inhabitants [source](https://example.org/source)."
            )
            item.state = "done"
            await sink({"delta": {"content": item.reponse}})

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_DATA_DIR": root,
                    "ATLAS_INTERACTION_DIR": root + "/log",
                },
            ),
            patch("services.orchestrator.chat_api.process", side_effect=process),
            TestClient(app) as client,
        ):
            for ui in (False, True):
                for stream in (False, True):
                    body = {
                        "messages": [
                            {"role": "user", "content": "Population of Lyon?"}
                        ],
                        "stream": stream,
                        "atlas_ui": ui,
                    }
                    reply = client.post("/v1/chat/completions", json=body)
                    self.assertEqual(reply.status_code, 200)
                    if stream:
                        frames = [
                            json.loads(line[6:])
                            for line in reply.text.splitlines()
                            if line.startswith("data: {")
                        ]
                        content = "".join(
                            frame["choices"][0]["delta"].get("content", "")
                            for frame in frames
                        )
                        metadata = frames[-1]["atlas"]
                    else:
                        content = reply.json()["choices"][0]["message"]["content"]
                        metadata = reply.json()["atlas"]
                    self.assertIn("520,774", content)
                    self.assertIn("https://example.org/source", content)
                    self.assertTrue(metadata["steps"])
                    self.assertTrue(metadata["sources"])
                    if not ui:
                        self.assertNotIn("I must search again", content)
                        self.assertNotIn("<details>", content)
                    elif stream:
                        self.assertIn("<details>", content)

    async def test_json_metadata_collection_preserves_atomic_provider_transport(
        self,
    ) -> None:
        from services.orchestrator.chat_api import process_request
        from services.orchestrator.model import Configuration, GatewayModel

        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )
        wire = {
            "text": "56",
            "calls": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "cost_eur": "0",
        }

        async def process(payload: ChatRequest, item: Interaction) -> None:
            model.sink = sink_context.get()
            result = await model.complete([{"role": "user", "content": "7 times 8"}], 5)
            item.reponse, item.state = result.text, "done"

        with (
            patch("services.orchestrator.chat_api.process", side_effect=process),
            patch.object(model, "post", AsyncMock(return_value=wire)) as post,
            patch(
                "services.orchestrator.model.receive", AsyncMock(return_value=wire)
            ) as stream,
        ):
            await process_request(
                ChatRequest.model_validate(
                    {"messages": [{"role": "user", "content": "7 times 8"}]}
                ),
                Interaction(),
            )
            post.assert_awaited_once()
            stream.assert_not_awaited()

    async def test_api_stream_preserves_validated_document_citation(self) -> None:
        key = "a" * 64
        citation: dict[str, object] = {
            "chunk_id": key,
            "doc_id": "document",
            "source": "policy.md",
            "text": "Seven reviewers must approve.",
        }
        url = "https://atlas.example.invalid/sources/" + key

        async def process(payload: ChatRequest, item: Interaction) -> None:
            item.chunks_recuperes = [citation]
            item.citations = [citation]
            item.reponse = "Seven reviewers must approve [Source 1](" + url + ")."
            item.state = "done"

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PUBLIC_URL": "https://atlas.example.invalid",
                    "ATLAS_DATA_DIR": root,
                    "ATLAS_INTERACTION_DIR": root + "/log",
                },
            ),
            patch("services.orchestrator.chat_api.process", side_effect=process),
            TestClient(app) as client,
        ):
            reply = client.post(
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "Who approves?"}],
                    "stream": True,
                },
            )
            self.assertEqual(reply.status_code, 200)
            frames = [
                json.loads(line[6:])
                for line in reply.text.splitlines()
                if line.startswith("data: {")
            ]
            content = "".join(
                frame["choices"][0]["delta"].get("content", "") for frame in frames
            )
            self.assertIn("[Source 1](" + url + ")", content)

    def test_browser_paths_send_explicit_signal(self) -> None:
        for name in ("chat-ui.js", "project_mode.js", "project_ui.js"):
            self.assertIn("atlas_ui", Path("services/orchestrator", name).read_text())
        self.assertFalse(
            ChatRequest.model_validate(
                {"messages": [{"role": "user", "content": "Hello"}]}
            ).atlas_ui
        )
