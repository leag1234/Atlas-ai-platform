"""A failed provider attempt is charged what it cost, and a failure stays a failure.

Replay of staging 2026-11-16: two calls of one turn each met an HTTP 503
("Service unavailable", no byte of completion) on their first attempt. Each
refusal kept its full reservation as spent; the second call's recovery no
longer fit the per-call budget and the turn ended as a budget refusal (429)
instead of a provider failure.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from decimal import Decimal
import json
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

from agent_provider import AgentProvider, AgentRequest
from packages.limits import ProviderFailure, ProviderLimitError
from provider_retry import (
    ModelUnavailableError,
    ProviderUnreached,
    RetryLedger,
    TransientProviderError,
    retry_stream,
    unbilled,
)
from quality import input_bound, stream_quality
from serverless import ServerlessPolicy
from serverless_support import environment
from test_streaming_decoder import end, frame

QUESTION = [{"role": "user", "content": "Will it be sunny tomorrow in a coastal town?"}]
REFUSAL = (
    b'{"error":{"type":"internal_server_error","code":"3800",'
    b'"message":"Service unavailable."}}'
)


class Body:
    def __init__(self, pieces: list[bytes]) -> None:
        self.pieces = pieces

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        for piece in self.pieces:
            yield piece


class Response:
    def __init__(self, status: int, pieces: list[bytes]) -> None:
        self.status = status
        self.content = Body(pieces)
        self.headers: dict[str, str] = {}

    async def __aenter__(self) -> "Response":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class Provider:
    """Answers each HTTP call with the next scripted status (200 streams an answer)."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.models: list[str] = []

    def __call__(self, **kwargs: object) -> "Provider":
        return self

    async def __aenter__(self) -> "Provider":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def post(self, url: str, **kwargs: object) -> Response:
        body = kwargs["json"]
        assert isinstance(body, dict)
        self.models.append(str(body["model"]))
        status = self.statuses.pop(0)
        if status == 0:
            raise aiohttp.ClientConnectorError(Mock(), OSError(111, "refused"))
        if status == 200:
            return Response(200, [frame({"content": "Sunny."}), end()])
        return Response(status, [REFUSAL])


def request(budget: Decimal) -> AgentRequest:
    return AgentRequest.model_validate(
        {
            "messages": QUESTION,
            "tools": [],
            "timeout": 5.0,
            "local_enabled": False,
            "profile": "atlas-glm",
            "max_tokens": 500,
            "budget_eur": str(budget),
        }
    )


def first_reservation() -> Decimal:
    """The budget that fits the first attempt and nothing more."""
    policy = ServerlessPolicy()
    role = policy.profile_roles.get("atlas-glm", "text")
    return policy.cost(policy.models[role], input_bound(QUESTION, []), 500)


def events(name: str, logs: list[str]) -> list[dict[str, object]]:
    found = []
    for line in logs:
        start = line.find("{")
        if start >= 0 and f'"event": "{name}"' in line:
            found.append(json.loads(line[start:]))
    return found


class FailedAttemptChargeTests(unittest.IsolatedAsyncioTestCase):
    async def run_turn(
        self, provider: Provider, budget: Decimal
    ) -> tuple[dict[str, object], list[str]]:
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.aiohttp.ClientSession", provider),
            self.assertLogs(level="WARNING") as logs,
        ):
            answer: dict[str, object] = {}
            async for event in stream_quality(AgentProvider(), request(budget)):
                if isinstance(event.get("result"), dict):
                    answer = event["result"]  # type: ignore[assignment]
        return answer, logs.output

    async def test_refusal_then_answer_charges_only_the_answer(self) -> None:
        # Before the fix the refusal kept the whole budget: cost_budget (429).
        provider = Provider(503, 200)
        answer, logs = await self.run_turn(provider, first_reservation())
        policy = ServerlessPolicy()
        self.assertEqual(answer["text"], "Sunny.")
        self.assertEqual(
            Decimal(str(answer["cost_eur"])), policy.cost(provider.models[1], 20, 8)
        )
        self.assertEqual(answer["usage"], {"prompt_tokens": 20, "completion_tokens": 8})
        self.assertFalse(answer["token_split_estimated"])
        failed = events("quality_attempt_failed", logs)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["attempt_charge"], "none")
        self.assertEqual(failed[0]["reserved_unknown_eur"], "0")

    async def test_two_consecutive_calls_with_refusals_keep_the_turn_budget(
        self,
    ) -> None:
        # The staging turn: call 1 refused then answered, call 2 refused then
        # answered, with the turn budget left after call 1.
        budget = first_reservation() * 2
        first, _ = await self.run_turn(Provider(503, 200), budget)
        left = budget - Decimal(str(first["cost_eur"]))
        self.assertGreaterEqual(left, first_reservation())
        second, logs = await self.run_turn(Provider(503, 200), left)
        self.assertEqual(second["text"], "Sunny.")
        self.assertEqual(
            events("quality_attempt_failed", logs)[0]["attempt_charge"], "none"
        )

    async def test_exhausted_provider_is_a_provider_failure_without_charge(
        self,
    ) -> None:
        provider = Provider(503, 503)
        with self.assertRaises(ProviderFailure) as raised:
            await self.run_turn(provider, first_reservation())
        self.assertNotIsInstance(raised.exception, ProviderLimitError)
        self.assertEqual(raised.exception.code, "provider_error")
        self.assertEqual(len(provider.models), 2)

    async def test_unreached_provider_is_unbilled(self) -> None:
        answer, logs = await self.run_turn(Provider(0, 200), first_reservation())
        self.assertEqual(answer["text"], "Sunny.")
        self.assertEqual(
            events("quality_attempt_failed", logs)[0]["attempt_charge"], "none"
        )

    async def test_gateway_timeout_status_keeps_the_reservation(self) -> None:
        # A 504 comes from a proxy that gave up: the model may have run.
        provider = Provider(504, 200)
        answer, logs = await self.run_turn(provider, Decimal("0.10"))
        policy = ServerlessPolicy()
        self.assertGreater(
            Decimal(str(answer["cost_eur"])), policy.cost(provider.models[1], 20, 8)
        )
        self.assertTrue(answer["token_split_estimated"])
        failed = events("quality_attempt_failed", logs)
        self.assertEqual(failed[0]["attempt_charge"], "reservation")

    async def test_unaffordable_recovery_reports_the_provider_failure(self) -> None:
        # Bytes were received, so the cut attempt keeps its reservation (usage
        # unknown) and the recovery no longer fits: the call reports what
        # happened, a provider failure, never cost_budget.
        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            raise TransientProviderError("provider_error", received_bytes=1)
            yield {}

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            self.assertLogs("quality", level="WARNING") as logs,
        ):
            with self.assertRaises(ProviderFailure) as raised:
                _ = [
                    event
                    async for event in stream_quality(
                        AgentProvider(), request(first_reservation())
                    )
                ]
        self.assertEqual(raised.exception.code, "provider_error")
        self.assertEqual(raised.exception.retries, 0)
        failed = events("quality_attempt_failed", logs.output)
        self.assertEqual(failed[0]["attempt_charge"], "reservation")
        unaffordable = events("quality_recovery_unaffordable", logs.output)
        self.assertEqual(len(unaffordable), 1)
        self.assertGreater(
            unaffordable[0]["measured_microeur"], unaffordable[0]["limit_microeur"]
        )

    async def test_unaffordable_recovery_after_timeout_stays_a_timeout(self) -> None:
        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            raise TimeoutError("timeout")
            yield {}

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            self.assertLogs("quality", level="WARNING"),
        ):
            with self.assertRaises(TimeoutError):
                _ = [
                    event
                    async for event in stream_quality(
                        AgentProvider(), request(first_reservation())
                    )
                ]


class RetryLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_retried_refusal_is_not_charged(self) -> None:
        calls = 0

        async def call(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TransientProviderError("provider_error", received_bytes=0)
            yield {"result": {"text": "Sunny."}}

        ledger = RetryLedger()
        with (
            patch.dict(os.environ, environment()),
            patch("provider_retry.asyncio.sleep", AsyncMock()),
            self.assertLogs("provider_retry", level="WARNING") as logs,
        ):
            policy = ServerlessPolicy()
            model = policy.models["text"]
            out = [
                event
                async for event in retry_stream(
                    call,
                    request(Decimal("0.10")),
                    model,
                    30.0,
                    policy.cost(model, 360, 500),
                    360,
                    ledger,
                )
            ]
        self.assertEqual(out, [{"result": {"text": "Sunny."}}])
        self.assertEqual(ledger.spent, 0)
        self.assertFalse(ledger.estimated)
        self.assertTrue(events("provider_transient_retry", logs.output)[0]["unbilled"])

    def test_charging_rule(self) -> None:
        from agent_provider import Usage

        self.assertTrue(unbilled(TransientProviderError("x", received_bytes=0)))
        self.assertTrue(unbilled(ProviderUnreached("provider_error")))
        self.assertTrue(unbilled(ModelUnavailableError("model_unavailable")))
        self.assertFalse(unbilled(TransientProviderError("x")))
        self.assertFalse(unbilled(TransientProviderError("x", received_bytes=5)))
        self.assertFalse(
            unbilled(
                TransientProviderError(
                    "x",
                    received_bytes=0,
                    usage=Usage(prompt_tokens=1, completion_tokens=1),
                )
            )
        )
        self.assertFalse(unbilled(RuntimeError("provider_error")))
        self.assertFalse(unbilled(TimeoutError("timeout")))


if __name__ == "__main__":
    unittest.main()
