"""Legacy retries stay inside their already reserved primary/fallback envelope."""

from decimal import Decimal
from collections.abc import AsyncGenerator
import os
import unittest
from unittest.mock import AsyncMock, patch

from agent_provider import AgentProvider, AgentRequest
from provider_retry import TransientProviderError
from serverless import ServerlessPolicy
from serverless_support import environment


class LegacyRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_exhaustion_refuses_another_transport_before_spending(self) -> None:
        from packages.limits import ProviderLimitError

        with patch.dict(os.environ, environment()):
            request = AgentRequest(
                messages=[{"role": "user", "content": "Hello"}],
                tools=[],
                timeout=120.0,
                local_enabled=False,
            )
            plan = ServerlessPolicy().reserve(request.messages, [])
            call = AsyncMock(side_effect=TransientProviderError("provider_error"))
            with (
                patch.object(AgentProvider, "_complete", call),
                patch("provider_retry.asyncio.sleep", new_callable=AsyncMock),
                self.assertRaises(ProviderLimitError),
            ):
                await AgentProvider().complete(request.model_dump())
            self.assertGreater(call.call_count, 1)
            self.assertLessEqual(
                call.call_count * plan.primary_bound, plan.reserved_eur
            )
            self.assertEqual({c.args[2] for c in call.call_args_list}, {plan.primary})

    async def test_client_charges_unknown_retry_even_without_observation(self) -> None:
        from services.orchestrator.model import Configuration, GatewayModel

        with patch.dict(os.environ, environment()):
            model = GatewayModel(
                "http://localhost",
                Configuration.model_validate(ServerlessPolicy().configuration()),
            )
            messages: list[dict[str, object]] = [{"role": "user", "content": "Hello"}]
            held = model.estimate(messages).cost
            wire = {
                "text": "Hello",
                "calls": [],
                "cost_eur": str(held),
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }
            with patch.object(model, "post", new=AsyncMock(return_value=wire)):
                turn = await model.complete(messages, 120.0)
            assert turn.usage is not None
            self.assertEqual(turn.usage.cost, held)

    async def test_legacy_stream_retries_inside_original_envelope(self) -> None:
        from stream_transport import stream

        with patch.dict(os.environ, environment()):
            payload = AgentRequest(
                messages=[{"role": "user", "content": "Hello"}],
                tools=[],
                timeout=120.0,
                local_enabled=False,
            )
            plan = ServerlessPolicy().reserve(payload.messages, [])
            selected = []

            async def attempt(
                request: AgentRequest, model: str, timeout: float
            ) -> AsyncGenerator[dict[str, object], None]:
                selected.append(model)
                if len(selected) == 1:
                    raise TransientProviderError("provider_error")
                yield {
                    "result": {
                        "text": "Hello",
                        "calls": [],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
                    }
                }

            with (
                patch("stream_transport.attempt", attempt),
                patch("provider_retry.asyncio.sleep", new_callable=AsyncMock),
            ):
                events = [
                    event
                    async for event in stream(AgentProvider(), payload.model_dump())
                ]
            self.assertEqual(selected, [plan.primary] * 2)
            result = events[-1]["result"]
            assert isinstance(result, dict)
            self.assertGreaterEqual(
                Decimal(str(result["cost_eur"])), plan.primary_bound
            )
            self.assertLessEqual(Decimal(str(result["cost_eur"])), plan.reserved_eur)

    async def test_retry_same_model_before_fallback_and_keep_unknown_charge(
        self,
    ) -> None:
        with patch.dict(os.environ, environment()):
            request = AgentRequest(
                messages=[{"role": "user", "content": "Hello"}],
                tools=[],
                timeout=120.0,
                local_enabled=False,
            )
            policy = ServerlessPolicy()
            plan = policy.reserve(request.messages, [])
            answer = {
                "text": "Hello",
                "calls": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }
            call = AsyncMock(
                side_effect=[TransientProviderError("provider_error"), answer]
            )
            with (
                patch.object(AgentProvider, "_complete", call),
                patch("provider_retry.asyncio.sleep", new_callable=AsyncMock) as sleep,
            ):
                result = await AgentProvider().complete(request.model_dump())
            self.assertEqual(
                [c.args[2] for c in call.call_args_list], [plan.primary] * 2
            )
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [2])
            self.assertEqual(
                Decimal(str(result["cost_eur"])),
                plan.primary_bound + policy.cost(plan.primary, 10, 4),
            )
            self.assertLessEqual(Decimal(str(result["cost_eur"])), plan.reserved_eur)
