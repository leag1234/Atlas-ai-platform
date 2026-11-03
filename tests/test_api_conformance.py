"""API parity contract: every documented request parameter is either accepted or refused by name.

One request per parameter of the OpenAI Chat Completions and Responses specifications and of the
Anthropic Messages API (lists of 2026-11-02, see docs/developer-api.md "OpenAI and Anthropic
compatibility"), through the key edge with a recorded provider stream. A change that refuses a
parameter Atlas accepts, or silently accepts one it refuses, fails here.
"""

from collections.abc import AsyncGenerator
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import dev_gateway as gateway
from services.orchestrator.api_keys import Store
from services.orchestrator.chat_api import app

FUNCTION = {
    "name": "get_time",
    "parameters": {"type": "object", "properties": {"tz": {"type": "string"}}},
}

# Chat Completions on the direct models: accepted parameters, with a valid value.
CHAT_ACCEPTED: dict[str, Any] = {
    "frequency_penalty": 0.1,
    "presence_penalty": 0.1,
    "logprobs": True,
    "metadata": {"k": "v"},
    "parallel_tool_calls": False,
    "prompt_cache_key": "k1",
    "prompt_cache_retention": "24h",
    "reasoning_effort": "low",
    "response_format": {"type": "text"},
    "safety_identifier": "user-1",
    "seed": 1,
    "service_tier": "auto",
    "stop": ["\n\n"],
    "store": False,
    "stream": True,
    "stream_options": {"include_usage": True},
    "temperature": 0.2,
    "tool_choice": "auto",
    "tools": [{"type": "function", "function": FUNCTION}],
    "top_p": 0.9,
    "user": "user-1",
    "verbosity": "low",
    "functions": [FUNCTION],
    "web_search_options": {},
}
# Refused, and the error names the parameter.
CHAT_REFUSED: dict[str, Any] = {
    "logit_bias": {"50256": -100},
    "audio": {"voice": "alloy", "format": "mp3"},
    "modalities": ["text", "audio"],
    "prediction": {"type": "content", "content": "OK"},
    "moderation": {"mode": "strict"},
    "prompt_cache_options": {"mode": "x"},
}
RESPONSES_ACCEPTED: dict[str, Any] = {
    "background": False,
    "context_management": [{"type": "compaction"}],
    "include": ["reasoning.encrypted_content"],
    "instructions": "Be brief.",
    "max_output_tokens": 16,
    "max_tool_calls": 2,
    "metadata": {"k": "v"},
    "parallel_tool_calls": False,
    "prompt_cache_key": "k1",
    "prompt_cache_retention": "24h",
    "reasoning": {"effort": "low"},
    "safety_identifier": "user-1",
    "service_tier": "auto",
    "store": True,
    "stream": True,
    "stream_options": {"include_obfuscation": False},
    "temperature": 0.2,
    "text": {"format": {"type": "text"}},
    "tool_choice": "auto",
    "tools": [{"type": "function", **FUNCTION}],
    "top_logprobs": 2,
    "top_p": 0.9,
    "truncation": "auto",
    "user": "user-1",
}
# Refused with storage_not_supported: Atlas stores nothing.
RESPONSES_REFUSED: dict[str, Any] = {
    "previous_response_id": "resp_1",
    "conversation": "conv_1",
    "background": True,
}
MESSAGES_ACCEPTED: dict[str, Any] = {
    "cache_control": {"type": "ephemeral"},
    "metadata": {"user_id": "user-1"},
    "output_config": {"effort": "low"},
    "service_tier": "auto",
    "stop_sequences": ["\n\n"],
    "stream": True,
    "system": "Be brief.",
    "temperature": 0.2,
    "thinking": {"type": "disabled"},
    "tool_choice": {"type": "auto"},
    "tools": [{"name": "get_time", "input_schema": {"type": "object"}}],
    "top_k": 5,
    "top_p": 0.9,
}
MESSAGES_REFUSED: dict[str, Any] = {
    "container": "container_1",
    "inference_geo": "eu",
    "speed": "fast",
    "thinking": {"type": "enabled", "budget_tokens": 1024},
}


class ConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = Store(
            root / "keys.sqlite", "fictional-pepper-" * 4, lambda: 1800000000.0
        )
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_DATA_DIR": str(root),
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                    "ATLAS_RAW_CALL_MAX_MICRO_EUR": "5000000",
                    "ATLAS_USER_DAILY_EUR": "10",
                    "ATLAS_GLOBAL_DAILY_EUR": "20",
                    "ATLAS_PROVIDER": "",
                    "SCW_GENERATIVE_BASE_URL": "https://provider.invalid/v1",
                },
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=store,
            )
        )
        self.enterContext(patch.object(gateway, "transport", self.transport))
        self.client = self.enterContext(TestClient(app))
        _, secret = store.create(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="conformance",
            scopes=["model"],
            created_by="operator",
            daily_requests=1000,
            daily_micro_eur=10000000,
        )
        self.openai = {"Authorization": "Bearer " + secret}
        self.anthropic = {"x-api-key": secret, "anthropic-version": "2023-06-01"}

    async def transport(self, plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
        for frame in (
            {
                "choices": [
                    {"index": 0, "delta": {"content": "OK"}, "finish_reason": None}
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 1}},
        ):
            yield ("data: " + json.dumps(frame) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    def chat(self, name: str, value: Any) -> Any:
        body: dict[str, Any] = {
            "model": "atlas-dev",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply OK."}],
            name: value,
        }
        return self.client.post(
            "/api/atlas/v1/chat/completions", headers=self.openai, json=body
        )

    def test_chat_completions(self) -> None:
        for name, value in CHAT_ACCEPTED.items():
            with self.subTest(accepted=name):
                reply = self.chat(name, value)
                self.assertEqual(reply.status_code, 200, reply.text)
        body = {
            "model": "atlas-dev",
            "max_completion_tokens": 16,
            "messages": [{"role": "user", "content": "Reply OK."}],
        }
        reply = self.client.post(
            "/api/atlas/v1/chat/completions", headers=self.openai, json=body
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        for name, value in CHAT_REFUSED.items():
            with self.subTest(refused=name):
                reply = self.chat(name, value)
                self.assertEqual(reply.status_code, 400, reply.text)
                self.assertIn(name, reply.text)

    def test_responses(self) -> None:
        for name, value in RESPONSES_ACCEPTED.items():
            with self.subTest(accepted=name):
                body = {"model": "atlas-dev", "input": "Reply OK.", name: value}
                reply = self.client.post(
                    "/api/atlas/v1/responses", headers=self.openai, json=body
                )
                self.assertEqual(reply.status_code, 200, reply.text)
        for name, value in RESPONSES_REFUSED.items():
            with self.subTest(refused=name):
                body = {"model": "atlas-dev", "input": "Reply OK.", name: value}
                reply = self.client.post(
                    "/api/atlas/v1/responses", headers=self.openai, json=body
                )
                self.assertEqual(reply.status_code, 400, reply.text)
                self.assertIn("storage_not_supported", reply.text)

    def test_messages(self) -> None:
        for name, value in MESSAGES_ACCEPTED.items():
            with self.subTest(accepted=name):
                body = {
                    "model": "atlas-dev",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Reply OK."}],
                    name: value,
                }
                reply = self.client.post(
                    "/api/atlas/v1/messages", headers=self.anthropic, json=body
                )
                self.assertEqual(reply.status_code, 200, reply.text)
        for name, value in MESSAGES_REFUSED.items():
            with self.subTest(refused=name):
                body = {
                    "model": "atlas-dev",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Reply OK."}],
                    name: value,
                }
                reply = self.client.post(
                    "/api/atlas/v1/messages", headers=self.anthropic, json=body
                )
                self.assertEqual(reply.status_code, 400, reply.text)
                if name != "thinking":
                    # thinking is a known field; only an enabled value is refused.
                    self.assertIn(name, reply.text)

    def test_routes(self) -> None:
        for headers in (self.openai, self.anthropic):
            self.assertEqual(
                self.client.get("/api/atlas/v1/models", headers=headers).status_code,
                200,
            )
            self.assertEqual(
                self.client.get(
                    "/api/atlas/v1/models/atlas-dev", headers=headers
                ).status_code,
                200,
            )
        counted = self.client.post(
            "/api/atlas/v1/responses/input_tokens",
            headers=self.openai,
            json={"model": "atlas-dev", "input": "Hi"},
        )
        self.assertEqual(counted.status_code, 200, counted.text)
        for method, path in (
            ("GET", "/responses/resp_1"),
            ("DELETE", "/responses/resp_1"),
            ("GET", "/chat/completions"),
        ):
            reply = self.client.request(
                method, "/api/atlas/v1" + path, headers=self.openai
            )
            self.assertEqual(reply.status_code, 404, reply.text)
            self.assertIn("storage_not_supported", reply.text)
