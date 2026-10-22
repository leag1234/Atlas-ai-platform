"""M26 D5: malformed upstream output gets one budgeted recovery, never a bypass."""

from collections.abc import AsyncGenerator
from decimal import Decimal
import asyncio
import os
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
from packages.limits import LimitError, ProviderLimitError
from quality import input_bound, stream_quality
from serverless import ServerlessPolicy
from serverless_support import environment
from test_quality_gateway import request, result


class ProviderLimitRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_finalization_context_is_included_in_model_affordability(
        self,
    ) -> None:
        attempts: list[tuple[AgentRequest, str]] = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            attempts.append((req, model))
            yield result("The evidence supports this complete explanation.", 20)

        messages: list[dict[str, object]] = [
            {"role": "user", "content": "Explain these observations."},
            {"role": "tool", "content": "Evidence. " * 300},
        ]
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            policy = ServerlessPolicy()
            role = policy.public_profiles["atlas-glm"]
            primary = policy.models[role]
            alternate = policy.models[policy.config[role]["fallback"]]
            budget = policy.cost(primary, input_bound(messages, []), 1)
            events = [
                event
                async for event in stream_quality(
                    AgentProvider(),
                    request(messages=messages, budget_eur=str(budget)),
                )
            ]
            self.assertEqual(len(attempts), 1)
            actual, model = attempts[0]
            self.assertEqual(model, alternate)
            self.assertEqual(actual.messages[: len(messages)], messages)
            self.assertGreater(len(actual.messages), len(messages))
            self.assertGreater(
                policy.cost(primary, input_bound(actual.messages, actual.tools), 1),
                budget,
            )
            self.assertLessEqual(
                policy.cost(
                    model, input_bound(actual.messages, actual.tools), actual.max_tokens
                ),
                budget,
            )
            answer = events[-1]["result"]
            assert isinstance(answer, dict)
            self.assertIn("complete explanation", str(answer["text"]))

    async def test_malformed_output_recovers_with_full_failed_reservation(self) -> None:
        attempts: list[tuple[AgentRequest, str]] = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            attempts.append((req, model))
            if len(attempts) == 1:
                raise LimitError("provider_response_limit", 128, 80, "characters")
            yield result("Here is the complete requested explanation.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            events = [e async for e in stream_quality(AgentProvider(), request())]
            policy = ServerlessPolicy()
            first, model = attempts[0]
            expected = policy.cost(
                model, input_bound(first.messages, first.tools), first.max_tokens
            ) + policy.cost(attempts[1][1], 20, 20)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0][0].messages, attempts[1][0].messages)
        answer = events[-1]["result"]
        assert isinstance(answer, dict)
        self.assertEqual(answer["text"], "Here is the complete requested explanation.")
        self.assertEqual(answer["provider_retries"], 1)
        self.assertEqual(Decimal(str(answer["cost_eur"])), expected)
        self.assertLessEqual(expected, Decimal("0.10"))

    async def test_repeat_failure_preserves_measurements_and_stops(self) -> None:
        calls = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            raise LimitError("provider_response_limit", 128, 80, "characters")
            yield {}

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            self.assertRaises(LimitError) as caught,
        ):
            _ = [e async for e in stream_quality(AgentProvider(), request())]
        self.assertEqual(calls, 2)
        self.assertEqual((caught.exception.measured, caught.exception.limit), (128, 80))

    async def test_user_limits_and_visible_partial_answers_are_not_retried(
        self,
    ) -> None:
        for visible, error in (
            (False, ProviderLimitError("cost_budget", 100001, 100000, "microEUR")),
            (False, LimitError("output_budget", 6001, 6000, "tokens")),
            (True, LimitError("provider_response_limit", 128, 80, "characters")),
        ):
            calls = 0

            async def upstream(
                req: AgentRequest, model: str, timeout: float
            ) -> AsyncGenerator[dict[str, object], None]:
                nonlocal calls
                calls += 1
                if visible:
                    yield {"delta": {"content": "Partial answer"}}
                raise error

            with (
                self.subTest(visible=visible, code=error.code),
                patch.dict(os.environ, environment()),
                patch("stream_transport.attempt", upstream),
                self.assertRaises(LimitError) as caught,
            ):
                _ = [e async for e in stream_quality(AgentProvider(), request())]
            self.assertIs(caught.exception, error)
            self.assertEqual(calls, 1)


class VisibleDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_visible_answer_can_finish_after_recovery_window(self) -> None:
        calls = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            yield {"delta": {"content": "The explanation begins."}}
            await asyncio.sleep(1.7)
            yield result("The explanation begins. Here is its conclusion.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            events = [
                event
                async for event in stream_quality(
                    AgentProvider(), request(timeout=2.0, max_tokens=500)
                )
            ]
        self.assertEqual(calls, 1)
        self.assertIn("Here is its conclusion", str(events[-1]))

    async def test_visible_answer_still_obeys_original_deadline(self) -> None:
        calls = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            yield {"delta": {"content": "The explanation begins."}}
            await asyncio.sleep(10)
            yield result("Late answer must not be delivered.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            self.assertRaises(TimeoutError),
        ):
            _ = [
                event
                async for event in stream_quality(
                    AgentProvider(), request(timeout=0.2, max_tokens=500)
                )
            ]
        self.assertEqual(calls, 1)

    async def test_transport_deadline_is_scoped_to_quality_request(self) -> None:
        from stream_transport import attempt
        from test_stream_transport import Session

        with (
            patch.dict(os.environ, environment()),
            patch(
                "stream_transport.aiohttp.ClientSession", return_value=Session()
            ) as client,
        ):
            _ = [
                event
                async for event in stream_quality(
                    AgentProvider(), request(timeout=2.0, max_tokens=500)
                )
            ]
            full = client.call_args.kwargs["timeout"].total
            self.assertGreater(full, 1.6)
            self.assertLessEqual(full, 2.0)
            _ = [
                event
                async for event in attempt(
                    request(timeout=2.0), ServerlessPolicy().models["text"], 0.5
                )
            ]
            self.assertEqual(client.call_args.kwargs["timeout"].total, 0.5)
