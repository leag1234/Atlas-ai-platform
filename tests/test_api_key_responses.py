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

    def test_three_mib_stream_is_accepted_and_final_event_names_provider(self) -> None:
        payload = self.payload | {
            "stream": True,
            "input": "fictional-" * 262144,
        }
        # Production sets the per-call cap to 5 EUR; a 3 MiB turn reserves above the 0.05 EUR default.
        with patch.dict(os.environ, {"ATLAS_RAW_CALL_MAX_MICRO_EUR": "5000000"}):
            reply = self.post(payload)
        self.assertEqual(reply.status_code, 200, reply.text)
        events = [
            json.loads(line[6:])
            for line in reply.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(events[-1]["type"], "response.completed")
        self.assertEqual(events[-1]["served_by"], "mistral/zai-glm-5-3-flash")
        self.assertEqual(
            events[-1]["response"]["served_by"], "mistral/zai-glm-5-3-flash"
        )

    def test_body_larger_than_every_context_is_refused_as_context(self) -> None:
        payload = self.payload | {"input": "fictional-" * 524288}
        reply = self.post(payload)
        self.assertEqual(reply.status_code, 400, reply.text)
        self.assertEqual(reply.json()["error"]["type"], "context_exceeded")
        self.assertEqual(reply.json()["error"]["measured"], 1310956)
        self.assertEqual(reply.json()["error"]["limit"], 1048576)

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
            self.payload | {"input": "x" * 8388608},
        ]
        for value in inputs:
            reply = self.post(value)
            self.assertIn(reply.status_code, (400, 403, 413), reply.text)
            self.assertIn("error", reply.json())
        self.assertEqual(self.inputs, [])

    def test_codex_160_tools_json_and_streaming(self) -> None:
        """Recorded-shaped, fictional Codex 0.160 public Responses bodies."""

        async def codex_transport(plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
            body = json.loads(plan.body)
            self.inputs.append(body)
            deltas: list[dict[str, Any]]
            reason: str
            if body["messages"][-1]["role"] == "tool":
                deltas, reason = [{"content": "tool result received"}], "stop"
            else:
                function = body["tools"][0]["function"]
                name = function["name"]
                arguments = (
                    '{"input":"*** Begin Patch\\n*** End Patch"}'
                    if name == "apply_patch"
                    else '{"command":["pwd"],"workdir":"/tmp","timeout_ms":1000,"env":{"LANG":"C"}}'
                    if name == "local_shell"
                    else '{"task":"delegate"}'
                )
                deltas = [
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_codex",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": arguments[:12],
                                },
                            }
                        ]
                    },
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": arguments[12:]}}
                        ]
                    },
                ]
                reason = "tool_calls"
            for delta in deltas:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "choices": [
                                {"index": 0, "delta": delta, "finish_reason": None}
                            ]
                        }
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

        bodies = {
            "custom": {
                "type": "custom",
                "name": "apply_patch",
                "description": "Apply a patch.",
                "format": {"type": "grammar", "syntax": "lark", "definition": "patch"},
            },
            "local_shell": {"type": "local_shell"},
            "namespace": {
                "type": "namespace",
                "name": "multi_agent_v1",
                "tools": [
                    {
                        "type": "function",
                        "name": "delegate",
                        "parameters": {"type": "object"},
                    }
                ],
            },
        }

        def codex_result(reply: Any, stream: bool) -> dict[str, Any]:
            self.assertEqual(reply.status_code, 200, reply.text)
            if not stream:
                return dict(reply.json())
            frames = [
                json.loads(line[6:])
                for line in reply.text.splitlines()
                if line.startswith("data: ")
            ]
            self.assertEqual(frames[-1]["type"], "response.completed")
            return dict(frames[-1]["response"])

        with patch.object(gateway, "transport", codex_transport):
            for label, tool in bodies.items():
                for stream in (False, True):
                    with self.subTest(label=label, stream=stream):
                        request = {
                            "model": "atlas-code",
                            "input": "fictional task",
                            "store": False,
                            "stream": stream,
                            "text": {"verbosity": "high"},
                            "reasoning": {"effort": "medium", "summary": "auto"},
                            "tools": [tool],
                            "codex_optional_field": {"fictional": True},
                        }
                        reply = self.post(request)
                        result = codex_result(reply, stream)
                        call = result["output"][0]
                        expected = {
                            "custom": "custom_tool_call",
                            "local_shell": "local_shell_call",
                            "namespace": "function_call",
                        }[label]
                        self.assertEqual(call["type"], expected)
                        if label == "custom":
                            self.assertEqual(
                                call["input"], "*** Begin Patch\n*** End Patch"
                            )
                            if stream:
                                self.assertIn(
                                    "response.custom_tool_call_input.done", reply.text
                                )
                        if label == "local_shell":
                            self.assertEqual(call["action"]["command"], ["pwd"])
                        if label == "namespace":
                            self.assertEqual(call["name"], "multi_agent_v1__delegate")
                        sent = self.inputs[-1]
                        self.assertNotIn("codex_optional_field", sent)
                        if label == "custom":
                            self.assertEqual(
                                sent["tools"][0]["function"]["parameters"]["required"],
                                ["input"],
                            )
                        if label == "local_shell":
                            self.assertEqual(
                                sent["tools"][0]["function"]["name"], "local_shell"
                            )
                        if label == "namespace":
                            self.assertEqual(
                                sent["tools"][0]["function"]["name"],
                                "multi_agent_v1__delegate",
                            )
            for label, output_type in (
                ("custom", "custom_tool_call_output"),
                ("local_shell", "local_shell_call_output"),
            ):
                tool = bodies[label]
                first = codex_result(
                    self.post(
                        {
                            "model": "atlas-code",
                            "input": "fictional",
                            "store": False,
                            "tools": [tool],
                        }
                    ),
                    False,
                )
                call = first["output"][0]
                reply = self.post(
                    {
                        "model": "atlas-code",
                        "input": [
                            *first["output"],
                            {
                                "type": output_type,
                                "call_id": call["call_id"],
                                "output": "fictional output",
                            },
                        ],
                        "store": False,
                        "tools": [tool],
                    }
                )
                self.assertEqual(
                    codex_result(reply, False)["output"][0]["content"][0]["text"],
                    "tool result received",
                )
