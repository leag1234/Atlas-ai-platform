"""Transient failures cannot silently multiply cost or replay visible output."""

import os
from collections.abc import AsyncGenerator
from agent_provider import AgentRequest, Usage
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from provider_retry import RetryLedger, TransientProviderError, retry_stream
from packages.limits import ProviderLimitError
from serverless import ServerlessPolicy
from serverless_support import environment
from test_quality_gateway import request, result


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_measured_empty_usage_keeps_recovery_affordable(self) -> None:
        with patch.dict(os.environ, environment()):
            policy = ServerlessPolicy()
            model = policy.models["text"]
            ledger = RetryLedger()
            calls = 0

            async def upstream(
                req: AgentRequest, selected: str, timeout: float
            ) -> AsyncGenerator[dict[str, object], None]:
                nonlocal calls
                calls += 1
                if calls < 4:
                    raise TransientProviderError(
                        "empty_completion",
                        usage=Usage(prompt_tokens=20, completion_tokens=1),
                    )
                yield result("Recovered", 10)

            allowance = policy.cost(model, 100, 100) + 3 * policy.cost(model, 20, 1)
            with patch("provider_retry.asyncio.sleep", new_callable=AsyncMock):
                events = [
                    e
                    async for e in retry_stream(
                        upstream,
                        request(max_tokens=100),
                        model,
                        120,
                        allowance,
                        100,
                        ledger,
                    )
                ]
            self.assertEqual(calls, 4)
            self.assertIn("Recovered", str(events))
            self.assertEqual(ledger.spent, 3 * policy.cost(model, 20, 1))
            self.assertEqual((ledger.prompt_tokens, ledger.completion_tokens), (60, 3))
            self.assertFalse(ledger.estimated)

    async def test_invalid_measured_usage_never_retries(self) -> None:
        for prompt, output in ((101, 1), (20, 101)):
            with patch.dict(os.environ, environment()):
                model = ServerlessPolicy().models["text"]
                calls = 0

                async def upstream(
                    req: AgentRequest, selected: str, timeout: float
                ) -> AsyncGenerator[dict[str, object], None]:
                    nonlocal calls
                    calls += 1
                    raise TransientProviderError(
                        "empty_completion",
                        usage=Usage(prompt_tokens=prompt, completion_tokens=output),
                    )
                    yield {}

                with self.assertRaisesRegex(
                    RuntimeError, "provider_usage_exceeds_reservation"
                ):
                    _ = [
                        e
                        async for e in retry_stream(
                            upstream,
                            request(max_tokens=100),
                            model,
                            120,
                            Decimal("0.1"),
                            100,
                            RetryLedger(),
                        )
                    ]
                self.assertEqual(calls, 1)

    async def test_three_delayed_retries_keep_same_request_and_charge_unknowns(
        self,
    ) -> None:
        with patch.dict(os.environ, environment()):
            policy = ServerlessPolicy()
            model = policy.models["text"]
            original = request(max_tokens=20)
            ledger = RetryLedger()
            calls: list[object] = []

            async def upstream(
                req: AgentRequest, selected: str, timeout: float
            ) -> AsyncGenerator[dict[str, object], None]:
                calls.append((req, selected))
                if len(calls) < 4:
                    raise TransientProviderError("provider_error")
                yield result("Recovered useful response", 10)

            with patch("provider_retry.asyncio.sleep", new_callable=AsyncMock) as sleep:
                events = [
                    e
                    async for e in retry_stream(
                        upstream, original, model, 120, Decimal("0.10"), 100, ledger
                    )
                ]
            self.assertEqual(calls, [(original, model)] * 4)
            self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 5, 15])
            self.assertEqual(ledger.spent, 3 * policy.cost(model, 100, 20))
            self.assertIn("Recovered useful", str(events))

    async def test_unaffordable_retry_makes_no_second_paid_call(self) -> None:
        with patch.dict(os.environ, environment()):
            policy = ServerlessPolicy()
            model = policy.models["text"]
            calls: list[object] = []

            async def upstream(
                req: AgentRequest, selected: str, timeout: float
            ) -> AsyncGenerator[dict[str, object], None]:
                calls.append(selected)
                raise TransientProviderError("provider_error")
                yield {}

            with (
                patch("provider_retry.asyncio.sleep", new_callable=AsyncMock),
                self.assertRaises(ProviderLimitError),
            ):
                _ = [
                    e
                    async for e in retry_stream(
                        upstream,
                        request(max_tokens=20),
                        model,
                        120,
                        policy.cost(model, 100, 20),
                        100,
                        RetryLedger(),
                    )
                ]
            self.assertEqual(len(calls), 1)

    async def test_visible_output_is_never_duplicated(self) -> None:
        with patch.dict(os.environ, environment()):
            model = ServerlessPolicy().models["text"]
            calls: list[object] = []

            async def upstream(
                req: AgentRequest, selected: str, timeout: float
            ) -> AsyncGenerator[dict[str, object], None]:
                calls.append(selected)
                yield {"delta": {"content": "Already displayed"}}
                raise TransientProviderError("provider_error")

            with self.assertRaises(TransientProviderError):
                _ = [
                    e
                    async for e in retry_stream(
                        upstream,
                        request(max_tokens=20),
                        model,
                        120,
                        Decimal("0.1"),
                        100,
                        RetryLedger(),
                    )
                ]
            self.assertEqual(len(calls), 1)

    async def test_gateway_reports_all_transient_reservations(self) -> None:
        from quality import input_bound, stream_quality
        from agent_provider import AgentProvider
        from services.orchestrator.model import WireTurn

        calls: list[str] = []

        async def upstream(
            req: AgentRequest, selected: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            calls.append(selected)
            if len(calls) < 2:
                raise TransientProviderError("provider_error")
            yield result("Recovered complete answer", 10)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            patch("provider_retry.asyncio.sleep", new_callable=AsyncMock),
        ):
            original = request(profile="atlas-qwen", max_tokens=100, timeout=120.0)
            policy = ServerlessPolicy()
            events = [e async for e in stream_quality(AgentProvider(), original)]
            wire = WireTurn.model_validate(events[-1]["result"])
            expected = policy.cost(
                calls[0], input_bound(original.messages, original.tools), 100
            ) + policy.cost(calls[-1], 20, 10)
        self.assertEqual(wire.cost_eur, expected)
        self.assertEqual(wire.retry_completion_tokens, 100)
        self.assertEqual(wire.usage.completion_tokens, 110)
        self.assertEqual(len(calls), 2)

    async def test_persistent_transient_stops_after_three_retries(self) -> None:
        calls: list[str] = []

        async def upstream(
            req: AgentRequest, selected: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            calls.append(selected)
            raise TransientProviderError("provider_error")
            yield {}

        with (
            patch.dict(os.environ, environment()),
            patch("provider_retry.asyncio.sleep", new_callable=AsyncMock),
            self.assertRaises(TransientProviderError),
        ):
            model = ServerlessPolicy().models["text"]
            _ = [
                e
                async for e in retry_stream(
                    upstream,
                    request(max_tokens=20),
                    model,
                    120,
                    Decimal("0.10"),
                    100,
                    RetryLedger(),
                )
            ]
        self.assertEqual(len(calls), 4)


class ExhaustedUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_last_empty_attempt_is_settled_before_fallback(self) -> None:
        from quality import stream_quality
        from agent_provider import AgentProvider
        from services.orchestrator.model import WireTurn

        calls: list[str] = []

        async def upstream(
            req: AgentRequest, selected: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            calls.append(selected)
            if len(calls) <= 1:
                raise TransientProviderError(
                    "empty_completion",
                    usage=Usage(prompt_tokens=20, completion_tokens=1),
                )
            yield result("Recovered after exhausted primary", 10)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            patch("provider_retry.asyncio.sleep", new_callable=AsyncMock),
        ):
            policy = ServerlessPolicy()
            events = [
                e
                async for e in stream_quality(
                    AgentProvider(),
                    request(profile="atlas-qwen", max_tokens=100, timeout=120.0),
                )
            ]
            wire = WireTurn.model_validate(events[-1]["result"])
            expected = policy.cost(calls[0], 20, 1) + policy.cost(calls[-1], 20, 10)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[-1])
        self.assertEqual(wire.cost_eur, expected)
        self.assertEqual(wire.usage.prompt_tokens, 40)
        self.assertEqual(wire.usage.completion_tokens, 11)
        self.assertEqual(wire.retry_completion_tokens, 1)
        self.assertFalse(wire.token_split_estimated)

    async def test_invalid_last_usage_prevents_paid_fallback(self) -> None:
        from quality import stream_quality
        from agent_provider import AgentProvider

        calls = 0

        async def upstream(
            req: AgentRequest, selected: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            if calls <= 1:
                raise TransientProviderError(
                    "empty_completion",
                    usage=Usage(prompt_tokens=20, completion_tokens=101),
                )
            yield result("Must not pay for fallback", 10)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            patch("provider_retry.asyncio.sleep", new_callable=AsyncMock),
            self.assertRaisesRegex(RuntimeError, "provider_usage_exceeds_reservation"),
        ):
            _ = [
                e
                async for e in stream_quality(
                    AgentProvider(),
                    request(profile="atlas-qwen", max_tokens=100, timeout=120.0),
                )
            ]
        self.assertEqual(calls, 1)
