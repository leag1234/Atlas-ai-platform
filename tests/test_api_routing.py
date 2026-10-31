"""Public usage routing is configuration-driven and has no provider I/O."""

import os
from collections.abc import AsyncIterator
import unittest
from unittest.mock import patch

from api_routing import select_public_model, targets
import dev_gateway


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

    def test_strict_mode_keeps_only_owned_targets_and_never_falls_back(self) -> None:
        with patch(
            "api_routing.config",
            return_value={
                "usages": {
                    "atlas-code": {
                        "none": [
                            {"provider": "mistral", "model": "shared", "context": 1000},
                            {
                                "provider": "owned-vllm",
                                "model": "owned",
                                "context": 1000,
                                "privacy": "owned",
                            },
                        ]
                    }
                }
            },
        ):
            _, entries = targets("atlas-code", "none", strict=True)
        self.assertEqual([target.privacy for target in entries], ["owned"])
        self.assertEqual(entries[0].model, "owned")

    def test_strict_mode_without_owned_target_is_refused(self) -> None:
        with self.assertRaises(ValueError) as raised:
            targets("atlas-code", "none", strict=True)
        self.assertEqual(str(raised.exception), "strict_privacy_unavailable")

    def test_strict_mode_never_configures_a_shared_fallback(self) -> None:
        from packages.dev_request import Request

        request = Request.model_validate(
            {
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 10,
            }
        )
        with patch(
            "api_routing.config",
            return_value={
                "usages": {
                    "atlas-code": {
                        "none": [
                            {
                                "provider": "owned-vllm",
                                "model": "owned",
                                "context": 1000,
                                "privacy": "owned",
                            },
                            {
                                "provider": "mistral",
                                "model": "shared",
                                "context": 1000,
                            },
                        ]
                    }
                }
            },
        ):
            with patch("dev_gateway.ServerlessPolicy") as policy:
                policy.return_value.allow_unpriced = True
                policy.return_value.prices = {
                    "owned": {
                        "input_eur_per_mtok": 0,
                        "output_eur_per_mtok": 0,
                    }
                }
                plan = dev_gateway.prepare_api(
                    request, "atlas-code", "key-test", strict=True
                )
        self.assertEqual(plan.privacy, "owned")
        self.assertIsNone(plan.fallback)


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


class RoutedContextTests(unittest.TestCase):
    def test_large_fictional_body_fits_primary_and_skips_small_fallback(self) -> None:
        import json as _json
        import dev_gateway
        from packages.dev_request import Request

        request = Request.model_validate(
            {
                "messages": [{"role": "user", "content": "fictional-" * 262144}],
                "max_tokens": 32,
                "reasoning_effort": "low",
            }
        )
        with patch.dict(
            os.environ,
            {
                "MISTRAL_API_KEY": "test-only",
                "SCW_GENERATIVE_API_KEY": "test-only",
                "ATLAS_RAW_CALL_MAX_MICRO_EUR": "5000000",
            },
        ):
            plan = dev_gateway.prepare_api(request, "atlas-code", "key-test")
            self.assertLessEqual(plan.reserved, dev_gateway.call_cap())
        self.assertIsNone(plan.fallback)
        self.assertEqual(_json.loads(plan.body)["model"], "zai-glm-5-3")

    def test_body_larger_than_every_context_is_refused_as_context(self) -> None:
        import dev_gateway
        from packages.dev_request import Request
        from packages.limits import LimitError

        request = Request.model_validate(
            {
                "messages": [{"role": "user", "content": "fictional-" * 524288}],
                "max_tokens": 32,
                "reasoning_effort": "low",
            }
        )
        with patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "test-only", "SCW_GENERATIVE_API_KEY": "test-only"},
        ):
            with self.assertRaises(LimitError) as raised:
                dev_gateway.prepare_api(request, "atlas-code", "key-test")
        self.assertEqual(raised.exception.code, "context_exceeded")
        self.assertEqual(raised.exception.limit, 1048576)


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


class RoutedReasoningOnlyRetryTests(unittest.TestCase):
    def _plan(self) -> "dev_gateway.Plan":
        import dev_gateway
        from packages.dev_request import Request

        request = Request.model_validate(
            {
                "messages": [{"role": "user", "content": "Reply OK"}],
                "reasoning_effort": "low",
            }
        )
        with patch.dict(
            os.environ,
            {"MISTRAL_API_KEY": "test-only", "SCW_GENERATIVE_API_KEY": "test-only"},
        ):
            return dev_gateway.prepare_api(request, "atlas-code", "key-test")

    @staticmethod
    def _stream(frames: list[dict[str, object]]) -> bytes:
        import json as _json

        return (
            b"".join(
                b"data: " + _json.dumps(frame).encode() + b"\n\n" for frame in frames
            )
            + b"data: [DONE]\n\n"
        )

    @staticmethod
    def _reasoning_only() -> bytes:
        return RoutedReasoningOnlyRetryTests._stream(
            [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"reasoning_content": "fictional reasoning"},
                            "finish_reason": "stop",
                        }
                    ]
                },
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 4,
                        "total_tokens": 14,
                    },
                },
            ]
        )

    @staticmethod
    def _answer() -> bytes:
        return RoutedReasoningOnlyRetryTests._stream(
            [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "OK"},
                            "finish_reason": "stop",
                        }
                    ]
                },
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 2,
                        "total_tokens": 22,
                    },
                },
            ]
        )

    def test_reasoning_only_retries_same_target_and_sums_usage(self) -> None:
        import asyncio

        plan = self._plan()
        streams = iter([self._reasoning_only(), self._answer()])

        async def fake_transport(requested_plan: object) -> AsyncIterator[bytes]:
            yield next(streams)

        async def collect() -> list[dict[str, object]]:
            with patch.object(dev_gateway, "transport", fake_transport):
                return [event async for event in dev_gateway.events(plan)]

        events = asyncio.run(collect())
        self.assertEqual(
            [event for event in events if event.get("choices")],
            [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "OK"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ],
        )
        self.assertEqual(
            events[-1]["usage"],
            {"prompt_tokens": 30, "completion_tokens": 6, "total_tokens": 36},
        )
        self.assertEqual(
            events[-1]["cost_micro_eur"],
            plan.charge(10, 4) + plan.charge(20, 2),
        )

    def test_two_reasoning_only_attempts_use_fallback_and_sums_usage(self) -> None:
        import asyncio

        plan = self._plan()
        assert plan.fallback is not None
        streams = iter([self._reasoning_only(), self._reasoning_only(), self._answer()])

        async def fake_transport(requested_plan: object) -> AsyncIterator[bytes]:
            yield next(streams)

        async def collect() -> list[dict[str, object]]:
            with patch.object(dev_gateway, "transport", fake_transport):
                return [event async for event in dev_gateway.events(plan)]

        events = asyncio.run(collect())
        self.assertEqual(events[-1]["served_by"], "scaleway/deepseek-v4-flash-0731")
        self.assertEqual(
            events[-1]["usage"],
            {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
        )
        self.assertEqual(
            events[-1]["cost_micro_eur"],
            2 * plan.charge(10, 4) + plan.fallback.charge(20, 2),
        )

    def test_visible_answer_is_never_retried(self) -> None:
        import asyncio

        plan = self._plan()
        calls = 0

        async def fake_transport(requested_plan: object) -> AsyncIterator[bytes]:
            nonlocal calls
            calls += 1
            yield self._answer()

        async def collect() -> list[dict[str, object]]:
            with patch.object(dev_gateway, "transport", fake_transport):
                return [event async for event in dev_gateway.events(plan)]

        events = asyncio.run(collect())
        self.assertEqual(calls, 1)
        terminal = events[-1]
        assert isinstance(terminal, dict)
        usage = terminal["usage"]
        assert isinstance(usage, dict)
        self.assertEqual(usage["completion_tokens"], 2)

    def test_transient_error_after_visible_frames_never_appends_fallback(self) -> None:
        import asyncio
        import json as _json

        from provider_retry import TransientProviderError

        plan = self._plan()
        calls = 0

        async def fake_transport(requested_plan: object) -> AsyncIterator[bytes]:
            nonlocal calls
            calls += 1
            frame = {"choices": [{"index": 0, "delta": {"content": "Part"}}]}
            yield b"data: " + _json.dumps(frame).encode() + b"\n\n"
            raise TransientProviderError("fictional reset")

        received: list[dict[str, object]] = []

        async def collect() -> None:
            with patch.object(dev_gateway, "transport", fake_transport):
                async for event in dev_gateway.events(plan):
                    received.append(event)

        with self.assertRaises(TransientProviderError):
            asyncio.run(collect())
        self.assertEqual(calls, 1)
        self.assertEqual(len(received), 1)

    def test_streaming_starts_at_first_visible_frame_and_stays_live(self) -> None:
        import asyncio
        import json as _json

        plan = self._plan()
        pulls: list[str] = []
        deliveries: list[str] = []

        async def fake_transport(requested_plan: object) -> AsyncIterator[bytes]:
            frames = [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"reasoning_content": "fictional reasoning"},
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "OK"},
                            "finish_reason": "stop",
                        }
                    ]
                },
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 2,
                        "total_tokens": 22,
                    },
                },
            ]
            for frame, label in zip(frames, ("reasoning", "OK", "usage")):
                pulls.append(label)
                yield b"data: " + _json.dumps(frame).encode() + b"\n\n"
            yield b"data: [DONE]\n\n"

        async def collect() -> None:
            with patch.object(dev_gateway, "transport", fake_transport):
                async for event in dev_gateway.events(plan):
                    if event.get("choices"):
                        delta = event["choices"][0]["delta"]
                        deliveries.append(
                            delta.get("content", delta.get("reasoning_content"))
                        )
                    else:
                        deliveries.append("terminal")

        asyncio.run(collect())
        self.assertEqual(pulls, ["reasoning", "OK", "usage"])
        self.assertEqual(deliveries, ["fictional reasoning", "OK", "terminal"])
        self.assertLess(
            deliveries.index("OK"), pulls.index("usage"), "terminal frame was buffered"
        )

    def test_retry_terminal_is_settled_before_consumer_receives_it(self) -> None:
        import asyncio

        plan = self._plan()
        streams = iter([self._reasoning_only(), self._answer()])
        terminal_costs: list[object] = []

        async def fake_transport(requested_plan: object) -> AsyncIterator[bytes]:
            yield next(streams)

        async def collect() -> None:
            with patch.object(dev_gateway, "transport", fake_transport):
                async for event in dev_gateway.events(plan):
                    if not event.get("choices"):
                        terminal_costs.append(event["cost_micro_eur"])

        asyncio.run(collect())
        self.assertEqual(terminal_costs, [plan.charge(10, 4) + plan.charge(20, 2)])


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
