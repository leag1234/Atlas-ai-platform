"""Public Messages protocol and accounting through the mounted key edge."""

from collections.abc import AsyncIterator
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from dev_gateway import Plan, prepare_api as real_prepare, events as real_events
from services.orchestrator.api_keys import Store
from services.orchestrator.chat_api import app
from services.orchestrator.daily_budget import DailyLedger


class MessagesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = [1800000000.0]
        self.store = Store(
            self.root / "keys.sqlite", "fictional-pepper-" * 4, lambda: self.now[0]
        )
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_DATA_DIR": str(self.root),
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                    "ATLAS_USER_DAILY_EUR": "10",
                    "ATLAS_GLOBAL_DAILY_EUR": "20",
                    "ATLAS_API_DEFAULT_RAW_MODEL": "",
                },
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=self.store,
            )
        )
        self.row, self.key = self.create()
        self.client = self.enterContext(TestClient(app))
        self.body: dict[str, Any] = {
            "model": "claude-fictional-client",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Calculate 7 times 8"}],
            "tools": [
                {
                    "name": "calculate",
                    "input_schema": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                    },
                }
            ],
        }
        self.prepared: list[Any] = []
        self.prepare = self.enterContext(
            patch(
                "services.orchestrator.dev_inference.gateway.prepare_api",
                side_effect=self.plan,
            )
        )
        self.events = self.enterContext(
            patch(
                "services.orchestrator.dev_inference.gateway.events",
                side_effect=self.frames,
            )
        )

    def create(self, **values: Any) -> tuple[dict[str, Any], str]:
        defaults: dict[str, Any] = dict(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="protocol",
            scopes=["model"],
            created_by="operator",
            daily_requests=20,
        )
        return self.store.create(**(defaults | values))

    def plan(self, request: Any, usage: str, api_key_id: str) -> Plan:
        self.prepared.append(request)
        return Plan(
            "{}",
            "https://example.invalid/v1",
            1000,
            Decimal(1),
            Decimal(1),
            provider="mistral",
            model="fictional-model",
        )

    async def frames(self, plan: Plan) -> AsyncIterator[dict[str, Any]]:
        if self.prepared[-1].messages[-1].role == "tool":
            yield {
                "choices": [
                    {"index": 0, "delta": {"content": "56"}, "finish_reason": None}
                ]
            }
            reason = "stop"
        else:
            for function in (
                {"name": "calculate", "arguments": '{"value":'},
                {"arguments": "56}"},
            ):
                yield {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        **(
                                            {"id": "call-fictional"}
                                            if "name" in function
                                            else {}
                                        ),
                                        "function": function,
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            reason = "tool_calls"
        yield {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}
        yield {
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            "cost_micro_eur": 12,
            "served_by": "mistral/fictional-model",
        }

    def post(self, path: str = "/messages", **kwargs: Any) -> Any:
        kwargs.setdefault(
            "headers", {"x-api-key": self.key, "anthropic-version": "2023-06-01"}
        )
        kwargs.setdefault("json", self.body)
        return self.client.post("/api/atlas/v1" + path, **kwargs)

    def assert_error(self, reply: Any, status: int, kind: str) -> None:
        self.assertEqual(reply.status_code, status, reply.text)
        self.assertEqual(reply.json()["type"], "error")
        self.assertEqual(reply.json()["error"]["type"], kind)
        self.assertEqual(reply.headers["cache-control"], "no-store")

    def test_json_tool_round_trip_and_both_ledgers(self) -> None:
        reply = self.post()
        self.assertEqual(reply.status_code, 200, reply.text)
        value = reply.json()
        self.assertEqual(value["stop_reason"], "tool_use")
        self.assertEqual(value["served_by"], "mistral/fictional-model")
        self.assertEqual(value["usage"], {"input_tokens": 10, "output_tokens": 2})
        self.assertEqual(
            value["content"],
            [
                {
                    "type": "tool_use",
                    "id": "call-fictional",
                    "name": "calculate",
                    "input": {"value": 56},
                }
            ],
        )
        history = self.body["messages"] + [
            {"role": "assistant", "content": value["content"]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-fictional",
                        "content": "56",
                    }
                ],
            },
        ]
        reply = self.post("/v1/messages", json=self.body | {"messages": history})
        self.assertEqual(reply.json()["content"], [{"type": "text", "text": "56"}])
        self.assertEqual(reply.json()["stop_reason"], "end_turn")
        self.assertEqual(self.prepared[-1].messages[-1].tool_call_id, "call-fictional")
        self.assertEqual(self.prepared[-1].messages[-1].content, "56")
        usage = self.store.usage(self.row["id"])
        self.assertEqual(usage["requests"], 2)
        self.assertEqual(usage["charged_micro_eur"], 24)
        self.assertEqual(DailyLedger().summary()[0]["measured_eur"], 0.000024)

    def test_streamed_tool_arguments_and_text_round_trip(self) -> None:
        reply = self.post("/v1/messages", json=self.body | {"stream": True})
        self.assertEqual(reply.status_code, 200, reply.text)
        events = [
            json.loads(line[6:])
            for line in reply.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            [e["type"] for e in events],
            [
                "message_start",
                "content_block_start",
                "content_block_delta",
                "content_block_delta",
                "content_block_stop",
                "message_delta",
                "message_stop",
            ],
        )
        block = events[1]["content_block"]
        block["input"] = json.loads(
            "".join(
                e["delta"]["partial_json"]
                for e in events
                if e["type"] == "content_block_delta"
            )
        )
        self.assertEqual(block["input"], {"value": 56})
        self.assertEqual(events[-2]["delta"]["stop_reason"], "tool_use")
        self.assertEqual(events[-1]["served_by"], "mistral/fictional-model")
        history = self.body["messages"] + [
            {"role": "assistant", "content": [block]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": block["id"], "content": "56"}
                ],
            },
        ]
        reply = self.post(json=self.body | {"stream": True, "messages": history})
        events = [
            json.loads(line[6:])
            for line in reply.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            "".join(
                e["delta"].get("text", "")
                for e in events
                if e["type"] == "content_block_delta"
            ),
            "56",
        )
        self.assertEqual(events[-2]["delta"]["stop_reason"], "end_turn")
        self.assertEqual(events[-2]["usage"]["output_tokens"], 2)
        self.assertEqual(self.store.usage(self.row["id"])["requests"], 2)

    def test_errors_auth_scope_quotas_expiry_and_revocation(self) -> None:
        for path in (
            "/messages",
            "/v1/messages",
            "/messages/count_tokens",
            "/v1/messages/count_tokens",
        ):
            self.assert_error(self.post(path, headers={}), 401, "authentication_error")
            _, assistant = self.create(scopes=["assistant"])
            self.assert_error(
                self.post(path, headers={"Authorization": "Bearer " + assistant}),
                403,
                "permission_error",
            )
        row, key = self.create(daily_requests=1)
        auth = {"Authorization": "Bearer " + key}
        self.assertEqual(self.post(headers=auth).status_code, 200)
        self.assert_error(self.post(headers=auth), 429, "rate_limit_error")
        self.assertEqual(self.events.call_count, 1)
        self.now[0] = float(row["expires_at"])
        self.assert_error(self.post(headers=auth), 401, "authentication_error")
        self.assert_error(
            self.post("/v1/messages/count_tokens", headers=auth),
            401,
            "authentication_error",
        )
        self.now[0] -= 1
        self.store.revoke(row["id"])
        self.assert_error(self.post(headers=auth), 401, "authentication_error")
        self.assert_error(
            self.post("/v1/messages/count_tokens", headers=auth),
            401,
            "authentication_error",
        )
        self.assertEqual(self.events.call_count, 1)

    def test_count_is_local_and_validated(self) -> None:
        body = {k: v for k, v in self.body.items() if k != "max_tokens"}
        values = [
            self.post(path, json=body)
            for path in ("/messages/count_tokens", "/v1/messages/count_tokens")
        ]
        for reply in values:
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertGreater(reply.json()["input_tokens"], 512)
        self.assertEqual(values[0].json(), values[1].json())
        self.assert_error(
            self.post("/messages/count_tokens", json=body | {"messages": []}),
            400,
            "invalid_request_error",
        )
        self.events.assert_not_called()
        self.assertEqual(self.store.usage(self.row["id"])["requests"], 0)

    def test_header_precedence_duplicates_and_browser_rejection(self) -> None:
        for headers in (
            {"Authorization": "Basic invalid", "x-api-key": self.key},
            {"x-api-key": self.key, "Cookie": "session=fictional"},
            {"x-api-key": self.key, "Origin": "https://example.invalid"},
            [("x-api-key", self.key), ("x-api-key", self.key)],
        ):
            self.assert_error(self.post(headers=headers), 401, "authentication_error")
        self.assertEqual(
            self.post(
                headers={"Authorization": "Bearer " + self.key, "x-api-key": "invalid"}
            ).status_code,
            200,
        )

    def test_member_budget_and_provider_errors(self) -> None:
        with patch.dict(os.environ, {"ATLAS_USER_DAILY_EUR": "0.000001"}):
            reply = self.post()
        self.assert_error(reply, 429, "rate_limit_error")
        self.events.assert_not_called()
        self.assertIn("limit", reply.json()["error"])
        self.assert_error(
            self.post(json=self.body | {"project_id": "fictional"}),
            403,
            "permission_error",
        )
        self.assert_error(self.post(json=[]), 400, "invalid_request_error")
        self.assert_error(self.post(content=b"{"), 400, "invalid_request_error")

    def test_models_resolve_to_public_usage_routes(self) -> None:
        for name, expected in (
            ("atlas-code", "atlas-code"),
            ("atlas-chat", "atlas-chat"),
            ("claude-fictional-client", "atlas-code"),
            ("gpt-fictional-client", "atlas-code"),
        ):
            reply = self.post(json=self.body | {"model": name})
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(reply.json()["model"], expected)
        self.assert_error(
            self.post(json=self.body | {"model": "raw/unknown"}),
            400,
            "invalid_request_error",
        )

    def test_actual_public_plan_with_recorded_provider_stream(self) -> None:
        raw = json.loads(Path("tests/fixtures/devapi-streams.json").read_text())[
            "named"
        ].encode()
        plans: list[Plan] = []

        async def transport(plan: Plan) -> AsyncIterator[bytes]:
            plans.append(plan)
            for offset in range(0, len(raw), 40):
                yield raw[offset : offset + 40]

        self.prepare.side_effect = real_prepare
        self.events.side_effect = real_events
        with (
            patch.dict(
                os.environ, {"SCW_GENERATIVE_BASE_URL": "https://example.invalid/v1"}
            ),
            patch("dev_gateway.transport", transport),
        ):
            reply = self.post(
                json=self.body
                | {
                    "tools": [
                        {"name": "save_value", "input_schema": {"type": "object"}}
                    ]
                }
            )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["content"][0]["input"], {"value": 7})
        self.assertEqual(
            reply.json()["usage"], {"input_tokens": 166, "output_tokens": 7}
        )
        sent = json.loads(plans[0].body)
        self.assertEqual(
            sent["messages"],
            [{"role": "user", "content": "Calculate 7 times 8"}],
        )
        self.assertEqual(sent["model"], "zai-glm-5-3-flash")
        self.assertEqual(reply.json()["model"], "atlas-code")
        usage = self.store.usage(self.row["id"])
        self.assertEqual(usage["requests"], 1)
        self.assertEqual(usage["charged_micro_eur"], plans[0].charge(166, 7))
        self.assertEqual(usage["unknown_micro_eur"], 0)

    def test_provider_failure_preserves_reserve_and_sse_error(self) -> None:
        async def broken(plan: Plan) -> AsyncIterator[dict[str, Any]]:
            yield {
                "choices": [
                    {"index": 0, "delta": {"content": "Partial"}, "finish_reason": None}
                ]
            }
            raise RuntimeError("fictional_provider_failure")

        self.events.side_effect = broken
        self.assert_error(self.post(), 502, "api_error")
        reply = self.post(json=self.body | {"stream": True})
        self.assertEqual(reply.status_code, 200)
        events = [
            json.loads(line[6:])
            for line in reply.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            events[-1],
            {
                "type": "error",
                "error": {"type": "api_error", "message": "provider_error"},
            },
        )
        self.assertNotIn("message_stop", [e["type"] for e in events])
        usage = self.store.usage(self.row["id"])
        self.assertEqual(usage["requests"], 2)
        self.assertEqual(usage["unknown_micro_eur"], 2000)
        self.assertEqual(DailyLedger().summary()[0]["measured_eur"], 0.002)

    def test_body_limit_and_key_money_quota(self) -> None:
        self.assert_error(
            self.post(content=b"x" * 1048577, json=None), 413, "request_too_large"
        )
        _, key = self.create(daily_micro_eur=1)
        reply = self.post(headers={"x-api-key": key})
        self.assert_error(reply, 429, "rate_limit_error")
        self.assertEqual(reply.json()["error"]["daily_micro_eur"], 1)
        self.events.assert_not_called()

    def test_tool_cache_hint_choice_and_invalid_history(self) -> None:
        tool = self.body["tools"][0] | {"cache_control": {"type": "ephemeral"}}
        reply = self.post(
            json=self.body
            | {
                "tools": [tool],
                "tool_choice": {
                    "type": "tool",
                    "name": "calculate",
                    "disable_parallel_tool_use": True,
                },
            }
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(
            self.prepared[-1].tool_choice,
            {"type": "function", "function": {"name": "calculate"}},
        )
        self.assertFalse(self.prepared[-1].parallel_tool_calls)
        self.assert_error(
            self.post(
                json=self.body
                | {"tools": [tool | {"cache_control": {"type": "invalid"}}]}
            ),
            400,
            "invalid_request_error",
        )
        invalid = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "orphan", "content": "56"}
                ],
            }
        ]
        self.assert_error(
            self.post(json=self.body | {"messages": invalid}),
            400,
            "invalid_request_error",
        )
        self.assertEqual(self.events.call_count, 1)
