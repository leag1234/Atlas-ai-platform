"""Progression and late-error regression at the real gateway quality boundary."""

import asyncio
from collections.abc import AsyncGenerator
import os
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
from packages.limits import MalformedToolCall
from quality import stream_quality
from serverless_support import environment
from test_quality_gateway import request, result


class ProgressiveDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_enabled_answer_arrives_before_provider_finishes(self) -> None:
        release = asyncio.Event()
        finished = False
        phrase = "Readable answer text. "

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal finished
            for _ in range(20):
                yield {"delta": {"content": phrase}}
            await release.wait()
            finished = True
            yield result(phrase * 20, 100)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            async with asyncio.timeout(2):
                seen: list[object] = []
                async for event in stream_quality(
                    AgentProvider(),
                    request(
                        tools=[{"type": "function", "function": {"name": "calculator"}}]
                    ),
                ):
                    if "delta" in event:
                        if not seen:
                            self.assertFalse(finished)
                            release.set()
                        seen.append(event["delta"])
        self.assertEqual(len(seen), 20)
        self.assertTrue(finished)

    async def test_late_malformed_tool_does_not_restart_visible_answer(self) -> None:
        calls = 0
        events = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            for _ in range(20):
                yield {"delta": {"content": "Readable answer text. "}}
            raise MalformedToolCall(128)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            self.assertRaises(MalformedToolCall) as caught,
        ):
            async for event in stream_quality(
                AgentProvider(),
                request(
                    tools=[{"type": "function", "function": {"name": "calculator"}}]
                ),
            ):
                events.append(event)
        self.assertEqual(calls, 1)
        self.assertEqual(caught.exception.retries, 0)
        self.assertEqual(len(events), 20)
        self.assertTrue(all("delta" in event for event in events))
