"""M8: preserve deadline and payload across one serverless fallback."""

import asyncio
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
from serverless_support import environment


class ServerlessTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        context = patch.dict("os.environ", environment())
        context.start()
        self.addCleanup(context.stop)

    async def test_code_task_and_fallback_are_observed(self) -> None:
        provider = AgentProvider()
        attempts: list[str] = []
        payload = {
            "messages": [{"role": "user", "content": "Write a Python function"}],
            "tools": [{}],
            "timeout": 10.0,
            "local_enabled": False,
            "observe": True,
        }

        async def transport(
            request: AgentRequest, endpoint: str, model: str, key: str, timeout: float
        ) -> dict[str, object]:
            self.assertEqual(request.messages, payload["messages"])
            self.assertTrue(endpoint.startswith("https://"))
            self.assertLessEqual(timeout, 10.0)
            attempts.append(model)
            if len(attempts) == 1:
                raise RuntimeError("provider_error")
            return {
                "text": "def add(a, b): return a + b",
                "calls": [],
                "usage": {"prompt_tokens": 30, "completion_tokens": 12},
            }

        with patch.object(provider, "_complete", side_effect=transport):
            result = await provider.complete(payload)
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0], attempts[1])
        observation = result["observation"]
        assert isinstance(observation, dict)
        self.assertEqual(observation["task_type"], "code")
        self.assertTrue(observation["fallback"])

    async def test_no_third_attempt_after_double_failure(self) -> None:
        provider = AgentProvider()
        with patch.object(
            provider, "_complete", side_effect=RuntimeError("provider_error")
        ) as call:
            with self.assertRaises(RuntimeError):
                await provider.complete(
                    {
                        "messages": [{"role": "user", "content": "Bonjour"}],
                        "tools": [{}],
                        "timeout": 5.0,
                        "local_enabled": False,
                    }
                )
            self.assertEqual(call.call_count, 2)

    async def test_budget_stops_before_transport(self) -> None:
        provider = AgentProvider()
        with patch.object(provider, "_complete") as call:
            with self.assertRaises(ValueError):
                await provider.complete(
                    {
                        "messages": [{"role": "user", "content": "x" * 300000}],
                        "tools": [{}],
                        "timeout": 5.0,
                        "local_enabled": False,
                    }
                )
            call.assert_not_called()

    async def test_deadline_includes_primary_and_fallback(self) -> None:
        async def slow(*args: object) -> dict[str, object]:
            await asyncio.sleep(1)
            self.fail("deadline escaped")

        provider = AgentProvider()
        with patch.object(provider, "_complete", side_effect=slow):
            with self.assertRaises(TimeoutError):
                await provider.complete(
                    {
                        "messages": [{"role": "user", "content": "Bonjour"}],
                        "tools": [{}],
                        "timeout": 0.02,
                        "local_enabled": False,
                    }
                )


if __name__ == "__main__":
    unittest.main()


class FallbackLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_primary_usage_keeps_reservation(self) -> None:
        from unittest.mock import AsyncMock
        from serverless import ServerlessPolicy
        from services.orchestrator.model import Configuration, GatewayModel

        model = GatewayModel(
            "http://localhost",
            Configuration.model_validate(ServerlessPolicy().configuration()),
        )
        messages: list[dict[str, object]] = [{"role": "user", "content": "Bonjour"}]
        reserved = model.estimate(messages)
        wire = {
            "text": "Bonjour",
            "calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
            "observation": {
                "provider": "escalade",
                "route": "complexe",
                "task_type": "text",
                "fallback": True,
            },
        }
        with patch.object(model, "post", new=AsyncMock(return_value=wire)):
            turn = await model.complete(messages, 5.0)
        assert turn.usage is not None
        self.assertEqual(turn.usage.cost, reserved.cost)
        self.assertEqual(turn.usage.tokens, 11)
