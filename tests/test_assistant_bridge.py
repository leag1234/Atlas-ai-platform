"""The Atlas assistant on Responses and Messages, through its Chat Completions stream."""

from collections.abc import AsyncGenerator
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from services.orchestrator.api_keys import Store
from services.orchestrator.chat_api import app


def assistant_stream() -> StreamingResponse:
    """The shape of the assistant's stream, measured on 2026-11-03 (with its atlas and event fields)."""
    base = {
        "id": "chatcmpl-x",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "atlas-qwen",
        "atlas": {},
        "event": "message",
    }
    chunks = [
        dict(
            base,
            choices=[
                {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
            ],
        ),
        dict(
            base,
            choices=[
                {"index": 0, "delta": {"content": "Hello from "}, "finish_reason": None}
            ],
        ),
        dict(
            base,
            choices=[
                {"index": 0, "delta": {"content": "Atlas."}, "finish_reason": None}
            ],
        ),
        dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
        dict(
            base,
            choices=[],
            usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        ),
    ]

    async def body() -> AsyncGenerator[str, None]:
        for chunk in chunks:
            yield "data: " + json.dumps(chunk) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


class AssistantBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(
            root / "keys.sqlite", "fictional-pepper-" * 4, lambda: 1800000000.0
        )
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_DATA_DIR": str(root),
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                    "ATLAS_USER_DAILY_EUR": "10",
                    "ATLAS_GLOBAL_DAILY_EUR": "20",
                },
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=self.store,
            )
        )
        self.sent: list[dict[str, Any]] = []

        async def fake_chat(request: Any) -> StreamingResponse:
            self.sent.append(json.loads(request._body))
            return assistant_stream()

        self.enterContext(patch("services.orchestrator.chat_api.chat", fake_chat))
        self.client = self.enterContext(TestClient(app))
        self.openai = self.key(["assistant", "model"])

    def key(self, scopes: list[str]) -> dict[str, str]:
        _, secret = self.store.create(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="assistant",
            scopes=scopes,
            created_by="operator",
            daily_requests=100,
            daily_micro_eur=1000000,
        )
        return {"Authorization": "Bearer " + secret}

    def test_responses(self) -> None:
        reply = self.client.post(
            "/api/atlas/v1/responses",
            headers=self.openai,
            json={"model": "atlas", "input": "Say hello."},
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        result = reply.json()
        self.assertEqual(result["output"][0]["content"][0]["text"], "Hello from Atlas.")
        self.assertEqual(result["usage"]["input_tokens"], 12)
        self.assertEqual(self.sent[-1]["model"], "atlas-qwen")
        self.assertEqual(self.sent[-1]["messages"][-1]["content"], "Say hello.")
        streamed = self.client.post(
            "/api/atlas/v1/responses",
            headers=self.openai,
            json={"model": "atlas-glm", "input": "Say hello.", "stream": True},
        )
        self.assertEqual(streamed.status_code, 200, streamed.text)
        types = [
            json.loads(line[6:])["type"]
            for line in streamed.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(types[0], "response.created")
        self.assertIn("response.output_text.delta", types)
        self.assertEqual(types[-1], "response.completed")
        self.assertEqual(self.sent[-1]["model"], "atlas-glm")

    def test_messages(self) -> None:
        headers = dict(self.openai, **{"anthropic-version": "2023-06-01"})
        body = {
            "model": "atlas",
            "max_tokens": 100,
            "system": "Be brief.",
            "messages": [{"role": "user", "content": "Say hello."}],
        }
        reply = self.client.post("/api/atlas/v1/messages", headers=headers, json=body)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(
            reply.json()["content"], [{"type": "text", "text": "Hello from Atlas."}]
        )
        self.assertEqual(reply.json()["stop_reason"], "end_turn")
        self.assertEqual(
            self.sent[-1]["messages"][0], {"role": "system", "content": "Be brief."}
        )

    def test_client_tools_are_refused_and_web_search_accepted(self) -> None:
        tool = {
            "type": "function",
            "name": "get_time",
            "parameters": {"type": "object"},
        }
        refused = self.client.post(
            "/api/atlas/v1/responses",
            headers=self.openai,
            json={"model": "atlas", "input": "Time?", "tools": [tool]},
        )
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertIn("atlas-dev", refused.text)
        accepted = self.client.post(
            "/api/atlas/v1/responses",
            headers=self.openai,
            json={
                "model": "atlas",
                "input": "News?",
                "tools": [{"type": "web_search"}],
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertNotIn("tools", self.sent[-1])

    def test_stop_sequences_end_the_assistant_text(self) -> None:
        # The sequence spans two chunks of the assistant stream; usage stays exact.
        headers = dict(self.openai, **{"anthropic-version": "2023-06-01"})
        body = {
            "model": "atlas",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "Hi"}],
        }
        for stops, text, reason, sequence in (
            (["om A", "zzz"], "Hello fr", "stop_sequence", "om A"),
            (["zzz"], "Hello from Atlas.", "end_turn", None),
        ):
            reply = self.client.post(
                "/api/atlas/v1/messages",
                headers=headers,
                json=body | {"stop_sequences": stops},
            )
            self.assertEqual(reply.status_code, 200, reply.text)
            answer = reply.json()
            self.assertEqual(answer["content"], [{"type": "text", "text": text}])
            self.assertEqual(
                (answer["stop_reason"], answer["stop_sequence"]), (reason, sequence)
            )
            self.assertEqual(answer["usage"], {"input_tokens": 12, "output_tokens": 4})
        streamed = self.client.post(
            "/api/atlas/v1/messages",
            headers=headers,
            json=body | {"stop_sequences": ["om A"], "stream": True},
        )
        events = [
            json.loads(line[6:])
            for line in streamed.text.splitlines()
            if line.startswith("data: ")
        ]
        shown = "".join(
            event["delta"].get("text", "")
            for event in events
            if event["type"] == "content_block_delta"
        )
        self.assertEqual(shown, "Hello fr")
        final = next(event for event in events if event["type"] == "message_delta")
        self.assertEqual(
            final["delta"], {"stop_reason": "stop_sequence", "stop_sequence": "om A"}
        )

    def test_unsupported_options_are_refused_by_name(self) -> None:
        cases: list[tuple[dict[str, Any], str]] = [
            (
                {"tools": [{"type": "file_search", "vector_store_ids": ["vs_1"]}]},
                "file_search",
            ),
            ({"prompt": {"id": "pmpt_1"}}, "prompt"),
            ({"previous_response_id": "resp_1"}, "does not store"),
        ]
        for extra, named in cases:
            reply = self.client.post(
                "/api/atlas/v1/responses",
                headers=self.openai,
                json={"model": "atlas", "input": "Hi"} | extra,
            )
            self.assertEqual(reply.status_code, 400, reply.text)
            self.assertIn(named, reply.json()["error"]["message"])
        headers = dict(self.openai, **{"anthropic-version": "2023-06-01"})
        reply = self.client.post(
            "/api/atlas/v1/messages",
            headers=headers,
            json={
                "model": "atlas",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Hi"}],
                "mcp_servers": [{"type": "url", "url": "https://example.invalid"}],
            },
        )
        self.assertEqual(reply.status_code, 400, reply.text)
        self.assertIn("mcp_servers", reply.json()["error"]["message"])

    def test_assistant_needs_the_assistant_scope(self) -> None:
        reply = self.client.post(
            "/api/atlas/v1/responses",
            headers=self.key(["model"]),
            json={"model": "atlas", "input": "Hi"},
        )
        self.assertEqual(reply.status_code, 403, reply.text)
