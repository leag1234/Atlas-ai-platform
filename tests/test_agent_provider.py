"""REQ-FIN-002: prices and responses fail closed at the provider boundary."""

import unittest
from collections.abc import AsyncIterator
from decimal import Decimal
from unittest.mock import patch

from agent_provider import AgentProvider, Completion


class ProviderTests(unittest.TestCase):
    def test_error_body_cannot_be_accepted_alongside_valid_answer(self) -> None:
        with self.assertRaises(ValueError):
            Completion.model_validate(
                {
                    "error": {"message": "private provider diagnostic"},
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": "answer"}}
                    ],
                }
            )

    def test_missing_price_denies_provider(self) -> None:
        with patch.dict("os.environ", {"ESCALATION_MODEL": "absent"}):
            with self.assertRaisesRegex(ValueError, "missing_price"):
                AgentProvider()

    def test_config_contains_no_model_identifier(self) -> None:
        with patch.dict("os.environ", {"ESCALATION_MODEL": "local"}):
            info = AgentProvider().configuration()
        self.assertEqual(Decimal(str(info["input_eur_per_mtok"])), Decimal(0))
        self.assertNotIn("model", info)

    def test_response_validation(self) -> None:
        values: tuple[dict[str, object], ...] = (
            {},
            {"choices": []},
            {"choices": [{"finish_reason": "length", "message": {"content": "cut"}}]},
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Completion.model_validate(value)


class ProviderTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_response_validation_and_output_limit(self) -> None:
        import json
        from unittest.mock import MagicMock, AsyncMock

        response = MagicMock()
        response.status = 200
        data: dict[str, object] = {
            "usage": {"prompt_tokens": 100, "completion_tokens": 1800},
            "choices": [
                {"finish_reason": "stop", "message": {"content": "complete answer"}}
            ],
        }

        async def pieces(size: int) -> AsyncIterator[bytes]:
            yield json.dumps(data).encode()

        response.content.iter_chunked = pieces
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        session = MagicMock()
        session.post.return_value = context
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=session)
        payload = {
            "messages": [{"role": "user", "content": "test"}],
            "tools": [{}],
            "timeout": 1.0,
        }
        with (
            patch.dict(
                "os.environ",
                {
                    "ESCALATION_MODEL": "local",
                    "SCW_GENERATIVE_BASE_URL": "https://example.invalid/v1",
                    "SCW_GENERATIVE_API_KEY": "test-only",
                },
            ),
            patch("agent_provider.aiohttp.ClientSession", return_value=client),
        ):
            provider = AgentProvider()
            answer = await provider.complete(payload)
            self.assertEqual(answer["text"], "complete answer")
            self.assertEqual(session.post.call_args.kwargs["json"]["max_tokens"], 2048)
            self.assertEqual(
                session.post.call_args.kwargs["json"]["reasoning_effort"], "none"
            )
            from packages.limits import LimitError

            data["usage"] = {"prompt_tokens": 100, "completion_tokens": 2049}
            with self.assertRaises(LimitError) as caught:
                await provider.complete(payload)
            self.assertEqual(
                (caught.exception.measured, caught.exception.limit), (2049, 2048)
            )
            data["usage"] = {"prompt_tokens": 100, "completion_tokens": 1800}
            data["choices"] = [
                {"finish_reason": "stop", "message": {"content": "x" * 32001}}
            ]
            with self.assertRaises(LimitError) as caught:
                await provider.complete(payload)
            self.assertEqual(
                (caught.exception.measured, caught.exception.limit), (32001, 32000)
            )
            data["choices"] = [
                {"finish_reason": "stop", "message": {"content": "x" * 128001}}
            ]
            with self.assertRaises(LimitError) as caught:
                await provider.complete(payload)
            self.assertEqual(
                (caught.exception.measured, caught.exception.limit), (128001, 128000)
            )
            data["choices"] = [
                {"finish_reason": "length", "message": {"content": "cut"}}
            ]
            with self.assertRaisesRegex(RuntimeError, "provider_error"):
                await provider.complete(payload)
            data["choices"] = [
                {"finish_reason": "stop", "message": {"content": " \n\t "}}
            ]
            with self.assertRaisesRegex(RuntimeError, "provider_response_invalid"):
                await provider.complete(payload)
            response.status = 429
            with self.assertRaisesRegex(RuntimeError, "provider_error"):
                await provider.complete(payload)
