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
                    # As on atlas.eu.com: Codex turns reserve up to 16384 output tokens.
                    "ATLAS_RAW_CALL_MAX_MICRO_EUR": "5000000",
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
        from packages.profiles import PROFILES

        for name in ("atlas", *PROFILES):
            with self.subTest(assistant=name):
                reply = self.post(self.payload | {"model": name})
                self.assertEqual(reply.status_code, 400)
                self.assertIn("/chat/completions", reply.text)

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
            _, limited = self.key(
                daily_requests=None,
                daily_micro_eur=None,
                weekly_micro_eur=1,
                own_budget=True,
            )
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
                if headers is limited:
                    self.assertEqual(reply.json()["error"]["code"], "quota_exceeded")
                    self.assertEqual(reply.json()["error"]["weekly_micro_eur"], 1)
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
                    # Reported 2026-10-31: Codex needs a terminal event after an error.
                    self.assertTrue(
                        reply.text.rstrip()
                        .split("\n\n")[-1]
                        .startswith("event: response.failed")
                    )
                else:
                    self.assertEqual(reply.status_code, 502)
                    self.assertEqual(reply.json()["error"]["type"], "provider_error")
        usage: dict[str, Any] = self.store.usage(self.row["id"])
        self.assertEqual(usage["requests"], 2)
        self.assertGreater(usage["unknown_micro_eur"], 0)
        self.assertEqual(usage["charged_micro_eur"], usage["unknown_micro_eur"])

    def test_stream_ending_without_a_final_frame_ends_with_response_failed(
        self,
    ) -> None:
        async def truncated(plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
            yield b'data: {"choices":[{"index":0,"delta":{"content":"Partial"},"finish_reason":null}]}\n\n'

        with patch.object(gateway, "transport", truncated):
            reply = self.post(self.payload | {"stream": True})
        self.assertEqual(reply.status_code, 200)
        self.assertNotIn("response.completed", reply.text)
        self.assertTrue(
            reply.text.rstrip().split("\n\n")[-1].startswith("event: response.failed")
        )

    def test_provider_credit_refusal_falls_back_then_reports_quota(self) -> None:
        from provider_retry import TransientProviderError

        models: list[str] = []

        async def primary_out_of_credit(
            plan: gateway.Plan,
        ) -> AsyncGenerator[bytes, None]:
            models.append(plan.model)
            if len(models) == 1:
                raise TransientProviderError("provider_quota")
            async for piece in self.transport(plan):
                yield piece

        with patch.object(gateway, "transport", primary_out_of_credit):
            reply = self.post(self.payload)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(len(models), 2)
        self.assertNotEqual(models[0], models[1])

        async def every_target_out_of_credit(
            plan: gateway.Plan,
        ) -> AsyncGenerator[bytes, None]:
            raise TransientProviderError("provider_quota")
            yield b""

        with patch.object(gateway, "transport", every_target_out_of_credit):
            reply = self.post(self.payload)
        self.assertEqual(reply.status_code, 503, reply.text)
        self.assertEqual(reply.json()["error"]["type"], "provider_quota")

    def test_context_trim_header_stubs_old_tool_outputs(self) -> None:
        from services.orchestrator.context_trim import BLOCK, KEEP

        items: list[dict[str, Any]] = [{"role": "user", "content": "Fix the parser."}]
        for index in range(KEEP + BLOCK):
            items += [
                {
                    "type": "function_call",
                    "call_id": f"c{index}",
                    "name": "save_value",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": f"c{index}",
                    "output": f"fictional output {index}",
                },
            ]
        payload = self.payload | {"input": items}
        for header, stubs in (("on", BLOCK), (None, 0)):
            headers = dict(self.headers)
            if header:
                headers["X-Atlas-Context-Trim"] = header
            reply = self.post(payload, headers)
            self.assertEqual(reply.status_code, 200, reply.text)
            tools = [m for m in self.inputs[-1]["messages"] if m["role"] == "tool"]
            self.assertEqual(len(tools), KEEP + BLOCK)
            self.assertEqual(
                sum("Atlas removed" in str(m["content"]) for m in tools), stubs
            )

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

    def test_key_route_gives_codex_turns_16384_output_tokens(self) -> None:
        # Reported 2026-10-31: a Codex turn writing a long file stopped at 4096 tokens.
        from services.orchestrator.api_key_responses import KEY_DEFAULT_OUTPUT_TOKENS

        self.assertEqual(KEY_DEFAULT_OUTPUT_TOKENS, 16384)
        reply = self.post(self.payload)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(self.inputs[-1]["max_tokens"], 16384)

    def test_nothing_is_stored(self) -> None:
        refused = self.post(dict(self.payload, previous_response_id="resp_1"))
        self.assertEqual(refused.status_code, 400)
        self.assertIn("does not store", refused.text)
        stored = self.post(dict(self.payload, store=True))
        self.assertEqual(stored.status_code, 200, stored.text)
        self.assertIs(stored.json()["store"], False)
        for method, path in (
            ("GET", "/api/atlas/v1/responses/resp_1"),
            ("DELETE", "/api/atlas/v1/responses/resp_1"),
            ("POST", "/api/atlas/v1/responses/resp_1/cancel"),
            ("GET", "/api/atlas/v1/responses/resp_1/input_items"),
            ("POST", "/api/atlas/v1/conversations"),
            ("GET", "/api/atlas/v1/conversations/conv_1/items"),
            ("GET", "/api/atlas/v1/chat/completions"),
            ("GET", "/api/atlas/v1/chat/completions/chatcmpl-1"),
        ):
            with self.subTest(method=method, path=path):
                reply = self.client.request(method, path, headers=self.headers)
                self.assertEqual(reply.status_code, 404, reply.text)
                self.assertEqual(reply.json()["error"]["code"], "storage_not_supported")

    def test_hosted_web_search(self) -> None:
        """web_search runs on Atlas between two model turns; the client sees a web_search_call item."""

        async def transport(plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
            body = json.loads(plan.body)
            self.inputs.append(body)
            if body["messages"][-1]["role"] == "tool":
                delta: dict[str, Any] = {
                    "content": "Paris has 2.1 million inhabitants, see https://example.invalid/paris"
                }
                reason = "stop"
            else:
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_search",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query":"Paris population"}',
                            },
                        }
                    ]
                }
                reason = "tool_calls"
            for frame in (
                {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3}},
            ):
                yield ("data: " + json.dumps(frame) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

        searched: list[str] = []

        async def fake_search(
            query: str, timeout: float, key_id: str
        ) -> dict[str, Any]:
            searched.append(query)
            return {
                "results": [
                    {
                        "title": "Paris",
                        "url": "https://example.invalid/paris",
                        "snippet": "2.1 million",
                    }
                ]
            }

        body = {
            "model": "atlas-code",
            "input": "How many people live in Paris?",
            "store": False,
            "tools": [{"type": "web_search"}],
        }
        with (
            patch.object(gateway, "transport", transport),
            patch("services.orchestrator.hosted_tools.web_search", fake_search),
        ):
            for stream in (False, True):
                with self.subTest(stream=stream):
                    reply = self.post(dict(body, stream=stream))
                    self.assertEqual(reply.status_code, 200, reply.text)
                    if stream:
                        frames = [
                            json.loads(line[6:])
                            for line in reply.text.splitlines()
                            if line.startswith("data: ")
                        ]
                        self.assertIn(
                            "response.web_search_call.completed",
                            [f["type"] for f in frames],
                        )
                        result = frames[-1]["response"]
                    else:
                        result = reply.json()
                    self.assertEqual(
                        [o["type"] for o in result["output"]],
                        ["web_search_call", "message"],
                    )
                    self.assertEqual(
                        result["output"][0]["action"]["query"], "Paris population"
                    )
                    self.assertIn(
                        "2.1 million", result["output"][1]["content"][0]["text"]
                    )
                    part = result["output"][1]["content"][0]
                    citation = part["annotations"][0]
                    self.assertEqual(citation["type"], "url_citation")
                    self.assertEqual(
                        part["text"][citation["start_index"] : citation["end_index"]],
                        "https://example.invalid/paris",
                    )
                    self.assertEqual(result["usage"]["input_tokens"], 20)
                    sent = self.inputs[-1]
                    self.assertEqual(sent["tools"][0]["function"]["name"], "web_search")
                    self.assertEqual(sent["messages"][-1]["role"], "tool")
                    self.assertIn(
                        "example.invalid/paris", sent["messages"][-1]["content"]
                    )
        self.assertEqual(searched, ["Paris population", "Paris population"])

        strict = self.post(
            dict(body), headers=dict(self.headers, **{"X-Atlas-Privacy": "strict"})
        )
        self.assertEqual(strict.status_code, 400, strict.text)
        self.assertIn("strict", strict.text)

    def test_hosted_web_search_on_chat_completions(self) -> None:
        """web_search_options runs the same hosted search; the answer cites retrieved urls."""

        async def transport(plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
            body = json.loads(plan.body)
            self.inputs.append(body)
            if body["messages"][-1]["role"] == "tool":
                delta: dict[str, Any] = {
                    "content": "About 2.1 million, see https://example.invalid/paris"
                }
                reason = "stop"
            else:
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_search",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query":"Paris population"}',
                            },
                        }
                    ]
                }
                reason = "tool_calls"
            for frame in (
                {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3}},
            ):
                yield ("data: " + json.dumps(frame) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

        async def fake_search(
            query: str, timeout: float, key_id: str
        ) -> dict[str, Any]:
            return {
                "results": [
                    {
                        "title": "Paris",
                        "url": "https://example.invalid/paris",
                        "snippet": "2.1 million",
                    }
                ]
            }

        body = {
            "model": "atlas-code",
            "messages": [{"role": "user", "content": "How many people live in Paris?"}],
            "web_search_options": {},
        }
        url = "/api/atlas/v1/chat/completions"
        with (
            patch.object(gateway, "transport", transport),
            patch("services.orchestrator.hosted_tools.web_search", fake_search),
        ):
            reply = self.client.post(url, headers=self.headers, json=body)
            self.assertEqual(reply.status_code, 200, reply.text)
            message = reply.json()["choices"][0]["message"]
            self.assertIn("2.1 million", message["content"])
            citation = message["annotations"][0]["url_citation"]
            self.assertEqual(citation["url"], "https://example.invalid/paris")
            self.assertEqual(
                message["content"][citation["start_index"] : citation["end_index"]],
                "https://example.invalid/paris",
            )
            self.assertEqual(reply.json()["usage"]["prompt_tokens"], 20)
            streamed = self.client.post(
                url, headers=self.headers, json=dict(body, stream=True)
            )
            self.assertEqual(streamed.status_code, 200, streamed.text)
            self.assertNotIn("hosted", streamed.text)
            self.assertIn("2.1 million", streamed.text)
        strict = self.client.post(
            url, headers=dict(self.headers, **{"X-Atlas-Privacy": "strict"}), json=body
        )
        self.assertEqual(strict.status_code, 400, strict.text)

    def test_hosted_web_search_on_messages(self) -> None:
        """The Anthropic web_search server tool: server_tool_use and web_search_tool_result blocks."""

        async def transport(plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
            body = json.loads(plan.body)
            self.inputs.append(body)
            if body["messages"][-1]["role"] == "tool":
                delta: dict[str, Any] = {"content": "About 2.1 million."}
                reason = "stop"
            else:
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_search",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query":"Paris population"}',
                            },
                        }
                    ]
                }
                reason = "tool_calls"
            for frame in (
                {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3}},
            ):
                yield ("data: " + json.dumps(frame) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

        async def fake_search(
            query: str, timeout: float, key_id: str
        ) -> dict[str, Any]:
            return {
                "results": [
                    {
                        "title": "Paris",
                        "url": "https://example.invalid/paris",
                        "snippet": "2.1 million",
                    }
                ]
            }

        headers = dict(self.headers, **{"anthropic-version": "2023-06-01"})
        body: dict[str, Any] = {
            "model": "atlas-code",
            "max_tokens": 200,
            "tools": [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
            ],
            "messages": [{"role": "user", "content": "How many people live in Paris?"}],
        }
        url = "/api/atlas/v1/messages"
        with (
            patch.object(gateway, "transport", transport),
            patch("services.orchestrator.hosted_tools.web_search", fake_search),
        ):
            reply = self.client.post(url, headers=headers, json=body)
            self.assertEqual(reply.status_code, 200, reply.text)
            result = reply.json()
            self.assertEqual(
                [b["type"] for b in result["content"]],
                ["server_tool_use", "web_search_tool_result", "text"],
            )
            self.assertEqual(
                result["content"][0]["input"], {"query": "Paris population"}
            )
            self.assertEqual(
                result["content"][1]["tool_use_id"], result["content"][0]["id"]
            )
            self.assertEqual(
                result["content"][1]["content"][0]["url"],
                "https://example.invalid/paris",
            )
            self.assertEqual(
                result["usage"]["server_tool_use"], {"web_search_requests": 1}
            )
            self.assertEqual(result["stop_reason"], "end_turn")

            streamed = self.client.post(
                url, headers=headers, json=dict(body, stream=True)
            )
            self.assertEqual(streamed.status_code, 200, streamed.text)
            starts = [
                json.loads(line[6:])
                for line in streamed.text.splitlines()
                if line.startswith("data: ") and '"content_block_start"' in line
            ]
            self.assertEqual([e["index"] for e in starts], [0, 1, 2])
            self.assertEqual(
                [e["content_block"]["type"] for e in starts],
                ["server_tool_use", "web_search_tool_result", "text"],
            )

            # The blocks come back in the next turn's history.
            follow = self.client.post(
                url,
                headers=headers,
                json=dict(
                    body,
                    messages=[
                        *body["messages"],
                        {"role": "assistant", "content": result["content"]},
                        {"role": "user", "content": "And Lyon?"},
                    ],
                ),
            )
            self.assertEqual(follow.status_code, 200, follow.text)

    def test_client_shell_and_apply_patch_tools(self) -> None:
        """The client-executed shell and apply_patch tools of the Responses API."""
        arguments = {
            "shell": '{"commands":["pwd","ls"],"timeout_ms":1000}',
            "apply_patch": '{"operation":{"type":"create_file","path":"a.txt","diff":"*** Begin Patch\\n*** Add File: a.txt\\n+hello\\n*** End Patch"}}',
        }

        async def transport(plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
            body = json.loads(plan.body)
            self.inputs.append(body)
            if body["messages"][-1]["role"] == "tool":
                delta: dict[str, Any] = {"content": "done"}
                reason = "stop"
            else:
                name = body["tools"][0]["function"]["name"]
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_client",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments[name]},
                        }
                    ]
                }
                reason = "tool_calls"
            for frame in (
                {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
            ):
                yield ("data: " + json.dumps(frame) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

        with patch.object(gateway, "transport", transport):
            for kind, output_item in (
                (
                    "shell",
                    {
                        "output": [
                            {
                                "stdout": "/tmp",
                                "stderr": "",
                                "outcome": {"type": "exit", "exit_code": 0},
                            }
                        ]
                    },
                ),
                ("apply_patch", {"status": "completed", "output": "created a.txt"}),
            ):
                with self.subTest(kind=kind):
                    reply = self.post(
                        {
                            "model": "atlas-code",
                            "input": "fictional task",
                            "store": False,
                            "tools": [{"type": kind}],
                        }
                    )
                    self.assertEqual(reply.status_code, 200, reply.text)
                    call = reply.json()["output"][0]
                    self.assertEqual(call["type"], kind + "_call")
                    self.assertEqual(
                        self.inputs[-1]["tools"][0]["function"]["name"], kind
                    )
                    if kind == "shell":
                        self.assertEqual(call["action"]["commands"], ["pwd", "ls"])
                        self.assertEqual(call["environment"], {"type": "local"})
                    else:
                        self.assertEqual(call["operation"]["path"], "a.txt")
                        # The freeform patch envelope is not part of an operation diff.
                        self.assertEqual(call["operation"]["diff"], "+hello")
                    follow = self.post(
                        {
                            "model": "atlas-code",
                            "store": False,
                            "tools": [{"type": kind}],
                            "input": [
                                {"role": "user", "content": "fictional task"},
                                call,
                                {
                                    "type": kind + "_call_output",
                                    "call_id": call["call_id"],
                                    **output_item,
                                },
                            ],
                        }
                    )
                    self.assertEqual(follow.status_code, 200, follow.text)
                    sent = self.inputs[-1]["messages"]
                    self.assertEqual(
                        sent[-2]["tool_calls"][0]["function"]["name"], kind
                    )
                    self.assertEqual(sent[-1]["role"], "tool")
                    self.assertEqual(
                        follow.json()["output"][0]["content"][0]["text"], "done"
                    )

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
