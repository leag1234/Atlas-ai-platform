"""A refused primary attempt (before any stream byte) costs nothing."""

import json
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock
from typing import Any

import aiohttp

import dev_gateway as gateway
from provider_retry import TransientProviderError


def _stream_bytes() -> bytes:
    raw: str = json.loads(
        (Path(__file__).parent / "fixtures/devapi-streams.json").read_text()
    )["auto"]
    return raw.encode()


def _plan() -> gateway.Plan:
    fallback = gateway.Plan(
        body=json.dumps({"messages": [{"role": "user", "content": "x"}]}),
        endpoint="https://provider.invalid/v1",
        reserved=0,
        input_rate=Decimal("1"),
        output_rate=Decimal("2"),
        provider="fallback",
        model="fallback-model",
        api_key="key",
        attempt_bound=1000,
    )
    return gateway.Plan(
        body=json.dumps({"messages": [{"role": "user", "content": "x"}]}),
        endpoint="https://provider.invalid/v1",
        reserved=0,
        input_rate=Decimal("1"),
        output_rate=Decimal("2"),
        provider="primary",
        model="primary-model",
        api_key="key",
        attempt_bound=5000,
        fallback=fallback,
    )


def _fallback_transport(plan: gateway.Plan) -> Iterator[bytes]:
    raw = _stream_bytes()
    for offset in range(0, len(raw), 7):
        yield raw[offset : offset + 7]


async def _fallback_chunks(plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
    for chunk in _fallback_transport(plan):
        yield chunk


class RefusedPrimaryTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, primary_transport: Any) -> list[dict[str, Any]]:
        plan = _plan()
        with patch.object(gateway, "transport", primary_transport):
            return [frame async for frame in gateway.events(plan)]

    async def test_402_before_stream_charges_only_fallback(self) -> None:
        async def transport(plan: gateway.Plan) -> AsyncIterator[bytes]:
            if plan.fallback is not None:
                raise TransientProviderError("provider_quota")
            async for chunk in _fallback_chunks(plan):
                yield chunk

        frames = await self._run(transport)
        terminal = frames[-1]
        # 166 prompt * 1 + 11 completion * 2 = 188 micro_eur, no primary bound.
        self.assertEqual(terminal["cost_micro_eur"], 188)

    async def test_429_before_stream_charges_only_fallback(self) -> None:
        async def transport(plan: gateway.Plan) -> AsyncIterator[bytes]:
            if plan.fallback is not None:
                raise TransientProviderError("provider_error")
            async for chunk in _fallback_chunks(plan):
                yield chunk

        frames = await self._run(transport)
        self.assertEqual(frames[-1]["cost_micro_eur"], 188)

    async def test_503_before_stream_charges_only_fallback(self) -> None:
        async def transport(plan: gateway.Plan) -> AsyncIterator[bytes]:
            if plan.fallback is not None:
                raise TransientProviderError("provider_error")
            async for chunk in _fallback_chunks(plan):
                yield chunk

        frames = await self._run(transport)
        self.assertEqual(frames[-1]["cost_micro_eur"], 188)

    async def test_primary_failing_after_streaming_still_adds_bound(self) -> None:
        async def transport(plan: gateway.Plan) -> AsyncIterator[bytes]:
            if plan.fallback is not None:
                yield _stream_bytes()[:10]
                raise TransientProviderError("provider_error", started=True)
            async for chunk in _fallback_chunks(plan):
                yield chunk

        frames = await self._run(transport)
        # 188 fallback cost + 5000 primary attempt bound.
        self.assertEqual(frames[-1]["cost_micro_eur"], 188 + 5000)

    async def test_credit_refusal_is_logged(self) -> None:
        plan = _plan()
        with patch.object(aiohttp, "ClientSession") as factory:
            factory.return_value.__aenter__.return_value = MagicMock()
            session = factory.return_value.__aenter__.return_value
            session.post.return_value.__aenter__.return_value = MagicMock()
            response = session.post.return_value.__aenter__.return_value
            response.status = 402
            with (
                self.assertLogs("dev_gateway", "WARNING") as logs,
                self.assertRaises(TransientProviderError) as caught,
            ):
                _ = [b async for b in gateway.transport(plan)]
        self.assertEqual(caught.exception.args[0], "provider_quota")
        self.assertFalse(caught.exception.started)
        self.assertTrue(
            any("devapi_provider_credit" in message for message in logs.output)
        )
