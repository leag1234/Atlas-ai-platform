"""Developer JSON wire contracts, validation, retry accounting and terminal errors."""

from collections.abc import AsyncGenerator
from decimal import Decimal
from pathlib import Path
import json
from typing import Any
import unittest
from unittest.mock import patch

import dev_gateway as gateway
from devapi_support import Environment
from services.orchestrator.dev_chat import ChatInput

SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}
FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "value", "schema": SCHEMA, "strict": True},
}
BODY: dict[str, Any] = {
    "model": "atlas-code",
    "messages": [{"role": "user", "content": "Return JSON with value equal to 7."}],
    "max_tokens": 64,
}


def recording(text: str | None) -> bytes:
    # Deliberate fault injection uses the same wire envelope as the live capture.
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


class DevJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = Environment()
        self.addCleanup(self.env.close)
        self.plans: list[gateway.Plan] = []

    def replay(self, *texts: str | None) -> None:
        async def transport(plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
            index = len(self.plans)
            self.plans.append(plan)
            yield recording(texts[min(index, len(texts) - 1)])

        patcher = patch.object(gateway, "transport", transport)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, payload: dict[str, Any], path: str = "chat/completions") -> Any:
        return self.env.client.post(
            "/v1/" + path, headers=self.env.headers, json=payload
        )

    def test_recorded_live_provider_stream(self) -> None:
        fixture = json.loads(
            Path("services/model-gateway/fixtures/devapi-json.json").read_text()
        )
        self.env.raw = fixture["sse"].encode()
        response = self.post(fixture["request"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            json.loads(response.json()["choices"][0]["message"]["content"]),
            {"value": 7},
        )
        self.assertEqual(json.loads(self.env.inputs[0]), fixture["provider_request"])
        self.assertEqual(
            self.env.store.usage("alice")["charged_micro_eur"],
            fixture["cost_micro_eur"],
        )

    def test_exact_chat_and_responses_schema_shapes(self) -> None:
        self.replay('{"value": 7}')
        chat = self.post(BODY | {"response_format": FORMAT})
        self.assertEqual(chat.status_code, 200, chat.text)
        self.assertEqual(
            json.loads(chat.json()["choices"][0]["message"]["content"]), {"value": 7}
        )
        response = self.post(
            {
                "model": "atlas-code",
                "input": "Return JSON with value equal to 7.",
                "max_output_tokens": 64,
                "text": {"format": {"type": "json_schema", **FORMAT["json_schema"]}},
            },
            "responses",
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            json.loads(response.json()["output"][0]["content"][0]["text"]), {"value": 7}
        )
        for plan in self.plans:
            self.assertEqual(json.loads(plan.body)["response_format"], FORMAT)
        self.assertEqual(response.json()["text"]["format"]["name"], "value")

    def test_json_object_and_explicit_text(self) -> None:
        self.replay('{"value": 7}', "ordinary text")
        for kind in ("json_object", "text"):
            response = self.post(BODY | {"response_format": {"type": kind}})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            json.loads(self.plans[0].body)["response_format"], {"type": "json_object"}
        )

    def test_invalid_request_shapes_rejected_before_io(self) -> None:
        self.replay("{}")
        formats: list[Any] = [
            None,
            [],
            {},
            {"type": "xml"},
            {"type": "text", "schema": {}},
            {"type": "json_schema"},
            {"type": "json_schema", "json_schema": {"schema": {}}},
            {"type": "json_schema", "json_schema": {"name": "bad name", "schema": {}}},
            {"type": "json_schema", "json_schema": {"name": "x", "schema": []}},
            {
                "type": "json_schema",
                "json_schema": {"name": "x", "schema": {}, "strict": "true"},
            },
            {
                "type": "json_schema",
                "json_schema": {"name": "x", "schema": {"pattern": "x"}},
            },
        ]
        for fmt in formats:
            with self.subTest(fmt=fmt):
                response = self.post(BODY | {"response_format": fmt})
                self.assertEqual(response.status_code, 400, response.text)
        text: Any
        for text in (
            None,
            [],
            {"format": {"type": "json_schema", "schema": {}}},
            {"format": FORMAT},
        ):
            response = self.post(
                {"model": "atlas-code", "input": "JSON", "text": text}, "responses"
            )
            self.assertEqual(response.status_code, 400, response.text)
        response = self.post(
            BODY
            | {
                "messages": [{"role": "user", "content": "Seven please"}],
                "response_format": {"type": "json_object"},
            }
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.plans, [])

    def test_invalid_json_retry_and_total_usage_charge(self) -> None:
        self.replay('{"value":"wrong"}', '{"value":7}')
        response = self.post(BODY | {"response_format": FORMAT})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.plans), 2)
        self.assertEqual(response.json()["usage"]["prompt_tokens"], 100)
        self.assertEqual(response.json()["usage"]["completion_tokens"], 20)
        second = json.loads(self.plans[1].body)
        self.assertIn("type", second["messages"][-1]["content"])
        usage = self.env.store.usage("alice")
        self.assertEqual(usage["requests"], 1)
        self.assertEqual(usage["unknown_micro_eur"], 0)
        self.assertEqual(
            usage["charged_micro_eur"], sum(p.charge(50, 10) for p in self.plans)
        )
        self.assertLessEqual(
            int(str(usage["charged_micro_eur"])), self.plans[0].reserved
        )

    def test_invalid_twice_is_502_never_success(self) -> None:
        self.replay('```json\n{"value":7}\n```', '{"value":true}')
        response = self.post(BODY | {"response_format": FORMAT})
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["error"]["type"], "invalid_response_format")
        self.assertEqual(len(self.plans), 2)
        self.assertEqual(self.env.store.usage("alice")["unknown_micro_eur"], 0)

    def test_stream_validation_error_without_completion_or_retry(self) -> None:
        self.replay('{"value":"wrong"}')
        chat = self.post(BODY | {"stream": True, "response_format": FORMAT})
        self.assertEqual(chat.status_code, 200)
        self.assertIn('"content": "{', chat.text)
        self.assertIn('"type": "invalid_response_format"', chat.text)
        self.assertNotIn('"finish_reason": "stop"', chat.text)
        self.assertEqual(chat.text.count("data: [DONE]"), 1)
        response = self.post(
            {
                "model": "atlas-code",
                "input": "JSON",
                "stream": True,
                "max_output_tokens": 512,
                "text": {"format": {"type": "json_schema", **FORMAT["json_schema"]}},
            },
            "responses",
        )
        self.assertIn("response.output_text.delta", response.text)
        self.assertIn('"code": "invalid_response_format"', response.text)
        self.assertNotIn("response.completed", response.text)
        self.assertNotIn("response.output_text.done", response.text)
        self.assertEqual(len(self.plans), 2)

    def test_stream_validated_before_terminal_usage(self) -> None:
        self.replay('{"value":7}')
        response = self.post(
            BODY
            | {
                "stream": True,
                "stream_options": {"include_usage": True},
                "response_format": FORMAT,
            }
        )
        self.assertNotIn('"error"', response.text)
        self.assertIn('"finish_reason": "stop"', response.text)
        self.assertIn('"total_tokens": 60', response.text)

    def test_none_omits_provider_tools_and_recovers_empty_text(self) -> None:
        self.replay(None, "The value is 7.")
        response = self.post(
            BODY
            | {
                "tool_choice": "none",
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "save_value", "parameters": SCHEMA},
                    }
                ],
            }
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("7", response.json()["choices"][0]["message"]["content"])
        self.assertEqual(len(self.plans), 2)
        for plan in self.plans:
            for key in ("tools", "tool_choice", "parallel_tool_calls"):
                self.assertNotIn(key, json.loads(plan.body))

    def test_retry_stays_in_reservation_and_deadline(self) -> None:
        from dataclasses import replace

        plan = gateway.prepare(
            ChatInput.model_validate(BODY | {"response_format": FORMAT})
        )
        with self.assertRaises(ValueError):
            gateway.retry(plan, "type", 1)
        with self.assertRaises(ValueError):
            gateway.prepare(
                ChatInput.model_validate(
                    BODY
                    | {
                        "response_format": FORMAT,
                        "messages": [
                            {"role": "user", "content": "JSON " + "x" * 30000}
                        ],
                    }
                )
            )
        self.assertLessEqual(
            gateway.retry(plan, "type", plan.reserved).reserved, plan.reserved
        )
        expensive = replace(plan, input_rate=Decimal(1000))
        with self.assertRaises(ValueError):
            gateway.retry(expensive, "type", plan.reserved)


class JsonSchemaSubsetTests(unittest.TestCase):
    def test_supported_subset_checks_values(self) -> None:
        from packages.dev_format import check_schema, validate_json

        cases: list[tuple[dict[str, Any], Any, Any]] = [
            ({"type": "integer"}, 1.0, True),
            ({"type": "number", "minimum": 1, "maximum": 3}, 2, 4),
            ({"type": "string", "minLength": 1, "maxLength": 2}, "é", "long"),
            (
                {
                    "type": "array",
                    "items": {"type": "boolean"},
                    "minItems": 1,
                    "maxItems": 2,
                },
                [True],
                [1],
            ),
            ({"type": ["string", "null"]}, None, 1),
            ({"enum": [1, 2]}, 1, True),
            ({"const": False}, False, 0),
            ({"anyOf": [{"type": "null"}, {"type": "string"}]}, "x", []),
            (
                {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "integer"}},
                    "additionalProperties": False,
                },
                {"value": 7},
                {"value": 7, "extra": 0},
            ),
            (
                {"type": "object", "additionalProperties": {"type": "integer"}},
                {"n": 7},
                {"n": "7"},
            ),
            (
                {
                    "$defs": {"value": {"type": "integer"}},
                    "$ref": "#/$defs/value",
                    "minimum": 1,
                },
                7,
                0,
            ),
            ({"type": "object", "required": ["value"]}, {"value": 7}, {}),
        ]
        for schema, good, bad in cases:
            with self.subTest(schema=schema):
                check_schema(schema)
                self.assertIsNone(validate_json(json.dumps(good), schema))
                self.assertIsNotNone(validate_json(json.dumps(bad), schema))
        for text in ("NaN", "Infinity", '{"x":1e999}', "{} trailing", "", "[" * 2000):
            self.assertEqual(validate_json(text, {}), "invalid_json")
        self.assertEqual(validate_json("[]", None), "object_required")
        self.assertIsNone(validate_json('{"value":7}', None))

    def test_schema_rejections_are_explicit_and_bounded(self) -> None:
        from packages.dev_format import check_schema

        invalid: list[dict[str, Any]] = [
            {"pattern": ".*"},
            {"format": "email"},
            {"type": []},
            {"type": "unknown"},
            {"properties": []},
            {"required": "x"},
            {"required": ["x", "x"]},
            {"items": True},
            {"additionalProperties": None},
            {"anyOf": []},
            {"minimum": "1"},
            {"minLength": -1},
            {"maxItems": True},
            {"title": 1},
            {"description": None},
            {"enum": []},
            {"enum": [{}]},
            {"const": []},
            {"$ref": "https://example.invalid/schema"},
            {"$ref": "#/$defs/missing"},
            {"$defs": {"loop": {"$ref": "#/$defs/loop"}}, "$ref": "#/$defs/loop"},
            {"properties": {str(i): {} for i in range(513)}},
        ]
        for schema in invalid:
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                check_schema(schema)

    def test_nested_client_schema_and_complexity(self) -> None:
        from packages.dev_format import check_schema, validate_json

        schema: dict[str, Any] = {
            "type": "object",
            "properties": {"rows": {"type": "array", "items": {"$ref": "#/$defs/row"}}},
            "$defs": {
                "row": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                }
            },
        }
        check_schema(schema)
        self.assertIsNone(validate_json('{"rows":[{"value":7}]}', schema))
        self.assertEqual(validate_json('{"rows":[{"value":false}]}', schema), "type")
        self.assertEqual(
            validate_json(json.dumps([0] * 10001), {"items": {}}),
            "validation_complexity_limit",
        )


class JsonBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_cancellation_and_unknown_retry_charge(self) -> None:
        from time import monotonic
        from services.orchestrator.dev_chat import Output
        from services.orchestrator.dev_inference import drive

        for failure in ("deadline", "cancel", "retry_disconnect"):
            env = Environment()
            try:
                request = ChatInput.model_validate(
                    BODY | {"response_format": FORMAT, "stream": failure == "cancel"}
                )
                plan = gateway.prepare(request)
                request_id = env.store.reserve("alice", plan.reserved)
                calls = 0

                async def transport(
                    current: gateway.Plan,
                ) -> AsyncGenerator[bytes, None]:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise RuntimeError("disconnect")
                    yield recording("not JSON")

                with patch.object(gateway, "transport", transport):
                    frames = drive(
                        env.store,
                        "alice",
                        request_id,
                        plan,
                        Output(request_id, request),
                        monotonic() + (-1 if failure == "deadline" else 120),
                    )
                    if failure == "cancel":
                        await frames.__anext__()
                        await frames.aclose()
                    else:
                        results = [f async for f in frames]
                        self.assertIn("error", results[-1])
                self.assertEqual(
                    calls, {"deadline": 0, "cancel": 1, "retry_disconnect": 2}[failure]
                )
                self.assertEqual(
                    env.store.usage("alice")["unknown_micro_eur"], plan.reserved
                )
            finally:
                env.close()
