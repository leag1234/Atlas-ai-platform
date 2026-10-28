"""Buffered Work can recover from provider failure without exposing partial text."""

from collections.abc import AsyncGenerator
import os
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
from quality import complete
from serverless_support import environment
from test_quality_gateway import request, result


class BufferedRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_nonstream_response_uses_alternate_and_keeps_deadline(
        self,
    ) -> None:
        calls: list[tuple[str, float]] = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            calls.append((model, timeout))
            if len(calls) == 1:
                yield {"delta": {"content": "Incomplete internal answer."}}
                raise TimeoutError("provider_timeout")
            yield result("Recovered complete answer.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            response = await complete(
                AgentProvider(),
                request(
                    tools=[{"type": "function", "function": {"name": "read_document"}}]
                ),
            )
        self.assertEqual(response["text"], "Recovered complete answer.")
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0][0], calls[1][0])
        self.assertLessEqual(calls[0][1], 2.5)
        self.assertLessEqual(calls[1][1], 5)
