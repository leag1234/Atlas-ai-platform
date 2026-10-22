"""File publication remains executable when optional research would be stopped."""

from collections.abc import AsyncGenerator
from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
from quality import input_bound, stream_quality
from serverless import ServerlessPolicy
from serverless_support import environment
from test_quality_gateway import request, result


class FileToolBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_invalid_arguments_stop_after_two_attempts(self) -> None:
        calls = 0

        async def transport(
            current: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            response = result("", 30)
            turn = response["result"]
            assert isinstance(turn, dict)
            turn["calls"] = [
                {"id": "invalid", "name": "terminal_command", "arguments": "[1]"}
            ]
            yield response

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
            self.assertRaisesRegex(RuntimeError, "invalid_tool_arguments"),
        ):
            _ = [event async for event in stream_quality(AgentProvider(), request())]
        self.assertEqual(calls, 2)

    async def test_invalid_command_json_is_recovered_before_execution(self) -> None:
        calls = 0

        async def transport(
            current: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            response = result("", 30)
            turn = response["result"]
            assert isinstance(turn, dict)
            turn["calls"] = [
                {
                    "id": "command-1",
                    "name": "terminal_command",
                    "arguments": '{"command": "unterminated'
                    if calls == 1
                    else '{"command": "pwd"}',
                }
            ]
            yield response

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
        ):
            events = [
                event async for event in stream_quality(AgentProvider(), request())
            ]
        self.assertEqual(calls, 2)
        answer = events[-1]["result"]
        assert isinstance(answer, dict)
        self.assertEqual(answer["calls"][0]["arguments"], '{"command": "pwd"}')

    async def test_publication_is_not_replaced_by_answer_only_history(self) -> None:
        tool = {"type": "function", "function": {"name": "publish_document"}}
        with patch.dict(os.environ, environment()):
            policy = ServerlessPolicy()
            req = request(tools=[tool], max_tokens=3000)
            model = policy.models[policy.public_profiles[str(req.profile)]]
            bound = input_bound(req.messages, req.tools)
            cap = policy.cost(model, bound, 3000) * Decimal("1.5")
            seen: list[AgentRequest] = []

            async def transport(
                current: AgentRequest, model: str, timeout: float
            ) -> AsyncGenerator[dict[str, object], None]:
                seen.append(current)
                yield result("Publication result", 30)

            with patch("stream_transport.attempt", transport):
                events = [
                    event
                    async for event in stream_quality(
                        AgentProvider(), req.model_copy(update={"budget_eur": str(cap)})
                    )
                ]
            self.assertEqual(seen[0].tool_choice, "auto")
            self.assertEqual(seen[0].messages, req.messages)
            answer = events[-1]["result"]
            assert isinstance(answer, dict)
            self.assertLessEqual(Decimal(str(answer["cost_eur"])), cap)
