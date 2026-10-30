"""Public Responses protocol and shared key/member accounting, without live I/O."""

from collections.abc import AsyncGenerator
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import dev_gateway as gateway
from serverless import ServerlessPolicy
from services.orchestrator.api_keys import Store
from services.orchestrator.chat_api import app
from services.orchestrator.daily_budget import DailyLedger


class KeyResponsesTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = [1800000000.0]
        self.store = Store(
            root / "keys.sqlite", "fictional-pepper-" * 4, lambda: self.now[0]
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
                    "ATLAS_PROVIDER": "",
                    "SCW_GENERATIVE_BASE_URL": "https://provider.invalid/v1",
                    "ATLAS_API_RESPONSES_MODEL": "atlas-code",
                },
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=self.store,
            )
        )
        self.client = self.enterContext(TestClient(app))
        self.inputs: list[dict[str, Any]] = []
        self.enterContext(patch.object(gateway, "transport", self.transport))
        self.row, self.headers = self.key()
        self.payload: dict[str, Any] = {
            "model": "atlas-code",
            "input": "Save seven",
            "store": False,
            "tools": [
                {
                    "type": "function",
                    "name": "save_value",
                    "parameters": {"type": "object"},
                }
            ],
        }

    def key(self, **options: Any) -> tuple[dict[str, Any], dict[str, str]]:
        values: dict[str, Any] = dict(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="coding",
            scopes=["model"],
            created_by="operator",
            daily_requests=20,
            daily_micro_eur=1000000,
        )
        row, secret = self.store.create(**(values | options))
        return row, {"Authorization": "Bearer " + secret}

    async def transport(self, plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
        body = json.loads(plan.body)
        self.inputs.append(body)
        chunks: list[dict[str, Any]]
        if body["messages"][-1]["role"] == "tool":
            chunks = [{"content": "Stored seven."}]
            reason = "stop"
        else:
            chunks = [
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_fixture",
                            "type": "function",
                            "function": {
                                "name": "save_value",
                                "arguments": '{"value":',
                            },
                        }
                    ]
                },
                {"tool_calls": [{"index": 0, "function": {"arguments": "7}"}}]},
            ]
            reason = "tool_calls"
        for delta in chunks:
            yield (
                "data: "
                + json.dumps(
                    {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                )
                + "\n\n"
            ).encode()
        yield (
            "data: "
            + json.dumps(
                {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}
            )
            + "\n\n"
        ).encode()
        yield b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}\n\ndata: [DONE]\n\n'

    def post(self, body: Any, headers: dict[str, str] | None = None) -> Any:
        return self.client.post(
            "/api/atlas/v1/responses",
            json=body,
            headers=self.headers if headers is None else headers,
        )

    def result(self, reply: Any, stream: bool) -> dict[str, Any]:
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.headers["cache-control"], "no-store")
        self.assertEqual(
            reply.headers["x-atlas-served-by"], "mistral/zai-glm-5-3-flash"
        )
        if not stream:
            result = dict(reply.json())
            self.assertEqual(result["served_by"], "mistral/zai-glm-5-3-flash")
            return result
        self.assertEqual(reply.headers["x-accel-buffering"], "no")
        events = [
            json.loads(line[6:])
            for line in reply.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(events[0]["type"], "response.created")
        self.assertEqual(events[0]["response"]["output"], [])
        self.assertEqual(events[-1]["type"], "response.completed")
        self.assertEqual(
            events[-1]["response"]["served_by"], "mistral/zai-glm-5-3-flash"
        )
        self.assertEqual(
            [event["sequence_number"] for event in events], list(range(len(events)))
        )
        for event in events:
            self.assertIn("event: " + event["type"] + "\n", reply.text)
        if events[-1]["response"]["output"][0]["type"] == "function_call":
            self.assertEqual(
                sum(
                    event["type"] == "response.function_call_arguments.delta"
                    for event in events
                ),
                2,
            )
        else:
            self.assertIn("response.output_text.delta", reply.text)
            self.assertIn("response.output_text.done", reply.text)
        return dict(events[-1]["response"])

    def test_round_trip_stream_and_json_meter_each_call(self) -> None:
        for stream in (False, True):
            with self.subTest(stream=stream):
                first = self.result(
                    self.post(self.payload | {"stream": stream}), stream
                )
                call = first["output"][0]
                self.assertEqual(call["type"], "function_call")
                self.assertEqual(call["call_id"], "call_fixture")
                self.assertEqual(json.loads(call["arguments"]), {"value": 7})
                history = [
                    {"role": "user", "content": "Save seven"},
                    *first["output"],
                    {
                        "type": "reasoning",
                        "id": "rs_fixture",
                        "summary": [],
                        "encrypted_content": "opaque",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": "Saved seven",
                    },
                ]
                second = self.result(
                    self.post(self.payload | {"input": history, "stream": stream}),
                    stream,
                )
                self.assertEqual(
                    second["output"][0]["content"][0]["text"], "Stored seven."
                )
                self.assertEqual(second["usage"]["input_tokens"], 10)
                self.assertEqual(second["usage"]["output_tokens"], 2)
                self.assertFalse(second["store"])
                self.assertEqual(
                    self.inputs[-1]["messages"][-1]["tool_call_id"], "call_fixture"
                )
        usage: dict[str, Any] = self.store.usage(self.row["id"])
        self.assertEqual(usage["requests"], 4)
        self.assertEqual(usage["unknown_micro_eur"], 0)
        self.assertGreater(usage["charged_micro_eur"], 0)
        self.assertEqual(
            Decimal(str(DailyLedger().summary()[0]["measured_eur"])),
            Decimal(usage["charged_micro_eur"]) / 1000000,
        )

    def test_model_aliases_default_to_code_and_refuse_raw_ids(self) -> None:
        for name, selected in [
            ("gpt-fictional-client", "atlas-code"),
            ("atlas-chat", "atlas-chat"),
            ("atlas-code-2026-10-30", "atlas-code"),
        ]:
            with self.subTest(model=name):
                result = self.result(self.post(self.payload | {"model": name}), False)
                self.assertEqual(result["model"], selected)
        reply = self.post(self.payload | {"model": "raw/internal-only"})
        self.assertEqual(reply.status_code, 400)

    def test_chat_completions_reports_public_usage_and_provider(self) -> None:
        reply = self.client.post(
            "/api/atlas/v1/chat/completions",
            headers=self.headers,
            json={
                "model": "gpt-fictional-client",
                "messages": [{"role": "user", "content": "Save seven"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "save_value",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["model"], "atlas-code")
        self.assertEqual(reply.json()["served_by"], "mistral/zai-glm-5-3-flash")
        self.assertEqual(
            reply.headers["x-atlas-served-by"], "mistral/zai-glm-5-3-flash"
        )

    def test_auth_scope_quota_expiry_revocation_before_transport(self) -> None:
        for stream in (False, True):
            body = self.payload | {"stream": stream}
            _, assistant = self.key(scopes=["assistant"])
            _, limited = self.key(daily_micro_eur=1)
            for headers, status in [
                ({}, 401),
                ({"Authorization": "Bearer invalid"}, 401),
                (assistant, 403),
                (limited, 429),
            ]:
                reply = self.post(body, headers)
                self.assertEqual(reply.status_code, status, reply.text)
                self.assertIsInstance(reply.json()["error"]["type"], str)
                self.assertIsInstance(reply.json()["error"]["message"], str)
            self.now[0] = float(self.row["expires_at"])
            self.assertEqual(self.post(body).status_code, 401)
            self.now[0] -= 1
        self.store.revoke(self.row["id"])
        self.assertEqual(self.post(self.payload).status_code, 401)
        self.assertEqual(self.inputs, [])

    def test_missing_public_route_price_fails_before_transport(self) -> None:
        policy = ServerlessPolicy(allow_unpriced=True)
        policy.prices.pop("zai-glm-5-3-flash")
        with patch.object(gateway, "ServerlessPolicy", return_value=policy):
            reply = self.post(self.payload)
            self.assertEqual(reply.status_code, 503)
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.store.usage(self.row["id"])["requests"], 0)

    def test_provider_failure_retains_reservation_json_and_stream(self) -> None:
        async def broken(plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
            yield b'data: {"choices":[{"index":0,"delta":{"content":"Partial"},"finish_reason":null}]}\n\n'
            raise OSError("fictional transport failure")

        with patch.object(gateway, "transport", broken):
            for stream in (False, True):
                reply = self.post(self.payload | {"stream": stream})
                if stream:
                    self.assertEqual(reply.status_code, 200)
                    self.assertIn("event: error", reply.text)
                    self.assertNotIn("response.completed", reply.text)
                else:
                    self.assertEqual(reply.status_code, 502)
                    self.assertEqual(reply.json()["error"]["type"], "provider_error")
        usage: dict[str, Any] = self.store.usage(self.row["id"])
        self.assertEqual(usage["requests"], 2)
        self.assertGreater(usage["unknown_micro_eur"], 0)
        self.assertEqual(usage["charged_micro_eur"], usage["unknown_micro_eur"])

    def test_user_budget_and_request_cap(self) -> None:
        with patch.dict(os.environ, {"ATLAS_USER_DAILY_EUR": "0.000001"}):
            reply = self.post(self.payload)
            self.assertEqual(reply.status_code, 429, reply.text)
            self.assertEqual(reply.json()["error"]["code"], "budget_limit")
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.store.usage(self.row["id"])["requests"], 0)
        row, headers = self.key(daily_requests=1)
        self.result(self.post(self.payload, headers), False)
        reply = self.post(self.payload, headers)
        self.assertEqual(reply.status_code, 429)
        self.assertEqual(reply.json()["error"]["requests"], 1)
        self.assertEqual(self.store.usage(row["id"])["requests"], 1)
        self.assertEqual(len(self.inputs), 1)

    def test_malformed_and_project_input_never_reach_transport(self) -> None:
        inputs: list[Any] = [
            [],
            None,
            {"model": "atlas-code"},
            self.payload | {"model": []},
            self.payload | {"project_id": "other"},
            self.payload | {"input": "x" * 1048576},
        ]
        for value in inputs:
            reply = self.post(value)
            self.assertIn(reply.status_code, (400, 403, 413), reply.text)
            self.assertIn("error", reply.json())
        self.assertEqual(self.inputs, [])
