"""M7: opting out of local inference and truthful per-call observations."""

import unittest
from unittest.mock import AsyncMock, patch

from agent_provider import AgentProvider
from serverless_support import environment
from services.orchestrator.model import Configuration, GatewayModel


class ChatGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_escalation_only_observation(self) -> None:
        payload = {
            "messages": [{"role": "user", "content": "Translate hello."}],
            "tools": [{}],
            "timeout": 5.0,
            "local_enabled": False,
            "observe": True,
        }
        with patch.dict(
            "os.environ",
            {
                **environment(),
                "LOCAL_MODEL": "test-only",
                "LOCAL_API_BASE": "http://127.0.0.1:1/v1",
                "SCW_GENERATIVE_BASE_URL": "https://example.invalid/v1",
                "SCW_GENERATIVE_API_KEY": "test-only",
            },
        ):
            provider = AgentProvider()
            with patch.object(
                provider,
                "_complete",
                new=AsyncMock(
                    return_value={
                        "text": "bonjour",
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                    }
                ),
            ) as call:
                result = await provider.complete(payload)
                self.assertEqual(call.call_count, 1)
                self.assertEqual(call.call_args.args[1], "https://example.invalid/v1")
                self.assertEqual(
                    result["observation"],
                    {
                        "provider": "escalade",
                        "route": "simple",
                        "task_type": "text",
                        "fallback": False,
                    },
                )
            payload["local_enabled"] = True
            with patch.object(
                provider,
                "_complete",
                new=AsyncMock(
                    return_value={
                        "text": "bonjour",
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                    }
                ),
            ):
                result = await provider.complete(payload)
                self.assertEqual(
                    result["observation"], {"provider": "local", "route": "simple"}
                )

    async def test_client_accumulates_usage_and_observations(self) -> None:
        model = GatewayModel(
            "http://127.0.0.1:1",
            Configuration.model_validate(
                {
                    "input_eur_per_mtok": "0.6",
                    "output_eur_per_mtok": "3.6",
                    "max_tokens": 2048,
                }
            ),
        )
        model.local_enabled = False
        model.observing = True
        wire = {
            "text": "bonjour",
            "calls": [],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            "observation": {"provider": "escalade", "route": "simple"},
        }
        with patch.object(model, "post", new=AsyncMock(return_value=wire)) as call:
            await model.complete([], 1)
            await model.complete([], 1)
            self.assertEqual(model.input_tokens, 40)
            self.assertEqual(model.output_tokens, 20)
            self.assertEqual(len(model.observations), 2)
            self.assertFalse(call.call_args.args[1]["local_enabled"])
            self.assertTrue(call.call_args.args[1]["observe"])
        wire["observation"] = {"provider": "invented", "route": "simple"}
        with patch.object(model, "post", new=AsyncMock(return_value=wire)):
            with self.assertRaises(ValueError):
                await model.complete([], 1)

    async def test_history_is_preserved_inside_budgeted_loop(self) -> None:
        from decimal import Decimal
        from unittest.mock import Mock
        from services.orchestrator.loop import Query, Reservation, Turn, run

        model = Mock()
        model.estimate.return_value = Reservation(20, Decimal("0.001"))
        model.complete = AsyncMock(return_value=Turn("Bonjour"))
        history: list[dict[str, object]] = [
            {"role": "user", "content": "Guten Tag"},
            {"role": "assistant", "content": "Bonjour"},
        ]
        result = await run(
            Query(question="Traduis cela.", lang="fr"),
            model,
            Mock(),
            "system",
            history=history,
        )
        self.assertEqual(result.state, "done")
        messages = model.complete.call_args.args[0]
        self.assertEqual(messages[1:3], history)
        self.assertEqual(model.estimate.call_args.args[0], messages)
