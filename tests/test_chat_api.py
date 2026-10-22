"""HTTP chat protocol, SSE, errors and exactly one journal row per request."""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from services.orchestrator.chat_api import app
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.stream_client import sink_context
from services.orchestrator.interactions import Interaction
from services.orchestrator.model import GatewayError
from packages.configuration import configuration_names


class ChatAPITests(unittest.TestCase):
    def test_continue_response_returns_only_the_suffix_for_json_and_sse(self) -> None:
        async def respond(request: ChatRequest, item: Interaction) -> None:
            self.assertTrue(request.continuation)
            self.assertEqual(request.user_text, "Explain the Stirling engine.")
            self.assertIn("Continue your previous answer", request.messages[-1].text)
            item.reponse = " uses a temperature difference."
            sink = sink_context.get()
            if sink is not None:
                await sink({"delta": {"content": item.reponse}})
            item.state = "done"

        payload = {
            "ui_locale": "en",
            "messages": [
                {"role": "user", "content": "Explain the Stirling engine."},
                {"role": "assistant", "content": "A Stirling engine"},
            ],
        }
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            TestClient(app) as client,
            patch("services.orchestrator.chat_api.process", side_effect=respond),
        ):
            response = client.post("/v1/chat/completions", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["choices"][0]["message"]["content"],
                " uses a temperature difference.",
            )
            response = client.post(
                "/v1/chat/completions", json={**payload, "stream": True}
            )
            self.assertEqual(response.status_code, 200)
            content = "".join(
                json.loads(line[6:])["choices"][0]["delta"].get("content", "")
                for line in response.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            )
            self.assertEqual(content, " uses a temperature difference.")

    def test_continue_response_is_localized_and_keeps_invalid_history_rejected(
        self,
    ) -> None:
        from services.orchestrator.chat_schema import CONTINUATION_INSTRUCTIONS

        questions = {
            "fr": "Explique ce moteur avec des détails utiles.",
            "en": "Explain this engine with useful details.",
            "de": "Erkläre diesen Motor mit nützlichen Details.",
            "es": "Explica este motor con detalles útiles.",
            "it": "Spiega questo motore con dettagli utili.",
        }
        for language, question in questions.items():
            request = ChatRequest.model_validate(
                {
                    "ui_locale": language,
                    "messages": [
                        {"role": "user", "content": question},
                        {"role": "assistant", "content": "Partial answer"},
                    ],
                }
            )
            self.assertTrue(request.continuation)
            self.assertTrue(
                request.messages[-1].text.startswith(
                    CONTINUATION_INSTRUCTIONS[language]
                )
            )
        for messages in (
            [{"role": "assistant", "content": ""}],
            [{"role": "assistant", "content": "  "}],
            [{"role": "system", "content": "context"}],
            [{"role": "assistant", "content": [{"type": "text", "text": "x"}]}],
        ):
            with self.assertRaises(ValueError):
                ChatRequest.model_validate({"messages": messages})

    def test_continue_response_preserves_unfinished_markdown(self) -> None:
        request = ChatRequest.model_validate(
            {
                "ui_locale": "en",
                "messages": [
                    {"role": "user", "content": "Show an example."},
                    {"role": "assistant", "content": "```python\nprint("},
                ],
            }
        )
        self.assertIn("unfinished code block", request.messages[-1].text)

    def test_configuration_errors_are_localized_without_variable_names(self) -> None:
        async def reject(request: ChatRequest, item: Interaction) -> None:
            raise GatewayError(
                "configuration_missing",
                503,
                "Missing required environment variables: SCW_DEFAULT_PROJECT_ID, LOCAL_MODEL, GPU_MAX_EUR_H",
            )

        failure = GatewayError(
            "configuration_missing",
            503,
            "Missing required environment variables: SCW_DEFAULT_PROJECT_ID, LOCAL_MODEL, GPU_MAX_EUR_H",
        )
        self.assertEqual(
            configuration_names(failure),
            ("SCW_DEFAULT_PROJECT_ID", "LOCAL_MODEL", "GPU_MAX_EUR_H"),
        )

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            TestClient(app) as client,
            patch("services.orchestrator.chat_api.process", side_effect=reject),
        ):
            for stable in ("1", "0"):
                for stream in (False, True):
                    with patch.dict(os.environ, {"ATLAS_STABLE": stable}):
                        response = client.post(
                            "/v1/chat/completions",
                            json={
                                "ui_locale": "en",
                                "lang": "en",
                                "stream": stream,
                                "messages": [
                                    {"role": "user", "content": "Generate an image"}
                                ],
                            },
                        )
                    if stable == "1":
                        # Guests never see setup diagnostics.
                        self.assertIn("temporarily unavailable", response.text)
                        for prefix in ("SCW_", "LOCAL_MODEL", "GPU_"):
                            self.assertNotIn(prefix, response.text)
                    else:
                        # The owner instance keeps the explicit M20 diagnostic.
                        self.assertIn("LOCAL_MODEL", response.text)

    def test_internal_size_limit_reaches_json_and_stream_with_measurements(
        self,
    ) -> None:
        from packages.limits import LimitError

        async def reject(request: ChatRequest, item: Interaction) -> None:
            raise LimitError("source_response_limit", 800001, 800000, "bytes")

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            TestClient(app) as client,
            patch("services.orchestrator.chat_api.process", side_effect=reject),
        ):
            for streaming in (False, True):
                result = client.post(
                    "/v1/chat/completions",
                    json={
                        "messages": [{"role": "user", "content": "Explain the source"}],
                        "stream": streaming,
                    },
                )
                self.assertEqual(result.status_code, 200 if streaming else 502)
                for value in ("source_response_limit", "800001", "800000", "bytes"):
                    self.assertIn(value, result.text)
                self.assertNotIn('"finish_reason": "stop"', result.text)

    def test_http_deadlines_follow_model_for_json_and_stream(self) -> None:
        limits: list[float] = []

        @asynccontextmanager
        async def deadline(seconds: float) -> AsyncIterator[None]:
            limits.append(seconds)
            yield

        async def respond(request: ChatRequest, item: Interaction) -> None:
            item.reponse = "Complete useful answer"
            item.state = "done"

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            TestClient(app) as client,
            patch("services.orchestrator.chat_api.process", side_effect=respond),
            patch("services.orchestrator.chat_api.request_deadline", deadline),
            patch("services.orchestrator.chat_stream.request_deadline", deadline),
        ):
            for profile, maximum in (
                ("atlas-qwen", 120),
                ("atlas-glm", 120),
                ("atlas-deepseek", 120),
            ):
                for streaming in (False, True):
                    before = len(limits)
                    response = client.post(
                        "/v1/chat/completions",
                        json={
                            "model": profile,
                            "stream": streaming,
                            "messages": [
                                {"role": "user", "content": "Explain this limit"}
                            ],
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(len(limits), before + 1)
                    self.assertGreater(limits[-1], maximum - 1)
                    self.assertLessEqual(limits[-1], maximum)

    def test_incomplete_deep_retry_status_survives_terminal_response(self) -> None:
        async def respond(request: ChatRequest, item: Interaction) -> None:
            item.reponse = "Complete answer"
            item.reasoning_retried = True
            item.state = "done"

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            TestClient(app) as client,
            patch("services.orchestrator.chat_api.process", side_effect=respond),
        ):
            for streaming in (False, True):
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "atlas-glm",
                        "stream": streaming,
                        "messages": [{"role": "user", "content": "Explain this limit"}],
                    },
                )
                if streaming:
                    rows = [
                        json.loads(line[6:])
                        for line in response.text.splitlines()
                        if line.startswith("data: ") and line != "data: [DONE]"
                    ]
                    metadata = rows[-1]["atlas"]
                else:
                    metadata = response.json()["atlas"]
                self.assertTrue(metadata["reasoning_retried"])
                self.assertIn("inachevée", metadata["status"])
                self.assertIn("reprise complète", metadata["status"])

    def test_json_sse_history_and_errors(self) -> None:
        async def respond(request: ChatRequest, item: Interaction) -> None:
            self.assertEqual(request.messages[-1].content, "Bonjour")
            item.reponse = "Réponse avec preuve"
            sink = sink_context.get()
            if sink is not None:
                await sink({"delta": {"content": item.reponse}})
            item.state = "done"
            item.tokens = {"in": 20, "out": 5}
            item.modele_utilise = "escalade"
            item.route_decision = "complexe"

        payload = {
            "model": "atlas-qwen",
            "messages": [{"role": "user", "content": "Bonjour"}],
        }
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            TestClient(app) as client,
        ):
            self.assertEqual(
                client.get("/v1/models").json()["data"][0]["id"], "atlas-qwen"
            )
            with patch("services.orchestrator.chat_api.process", side_effect=respond):
                response = client.post("/v1/chat/completions", json=payload)
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(
                    data["choices"][0]["message"]["content"], "Réponse avec preuve"
                )
                self.assertEqual(data["usage"]["total_tokens"], 25)
                response = client.post(
                    "/v1/chat/completions", json={**payload, "stream": True}
                )
                self.assertIn("text/event-stream", response.headers["content-type"])
                chunks = [
                    line[6:]
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                self.assertEqual(chunks[-1], "[DONE]")
                self.assertEqual(
                    json.loads(chunks[0])["object"], "chat.completion.chunk"
                )
                self.assertEqual(json.loads(chunks[0])["event"]["type"], "status")
                content_chunks = [
                    json.loads(chunk)["choices"][0]["delta"]["content"]
                    for chunk in chunks[:-1]
                    if "content" in json.loads(chunk)["choices"][0]["delta"]
                ]
                self.assertEqual(content_chunks, ["Réponse avec preuve"])
            with patch(
                "services.orchestrator.chat_api.process",
                side_effect=RuntimeError("private-detail"),
            ):
                response = client.post("/v1/chat/completions", json=payload)
                self.assertEqual(response.status_code, 502)
                self.assertNotIn("private-detail", response.text)
            self.assertEqual(
                client.post("/v1/chat/completions", content="{").status_code, 400
            )
            self.assertEqual(
                client.post("/v1/chat/completions", json={"messages": []}).status_code,
                400,
            )
            self.assertEqual(
                client.post("/v1/chat/completions", content="x" * 200000).status_code,
                413,
            )
            rows = [
                json.loads(line)
                for path in Path(root).glob("*.jsonl")
                for line in path.read_text().splitlines()
            ]
            self.assertEqual(len(rows), 6)
            self.assertEqual(rows[0]["question"], "Bonjour")
            self.assertEqual(rows[2]["erreurs"], ["provider_error"])
            self.assertNotIn("private-detail", json.dumps(rows))
            self.assertGreater(rows[0]["latence_ms"]["total"], 0)
            with patch("services.orchestrator.chat_api.process", new=AsyncMock()):
                response = client.post("/v1/chat/completions", json=payload)
                self.assertEqual(response.status_code, 504)
                self.assertEqual(response.json()["error"]["code"], "request_stopped")

    def test_timeout_cancellation_and_safe_source_html(self) -> None:
        import asyncio

        async def slow(*args: object) -> None:
            await asyncio.sleep(30)
            self.fail("disconnected request was not cancelled")

        payload = {"messages": [{"role": "user", "content": "Bonjour"}]}
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            TestClient(app) as client,
        ):
            with patch(
                "services.orchestrator.chat_api.process", side_effect=TimeoutError
            ):
                self.assertEqual(
                    client.post("/v1/chat/completions", json=payload).status_code, 504
                )
            with (
                patch("services.orchestrator.chat_api.process", side_effect=slow),
                patch(
                    "starlette.requests.Request.is_disconnected",
                    new=AsyncMock(return_value=True),
                ),
            ):
                self.assertEqual(
                    client.post("/v1/chat/completions", json=payload).status_code, 499
                )
            self.assertEqual(client.get("/sources/bad").status_code, 400)
            with patch(
                "services.orchestrator.chat_api.source",
                new=AsyncMock(
                    return_value={
                        "source": "<script>",
                        "text": "<script>alert(1)</script>",
                    }
                ),
            ):
                response = client.get("/sources/" + "a" * 64)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("<script>", response.text)
                self.assertIn("&lt;script&gt;", response.text)
            with patch(
                "services.orchestrator.chat_api.source",
                new=AsyncMock(side_effect=ValueError),
            ):
                self.assertEqual(client.get("/sources/" + "a" * 64).status_code, 404)
            rows = [
                json.loads(line)
                for p in Path(root).glob("*.jsonl")
                for line in p.read_text().splitlines()
            ]
            self.assertEqual([r["erreurs"] for r in rows], [["timeout"], ["cancelled"]])


class StopDiagnosticTests(unittest.TestCase):
    def test_http_and_journal_keep_measured_stop(self) -> None:
        async def stopped(request: ChatRequest, item: Interaction) -> None:
            item.state = "stopped"
            item.reponse = (
                "Tools executed: 10; limit 10. Cost reserved: 0.09 EUR; limit 0.10 EUR."
            )

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch("services.orchestrator.chat_api.process", stopped),
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "Explain the evidence"}]
                },
            )
            self.assertEqual(response.status_code, 504)
            self.assertIn("10; limit 10", response.json()["error"]["message"])
            row = json.loads(next(Path(root).glob("*.jsonl")).read_text())
            self.assertIn("0.09 EUR; limit 0.10 EUR", row["reponse"])
            self.assertEqual(row["question"], "Explain the evidence")
