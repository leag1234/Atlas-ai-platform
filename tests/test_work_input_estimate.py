"""Work-step input reservation uses a token estimate without changing chat."""

from collections.abc import AsyncGenerator
from decimal import Decimal
import os
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
from quality import WORK_BYTES_PER_TOKEN, input_bound, stream_quality, work_step
from serverless import ServerlessPolicy
from serverless_support import environment
from test_quality_gateway import request, result


class WorkInputEstimateTests(unittest.IsolatedAsyncioTestCase):
    def test_work_step_is_identified_by_switch_tool_group(self) -> None:
        switch: dict[str, object] = {
            "type": "function",
            "function": {"name": "switch_tool_group"},
        }
        self.assertTrue(work_step([switch]))
        self.assertFalse(work_step([]))
        self.assertFalse(
            work_step([{"type": "function", "function": {"name": "web_search"}}])
        )

    async def test_work_step_reserves_about_three_bytes_per_token(self) -> None:
        tools = [{"type": "function", "function": {"name": "switch_tool_group"}}]
        req = request(
            tools=tools,
            messages=[
                {
                    "role": "user",
                    "content": "x" * 45_000,
                }
            ],
            max_tokens=3000,
        )
        byte_bound = input_bound(req.messages, req.tools)
        expected = -(-byte_bound // WORK_BYTES_PER_TOKEN)
        with patch.dict(os.environ, environment()):
            policy = ServerlessPolicy()
            primary = policy.models[policy.public_profiles[str(req.profile)]]
            estimated_cost = policy.cost(primary, expected, 1)
            byte_cost = policy.cost(primary, byte_bound, 1)
            budget = (estimated_cost + byte_cost) / 2
        seen: list[AgentRequest] = []
        models: list[str] = []

        async def transport(
            current: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            seen.append(current)
            models.append(model)
            response = result("Work result", 30)
            turn = response["result"]
            assert isinstance(turn, dict)
            turn["usage"]["prompt_tokens"] = expected
            yield response

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
        ):
            events = [
                event
                async for event in stream_quality(
                    AgentProvider(), req.model_copy(update={"budget_eur": str(budget)})
                )
            ]
        self.assertEqual(models, [primary])
        self.assertEqual(seen[0].tool_choice, "auto")
        self.assertEqual(seen[0].tools, tools)
        answer = events[-1]["result"]
        assert isinstance(answer, dict)
        self.assertLess(Decimal(str(answer["cost_eur"])), Decimal("0.10"))
        self.assertEqual(answer["usage"]["prompt_tokens"], expected)

    async def test_non_work_request_reserves_the_full_byte_bound(self) -> None:
        req = request(
            tools=[{"type": "function", "function": {"name": "web_search"}}],
            messages=[{"role": "user", "content": "x" * 45_000}],
            max_tokens=3000,
        )
        byte_bound = input_bound(req.messages, req.tools)
        seen: list[AgentRequest] = []

        async def transport(
            current: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            seen.append(current)
            response = result("Search result", 30)
            turn = response["result"]
            assert isinstance(turn, dict)
            turn["usage"]["prompt_tokens"] = byte_bound
            yield response

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
        ):
            events = [
                event
                async for event in stream_quality(
                    AgentProvider(), req.model_copy(update={"budget_eur": "0.10"})
                )
            ]
        self.assertEqual(seen[0].tool_choice, "none")
        answer = events[-1]["result"]
        assert isinstance(answer, dict)
        self.assertEqual(answer["usage"]["prompt_tokens"], byte_bound)

    async def test_work_provider_usage_between_estimate_and_bytes_is_accepted(
        self,
    ) -> None:
        req = request(
            tools=[{"type": "function", "function": {"name": "switch_tool_group"}}],
            messages=[{"role": "user", "content": "x" * 45_000}],
            max_tokens=3000,
        )
        byte_bound = input_bound(req.messages, req.tools)
        estimated = -(-byte_bound // WORK_BYTES_PER_TOKEN)
        provider_prompt = (estimated + byte_bound) // 2

        async def transport(
            current: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            response = result("Work result", 30)
            turn = response["result"]
            assert isinstance(turn, dict)
            turn["usage"]["prompt_tokens"] = provider_prompt
            yield response

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
        ):
            events = [
                event
                async for event in stream_quality(
                    AgentProvider(), req.model_copy(update={"budget_eur": "0.10"})
                )
            ]
        answer = events[-1]["result"]
        assert isinstance(answer, dict)
        self.assertEqual(answer["usage"]["prompt_tokens"], provider_prompt)
