"""Strict json_schema on the public API: per-route guarantee or an explicit 400.

Every public model either guarantees the schema (provider-native structured
output plus Atlas validation of the final JSON with one retry) or refuses the
request with HTTP 400 before any provider call. An answer that still does not
validate after the retry is a 502, never a success; streams validate the
assembled answer before the final chunk and end with an error event.
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
from api_routing import PUBLIC_MODELS, supports_structured_output
from services.orchestrator.api_keys import Store
from services.orchestrator.chat_api import app

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "source_id": {"type": "integer"},
                },
                "required": ["text", "source_id"],
                "additionalProperties": False,
            },
        },
        "to_verify": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "claims", "to_verify", "tags"],
    "additionalProperties": False,
}
FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "support_answer", "schema": SCHEMA, "strict": True},
}
GOOD = json.dumps(
    {
        "answer": "Resolved.",
        "claims": [{"text": "Restart fixed it", "source_id": 3}],
        "to_verify": [],
        "tags": ["restart"],
    }
)


def recording(text: str | None) -> bytes:
    frames = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 10}},
    ]
    return (
        "".join("data: " + json.dumps(f) + "\n\n" for f in frames) + "data: [DONE]\n\n"
    ).encode()


class StrictJsonEnvironment:
    """Key edge with a recorded provider stream; plans record every provider call."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.store = Store(
            self.root / "keys.sqlite", "fictional-pepper-" * 4, lambda: 1800000000.0
        )
        self.plans: list[gateway.Plan] = []
        self.texts: list[str | None] = []
        self._patches: list[Any] = [
            patch.dict(
                os.environ,
                {
                    "ATLAS_DATA_DIR": str(self.root),
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                    "ATLAS_RAW_CALL_MAX_MICRO_EUR": "5000000",
                    "ATLAS_USER_DAILY_EUR": "10",
                    "ATLAS_GLOBAL_DAILY_EUR": "20",
                    "ATLAS_PROVIDER": "",
                    "SCW_GENERATIVE_BASE_URL": "https://provider.invalid/v1",
                    "MISTRAL_API_KEY": "test-only",
                    "SCW_GENERATIVE_API_KEY": "test-only",
                },
            ),
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=self.store,
            ),
            patch.object(gateway, "transport", self.transport),
        ]
        for patcher in self._patches:
            patcher.start()
        self.client = TestClient(app)
        _, secret = self.store.create(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="strict-json",
            scopes=["model"],
            created_by="operator",
            daily_requests=1000,
            daily_micro_eur=10000000,
        )
        self.headers = {"Authorization": "Bearer " + secret}

    async def transport(self, plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
        self.plans.append(plan)
        index = len(self.plans) - 1
        text = self.texts[min(index, len(self.texts) - 1)] if self.texts else None
        yield recording(text)

    def post(self, payload: dict[str, Any]) -> Any:
        return self.client.post(
            "/api/atlas/v1/chat/completions", headers=self.headers, json=payload
        )

    def body(self, model: str, **extra: Any) -> dict[str, Any]:
        return {
            "model": model,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "Answer in the requested JSON."}],
            **extra,
        }

    def close(self) -> None:
        for patcher in reversed(self._patches):
            patcher.stop()


class PerRouteSupportTableTests(unittest.TestCase):
    def test_every_public_model_is_listed_and_supported(self) -> None:
        for name in PUBLIC_MODELS:
            for effort in ("none", "low", "medium", "high"):
                with self.subTest(model=name, effort=effort):
                    self.assertTrue(supports_structured_output(name, effort))

    def test_pinned_ids_follow_their_provider_support(self) -> None:
        from api_routing import pinned_target
        from provider_settings import presets

        for name in ("mistral/zai-glm-5-3", "scaleway/glm-5.2"):
            target = pinned_target(name)
            if target is None:
                continue
            with self.subTest(name=name):
                expected = (
                    presets().get(target.provider, {}).get("json_response_format")
                    is True
                )
                self.assertEqual(supports_structured_output(name, "none"), expected)

    def test_unsupported_provider_route_is_reported(self) -> None:
        with patch(
            "api_routing.config",
            return_value={
                "structured_output": {"mistral": True, "scaleway": True},
                "usages": {
                    "atlas-code": {
                        "none": [
                            {
                                "provider": "mistral",
                                "model": "shared",
                                "context": 1000,
                            },
                            {
                                "provider": "ionos",
                                "model": "unverified",
                                "context": 1000,
                            },
                        ]
                    }
                },
            },
        ):
            self.assertFalse(supports_structured_output("atlas-code", "none"))


class StrictJsonApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = StrictJsonEnvironment()
        self.addCleanup(self.env.close)

    def test_supported_route_guarantees_schema_with_retry(self) -> None:
        self.env.texts = ['{"answer": "no"}', GOOD]
        reply = self.env.post(self.env.body("atlas-chat", response_format=FORMAT))
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(
            json.loads(reply.json()["choices"][0]["message"]["content"]),
            json.loads(GOOD),
        )
        self.assertEqual(len(self.env.plans), 2)
        for plan in self.env.plans:
            self.assertEqual(json.loads(plan.body)["response_format"], FORMAT)

    def test_invalid_after_retry_is_502_never_success(self) -> None:
        self.env.texts = ["not json at all", '{"answer": 7}']
        reply = self.env.post(self.env.body("atlas-dev", response_format=FORMAT))
        self.assertEqual(reply.status_code, 502, reply.text)
        self.assertEqual(reply.json()["error"]["type"], "invalid_response_format")
        self.assertIn("validation", reply.json()["error"]["message"])
        self.assertNotIn('"choices"', reply.text)
        self.assertEqual(len(self.env.plans), 2)

    def test_stream_validates_assembled_answer_and_ends_with_error_event(self) -> None:
        self.env.texts = ['{"answer": "no"}']
        reply = self.env.post(
            self.env.body("atlas-chat", stream=True, response_format=FORMAT)
        )
        self.assertEqual(reply.status_code, 200)
        self.assertIn('"type": "invalid_response_format"', reply.text)
        self.assertNotIn('"finish_reason": "stop"', reply.text)
        self.assertEqual(reply.text.count("data: [DONE]"), 1)

    def test_stream_success_carries_validated_json(self) -> None:
        self.env.texts = [GOOD]
        reply = self.env.post(
            self.env.body(
                "atlas-chat",
                stream=True,
                stream_options={"include_usage": True},
                response_format=FORMAT,
            )
        )
        self.assertNotIn('"error"', reply.text)
        self.assertIn('"finish_reason": "stop"', reply.text)
        self.assertIn("Restart fixed it", reply.text)

    def test_unsupported_route_refused_400_before_any_provider_call(self) -> None:
        unsupported = {
            "structured_output": {"mistral": True, "scaleway": True},
            "usages": {
                usage: {
                    effort: [
                        {"provider": "ionos", "model": "unverified", "context": 1000}
                    ]
                    for effort in ("none", "low", "medium", "high")
                }
                for usage in ("atlas-code", "atlas-chat")
            },
        }
        with patch("api_routing.config", return_value=unsupported):
            for model in ("atlas-dev", "atlas-chat", "atlas-code-2026-10-30"):
                with self.subTest(model=model):
                    reply = self.env.post(self.env.body(model, response_format=FORMAT))
                    self.assertEqual(reply.status_code, 400, reply.text)
                    error = reply.json()["error"]
                    self.assertEqual(error["type"], "unsupported_parameter")
                    self.assertEqual(error["param"], "response_format")
                    self.assertIn(
                        "json_schema strict is not supported by", error["message"]
                    )
        self.assertEqual(self.env.plans, [])

    def test_assistant_profiles_refuse_json_formats(self) -> None:
        lax = {"type": "json_schema", "json_schema": {"name": "x", "schema": SCHEMA}}
        _, secret = self.env.store.create(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="assistant-json",
            scopes=["assistant"],
            created_by="operator",
            daily_requests=1000,
            daily_micro_eur=10000000,
        )
        self.env.headers = {"Authorization": "Bearer " + secret}
        for model in ("atlas", "atlas-glm", "atlas-deepseek", "atlas-qwen"):
            for output_format in (FORMAT, lax, {"type": "json_object"}):
                with self.subTest(model=model, format=output_format["type"]):
                    reply = self.env.post(
                        self.env.body(model, response_format=output_format)
                    )
                    self.assertEqual(reply.status_code, 400, reply.text)
                    error = reply.json()["error"]
                    self.assertEqual(error["type"], "unsupported_parameter")
                    self.assertEqual(error["param"], "response_format")
                    self.assertIn("atlas-chat", error["message"])
        self.assertEqual(self.env.plans, [])

    def test_non_strict_json_schema_is_not_refused(self) -> None:
        self.env.texts = [GOOD]
        lax = {
            "type": "json_schema",
            "json_schema": {"name": "support_answer", "schema": SCHEMA},
        }
        reply = self.env.post(self.env.body("atlas-chat", response_format=lax))
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(len(self.env.plans), 1)
