"""Reservation arithmetic is independent of model identifiers and network I/O."""

import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from packages.ports import atlas_ports
from services.orchestrator.model import Configuration, GatewayModel


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def model(self) -> GatewayModel:
        return GatewayModel(
            atlas_ports().url("gateway", "localhost"),
            Configuration(
                input_eur_per_mtok=Decimal("0.6"),
                output_eur_per_mtok=Decimal("3.6"),
                max_tokens=512,
            ),
        )

    def test_reservation_unicode_and_growth(self) -> None:
        model = self.model()
        small = model.estimate([{"role": "user", "content": "é"}])
        large = model.estimate([{"role": "user", "content": "é" * 100}])
        self.assertEqual(large.tokens - small.tokens, 198)
        self.assertEqual(
            large.cost - small.cost, Decimal(198) * Decimal("0.6") / 1_000_000
        )
        self.assertGreater(small.cost, Decimal(512) * Decimal("3.6") / 1_000_000)

    async def test_turn_and_invalid_wire(self) -> None:
        model = self.model()
        with patch.object(
            model,
            "post",
            new=AsyncMock(
                return_value={
                    "text": "",
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                    "calls": [
                        {"id": "1", "name": "calculator", "arguments": '{"expr":"1+1"}'}
                    ],
                }
            ),
        ):
            turn = await model.complete([], 1)
            self.assertEqual(turn.calls[0].name, "calculator")
        with patch.object(
            model, "post", new=AsyncMock(return_value={"text": 5, "calls": []})
        ):
            with self.assertRaises(ValueError):
                await model.complete([], 1)

    def test_extended_output_is_reserved(self) -> None:
        model = self.model()
        before = model.estimate([])
        model.configuration = model.configuration.model_copy(
            update={"max_tokens": 2048}
        )
        Configuration.model_validate(model.configuration.model_dump())
        after = model.estimate([])
        self.assertEqual(after.tokens - before.tokens, 1536)
        self.assertEqual(
            after.cost - before.cost, Decimal(1536) * Decimal("3.6") / 1_000_000
        )

    def test_invalid_prices(self) -> None:
        for price in ("-1", "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                Configuration(
                    input_eur_per_mtok=Decimal(price),
                    output_eur_per_mtok=Decimal(0),
                    max_tokens=512,
                )

    def test_known_prefix_usage_reserves_only_new_context(self) -> None:
        model = self.model()
        messages: list[dict[str, object]] = [{"role": "user", "content": "é" * 1000}]
        baseline = model.estimate(messages)
        model.observe(messages, 500)
        extended = messages + [{"role": "assistant", "content": "answer"}]
        estimate = model.estimate(extended)
        self.assertLess(estimate.tokens, baseline.tokens)
        self.assertGreater(estimate.tokens, 500 + 512)
        changed: list[dict[str, object]] = [{"role": "user", "content": "x" * 2000}]
        fresh = self.model().estimate(changed)
        self.assertEqual(model.estimate(changed), fresh)
