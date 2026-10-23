"""Configured-provider fallback has one bounded, model-specific recovery."""

from collections.abc import AsyncGenerator
import os
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
from provider_retry import ProviderResponseError, TransientProviderError
from quality import stream_quality
from serverless import ServerlessPolicy
from packages.limits import ProviderLimitError


def request() -> AgentRequest:
    return AgentRequest(
        messages=[{"role": "user", "content": "Explain the transfer limit."}],
        tools=[],
        timeout=5.0,
        local_enabled=False,
        observe=True,
        profile="atlas-qwen",
        max_tokens=100,
    )


def answer() -> dict[str, object]:
    return {
        "result": {
            "text": "The transfer limit is 64 KiB.",
            "calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8},
            "finish_reason": "stop",
            "reasoning": "",
            "reasoning_tokens": 0,
        }
    }


class ProviderFallbackModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_503_uses_the_configured_fallback_once(self) -> None:
        calls: list[str] = []

        async def upstream(
            current: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object]]:
            calls.append(model)
            if len(calls) == 1:
                raise TransientProviderError("provider_error")
            yield answer()

        environment = {
            "ATLAS_PROVIDER": "mistral",
            "ATLAS_USD_TO_EUR": "0.9",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("stream_transport.attempt", upstream),
        ):
            events = [
                event async for event in stream_quality(AgentProvider(), request())
            ]
            policy = ServerlessPolicy()
        result = events[-1]["result"]
        assert isinstance(result, dict)
        self.assertEqual(calls, [policy.models["text"], policy.models["text_fallback"]])
        self.assertEqual(result["provider_model"], policy.models["text_fallback"])
        self.assertTrue(result["observation"]["fallback"])

    async def test_400_never_uses_fallback(self) -> None:
        calls: list[str] = []

        async def upstream(
            current: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object]]:
            calls.append(model)
            raise ProviderResponseError(400)
            yield answer()

        with (
            patch.dict(
                os.environ,
                {"ATLAS_PROVIDER": "mistral", "ATLAS_USD_TO_EUR": "0.9"},
                clear=True,
            ),
            patch("stream_transport.attempt", upstream),
            self.assertRaises(ProviderResponseError),
        ):
            _ = [event async for event in stream_quality(AgentProvider(), request())]
        self.assertEqual(len(calls), 1)

    async def test_unset_fallback_never_retries_the_primary(self) -> None:
        calls: list[str] = []

        async def upstream(
            current: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object]]:
            calls.append(model)
            raise TransientProviderError("provider_error")
            yield answer()

        with (
            patch.dict(
                os.environ,
                {"ATLAS_PROVIDER": "mistral", "ATLAS_TEXT_FALLBACK_MODEL": ""},
                clear=True,
            ),
            patch("stream_transport.attempt", upstream),
            self.assertRaisesRegex(RuntimeError, "provider_error"),
        ):
            _ = [event async for event in stream_quality(AgentProvider(), request())]
        self.assertEqual(len(calls), 1)

    async def test_fallback_respects_the_existing_request_budget(self) -> None:
        calls: list[str] = []

        async def upstream(
            current: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object]]:
            calls.append(model)
            raise TransientProviderError("provider_error")
            yield answer()

        with patch.dict(
            os.environ,
            {"ATLAS_PROVIDER": "mistral", "ATLAS_USD_TO_EUR": "0.9"},
            clear=True,
        ):
            policy = ServerlessPolicy()
            fallback = policy.models["text_fallback"]
            policy.prices[fallback].update(
                {"input_eur_per_mtok": "1000", "output_eur_per_mtok": "1000"}
            )
            with (
                patch("stream_transport.attempt", upstream),
                patch("quality.ServerlessPolicy", return_value=policy),
                self.assertRaises(ProviderLimitError),
            ):
                _ = [
                    event
                    async for event in stream_quality(
                        AgentProvider(),
                        request().model_copy(update={"budget_eur": "0.00011"}),
                    )
                ]
        self.assertEqual(len(calls), 1)
