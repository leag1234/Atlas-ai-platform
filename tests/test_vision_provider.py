"""Recorded vision answers, real reservation logic, no paid transport in tests."""

import copy
import json
from decimal import Decimal
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from pydantic import ValidationError
from vision import VisionProvider, VisionRequest
from test_vision_schema import payload, picture


class VisionProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_answer_and_exact_image_transport(self) -> None:
        provider, value = VisionProvider(), payload(picture())
        recorded = json.loads(
            Path("tests/cassettes/vision/response-0.json").read_text()
        )
        with patch.object(
            provider, "post", new=AsyncMock(return_value=recorded["response"])
        ) as post:
            result = await provider.complete(value)
        self.assertEqual(
            result["text"], recorded["response"]["choices"][0]["message"]["content"]
        )
        self.assertEqual(
            Decimal(str(result["cost_eur"])), Decimal(recorded["cost_eur"])
        )
        self.assertLessEqual(Decimal(str(result["reserved_eur"])), Decimal("0.05"))
        self.assertIsNotNone(post.await_args)
        assert post.await_args is not None
        body = post.await_args.args[0]
        self.assertEqual(body["model"], provider.config["model"])
        self.assertEqual(body["messages"][1:], value["messages"])
        self.assertNotIn("tools", body)
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertIn(
            Path("prompts/chat.txt").read_text(), body["messages"][0]["content"]
        )

    async def test_budget_and_context_stop_before_transport(self) -> None:
        for boundary in ("cost", "context"):
            provider = VisionProvider()
            if boundary == "cost":
                provider.input_price = Decimal(1000000)
            else:
                provider.config["context_tokens"] = 100
            with patch.object(provider, "post", new=AsyncMock()) as post:
                with (
                    self.subTest(boundary=boundary),
                    self.assertRaises((RuntimeError, ValueError)),
                ):
                    await provider.complete(payload(picture()))
                post.assert_not_awaited()

    async def test_bad_usage_and_tool_calls_rejected(self) -> None:
        recorded = json.loads(
            Path("tests/cassettes/vision/response-0.json").read_text()
        )["response"]
        for failure in ("missing", "oversized", "length"):
            data = copy.deepcopy(recorded)
            if failure == "missing":
                del data["usage"]
            elif failure == "oversized":
                data["usage"]["prompt_tokens"] = 1000000
            else:
                data["choices"][0]["finish_reason"] = "length"
            provider = VisionProvider()
            with patch.object(provider, "post", new=AsyncMock(return_value=data)):
                with (
                    self.subTest(failure=failure),
                    self.assertRaises((RuntimeError, ValidationError)),
                ):
                    await provider.complete(payload(picture()))

    async def test_timeout_is_not_retried(self) -> None:
        provider = VisionProvider()
        with patch.object(
            provider, "post", new=AsyncMock(side_effect=TimeoutError)
        ) as post:
            with self.assertRaises(TimeoutError):
                await provider.complete(payload(picture()))
            self.assertEqual(post.await_count, 1)

    def test_text_only_and_invalid_price_rejected(self) -> None:
        with self.assertRaises(ValueError):
            VisionProvider().reserve(
                VisionRequest.model_validate(
                    {"messages": [{"role": "user", "content": "Bonjour"}]}
                )
            )
        for price in ("NaN", "-1", "Infinity"):
            with patch(
                "vision.yaml.safe_load",
                return_value={"input_eur_per_mtok": price, "output_eur_per_mtok": 0},
            ):
                with self.subTest(price=price), self.assertRaises(ValueError):
                    VisionProvider()
