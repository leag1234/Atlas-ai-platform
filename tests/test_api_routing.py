"""Public usage routing is configuration-driven and has no provider I/O."""

import os
import unittest
from unittest.mock import patch

from api_routing import select_public_model, targets


class ApiRoutingTests(unittest.TestCase):
    def test_usage_effort_routes_and_aliases(self) -> None:
        for usage, effort, model in (
            ("atlas-code", "none", "zai-glm-5-3-flash"),
            ("atlas-code", "high", "zai-glm-5-3"),
            ("atlas-chat", "medium", "zai-glm-5-3"),
            ("atlas-chat-2026-10-30", "low", "zai-glm-5-3"),
        ):
            resolved, entries = targets(usage, effort)
            self.assertIn(resolved, ("atlas-code", "atlas-chat"))
            self.assertEqual(entries[0].model, model)
            self.assertEqual(entries[1].provider, "scaleway")

    def test_unknown_client_defaults_to_code_but_raw_is_refused(self) -> None:
        self.assertEqual(select_public_model("claude-fictional"), "atlas-code")
        self.assertEqual(select_public_model("gpt-fictional"), "atlas-code")
        with self.assertRaises(ValueError):
            select_public_model("raw/internal")


class RoutedBodyTests(unittest.TestCase):
    def test_routed_body_has_no_empty_tool_fields(self) -> None:
        import json as _json
        import dev_gateway
        from packages.dev_request import Request

        request = Request.model_validate(
            {
                "messages": [{"role": "user", "content": "Reply OK"}],
                "max_tokens": 20,
                "reasoning_effort": "low",
            }
        )
        with patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "test-only", "SCW_GENERATIVE_API_KEY": "test-only"},
        ):
            plan = dev_gateway.prepare_api(request, "atlas-code", "key-test")
        body = _json.loads(plan.body)
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.assertNotIn("response_format", body)
        self.assertTrue(all("tool_calls" not in m for m in body["messages"]))


class RoutedEffortTests(unittest.TestCase):
    def test_routed_effort_uses_values_the_provider_accepts(self) -> None:
        import json as _json
        import dev_gateway
        from packages.dev_request import Request

        expected = {"none": "low", "low": "low", "medium": "high", "high": "high"}
        with patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "test-only", "SCW_GENERATIVE_API_KEY": "test-only"},
        ):
            for level, sent in expected.items():
                request = Request.model_validate(
                    {
                        "messages": [{"role": "user", "content": "Reply OK"}],
                        "reasoning_effort": level,
                    }
                )
                plan = dev_gateway.prepare_api(request, "atlas-code", "key-test")
                self.assertEqual(
                    _json.loads(plan.body)["reasoning_effort"], sent, level
                )
                assert plan.fallback is not None
                self.assertEqual(
                    _json.loads(plan.fallback.body)["reasoning_effort"], level
                )


class RoutedTypedContentTests(unittest.TestCase):
    def test_routed_stream_keeps_text_and_drops_reasoning_parts(self) -> None:
        import asyncio
        import json as _json
        from dataclasses import replace
        import dev_gateway
        from packages.dev_request import Request

        frames = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": [
                                {
                                    "type": "thinking",
                                    "thinking": [
                                        {"type": "text", "text": "fictional reasoning"}
                                    ],
                                    "closed": True,
                                }
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {"index": 0, "delta": {"content": "391"}, "finish_reason": "stop"}
                ]
            },
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 5,
                    "total_tokens": 17,
                },
            },
        ]
        stream = (
            b"".join(b"data: " + _json.dumps(f).encode() + b"\n\n" for f in frames)
            + b"data: [DONE]\n\n"
        )

        async def fake_transport(plan: object):  # type: ignore[no-untyped-def]
            yield stream

        request = Request.model_validate(
            {
                "messages": [{"role": "user", "content": "What is 17 times 23?"}],
                "reasoning_effort": "low",
            }
        )
        with patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "test-only", "SCW_GENERATIVE_API_KEY": "test-only"},
        ):
            plan = replace(
                dev_gateway.prepare_api(request, "atlas-code", "key-test"),
                fallback=None,
            )

        async def collect() -> list[dict[str, object]]:
            with patch.object(dev_gateway, "transport", fake_transport):
                return [e async for e in dev_gateway.attempt_events(plan)]

        events = asyncio.run(collect())
        texts: list[object] = []
        for event in events:
            choices = event.get("choices")
            if isinstance(choices, list):
                texts.extend(c["delta"].get("content") for c in choices)
        self.assertIn("391", texts)
        self.assertNotIn("fictional reasoning", _json.dumps(events))


class ReusedCallIndexTests(unittest.TestCase):
    def test_new_call_id_on_reused_index_opens_a_new_call(self) -> None:
        from services.orchestrator.dev_chat import ChatInput, Output

        request = ChatInput.model_validate(
            {
                "model": "atlas-code",
                "messages": [{"role": "user", "content": "Fictional task."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "run",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            }
        )
        output = Output("test", request)
        for call_id in ("call_a", "call_b"):
            output.feed(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": call_id,
                                        "type": "function",
                                        "function": {"name": "run", "arguments": "{}"},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            )
        output.feed(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
        )
        self.assertEqual([c["id"] for c in output.calls.values()], ["call_a", "call_b"])


class EmptyContinuationFieldTests(unittest.TestCase):
    def test_empty_name_in_continuation_chunk_is_not_a_new_identity(self) -> None:
        from services.orchestrator.dev_chat import ChatInput, Output

        request = ChatInput.model_validate(
            {
                "model": "atlas-code",
                "messages": [{"role": "user", "content": "Fictional task."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "run",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            }
        )
        output = Output("test", request)
        output.feed(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_a",
                                    "type": "function",
                                    "function": {
                                        "name": "run",
                                        "arguments": '{"command": "',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        )
        output.feed(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "type": "function",
                                    "function": {"name": "", "arguments": 'ls"}'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        )
        output.feed(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
        )
        self.assertEqual(len(output.calls), 1)
        self.assertEqual(output.calls[0]["function"]["arguments"], '{"command": "ls"}')
