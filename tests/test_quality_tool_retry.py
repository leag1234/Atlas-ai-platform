"""Task D: tool-name validation must not change ordinary retry decisions."""

from collections.abc import AsyncGenerator
import asyncio
import os
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
from packages.limits import (
    LimitError,
    MalformedToolCall,
    ProviderFailure,
    ProviderLimitError,
)
from quality import stream_quality
from serverless_support import environment
from test_quality_gateway import request, result


class ToolRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_name_cannot_retry_text_already_emitted_without_tools(
        self,
    ) -> None:
        calls = 0
        events = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            yield {"delta": {"content": "The explanation begins."}}
            raise MalformedToolCall(128)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            self.assertRaises(MalformedToolCall) as caught,
        ):
            async for event in stream_quality(AgentProvider(), request()):
                events.append(event)
        self.assertEqual(calls, 1)
        self.assertEqual(caught.exception.retries, 0)
        self.assertEqual(events, [{"delta": {"content": "The explanation begins."}}])

    async def test_tool_enabled_partial_answer_failure_does_not_retry(self) -> None:
        calls = 0
        visible: list[str] = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            if calls == 1:
                yield {"delta": {"content": "The sky appears blue because "}}
                yield {"delta": {"content": "air scatters sunlight."}}
                raise RuntimeError("provider_error")
            yield result("An unwanted second explanation.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            self.assertRaises(ProviderFailure) as caught,
        ):
            async for event in stream_quality(
                AgentProvider(),
                request(
                    tools=[{"type": "function", "function": {"name": "calculator"}}]
                ),
            ):
                delta = event.get("delta")
                if isinstance(delta, dict):
                    visible.append(str(delta.get("content", "")))
        self.assertEqual(calls, 1)
        self.assertEqual(caught.exception.retries, 0)
        self.assertEqual(caught.exception.code, "provider_error")
        self.assertEqual(
            "".join(visible),
            "The sky appears blue because air scatters sunlight.",
        )

    async def test_tool_enabled_length_finish_keeps_m21_single_attempt(self) -> None:
        calls = 0
        visible = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            if calls == 1:
                yield {"delta": {"content": "The"}}
                yield result("The", 1, empty=True)
            else:
                yield result("An unwanted second explanation.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            self.assertRaises(ProviderFailure) as caught,
        ):
            async for event in stream_quality(
                AgentProvider(),
                request(
                    max_tokens=1,
                    tools=[{"type": "function", "function": {"name": "calculator"}}],
                ),
            ):
                if "delta" in event:
                    visible.append(event)
        self.assertEqual(calls, 1)
        self.assertEqual(caught.exception.code, "incomplete_provider_answer")
        self.assertEqual(caught.exception.retries, 0)
        self.assertEqual(visible, [{"delta": {"content": "The"}}])

    async def test_non_name_limits_and_timeouts_preserve_partial_text(self) -> None:
        for failure in (
            TimeoutError("provider_timeout"),
            LimitError("provider_response_limit", 128, 80, "characters"),
            LimitError("tool_limit", 16001, 16000, "characters"),
            LimitError("output_budget", 6001, 6000, "tokens"),
            ProviderLimitError("cost_budget", 100001, 100000, "microEUR"),
        ):
            calls = 0
            events = []

            async def upstream(
                req: AgentRequest, model: str, timeout: float
            ) -> AsyncGenerator[dict[str, object], None]:
                nonlocal calls
                calls += 1
                yield {"delta": {"content": "The explanation begins."}}
                raise failure

            with (
                self.subTest(failure=str(failure)),
                patch.dict(os.environ, environment()),
                patch("stream_transport.attempt", upstream),
                self.assertRaises(type(failure)) as caught,
            ):
                async for event in stream_quality(
                    AgentProvider(),
                    request(
                        tools=[{"type": "function", "function": {"name": "calculator"}}]
                    ),
                ):
                    events.append(event)
            self.assertIs(caught.exception, failure)
            self.assertEqual(calls, 1)
            self.assertEqual(
                events, [{"delta": {"content": "The explanation begins."}}]
            )

    async def test_tool_enabled_pre_answer_failure_still_recovers(self) -> None:
        for prefix in (
            "",
            '{"untrusted_tool_history":',
            "I will search for evidence.",
        ):
            calls = 0
            events = []

            async def upstream(
                req: AgentRequest, model: str, timeout: float
            ) -> AsyncGenerator[dict[str, object], None]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    for char in prefix:
                        yield {"delta": {"content": char}}
                    raise RuntimeError("provider_error")
                yield {"delta": {"content": "Air scatters sunlight."}}
                yield result("Air scatters sunlight.", 20)

            with (
                self.subTest(prefix=prefix),
                patch.dict(os.environ, environment()),
                patch("stream_transport.attempt", upstream),
            ):
                events = [
                    event
                    async for event in stream_quality(
                        AgentProvider(),
                        request(
                            tools=[
                                {
                                    "type": "function",
                                    "function": {"name": "calculator"},
                                }
                            ]
                        ),
                    )
                ]
            self.assertEqual(calls, 2)
            self.assertEqual(
                [e for e in events if "delta" in e],
                [{"delta": {"content": "Air scatters sunlight."}}],
            )

    async def test_tool_enabled_invalid_arguments_after_text_do_not_retry(
        self,
    ) -> None:
        calls = 0
        events = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            yield {"delta": {"content": "The calculation uses these values."}}
            yield {
                "result": {
                    "text": "The calculation uses these values.",
                    "calls": [{"id": "call", "name": "calculator", "arguments": "{"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 20},
                    "finish_reason": "tool_calls",
                }
            }

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            self.assertRaisesRegex(RuntimeError, "invalid_tool_arguments"),
        ):
            async for event in stream_quality(
                AgentProvider(),
                request(
                    tools=[{"type": "function", "function": {"name": "calculator"}}]
                ),
            ):
                events.append(event)
        self.assertEqual(calls, 1)
        self.assertEqual(
            events, [{"delta": {"content": "The calculation uses these values."}}]
        )

    async def test_tool_enabled_answer_retains_original_deadline(self) -> None:
        calls = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            yield {"delta": {"content": "The explanation begins."}}
            await asyncio.sleep(1.7)
            yield result("The explanation begins.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            events = [
                event
                async for event in stream_quality(
                    AgentProvider(),
                    request(
                        timeout=2.0,
                        max_tokens=500,
                        tools=[
                            {"type": "function", "function": {"name": "calculator"}}
                        ],
                    ),
                )
            ]
        self.assertEqual(calls, 1)
        self.assertEqual(events[0], {"delta": {"content": "The explanation begins."}})
        self.assertIn("result", events[-1])
