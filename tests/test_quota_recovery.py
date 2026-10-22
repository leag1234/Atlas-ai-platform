"""Excess tool proposals get one bounded answer opportunity, never extra I/O."""

import json
import unittest
from decimal import Decimal

from services.orchestrator.loop import (
    Call,
    Limits,
    Message,
    Query,
    Reservation,
    Turn,
    run,
)
from services.orchestrator.model import Configuration, GatewayModel
from test_budgets import Tools


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_overflow_is_declined_and_answered_once(self) -> None:
        for ignore_refusal in (False, True):

            class Provider:
                calls = 0

                def estimate(self, messages: list[Message]) -> Reservation:
                    return Reservation(10, Decimal("0.001"))

                async def complete(
                    self, messages: list[Message], timeout: float
                ) -> Turn:
                    self.calls += 1
                    if self.calls == 1:
                        return Turn(
                            "",
                            tuple(
                                Call(
                                    str(i), "web_search", json.dumps({"query": str(i)})
                                )
                                for i in range(5)
                            ),
                        )
                    self_outer.assertEqual(
                        len([m for m in messages if m.get("role") == "tool"]), 5
                    )
                    model = GatewayModel(
                        "http://unused",
                        Configuration(
                            input_eur_per_mtok=Decimal(0),
                            output_eur_per_mtok=Decimal(0),
                            max_tokens=100,
                        ),
                    )
                    self_outer.assertEqual(model.available_tools(messages), [])
                    if ignore_refusal:
                        return Turn("", (Call("extra", "calculator", '{"expr":"1"}'),))
                    return Turn(
                        "Three sources establish the requested result; search quota reached (3 of 3)."
                    )

            self_outer = self
            provider, tools = Provider(), Tools()
            result = await run(
                Query(question="Find evidence", lang="en"),
                provider,
                tools,
                "",
                Limits(profile="atlas-qwen"),
            )
            self.assertEqual(tools.calls, 3)
            self.assertEqual(provider.calls, 2)
            self.assertEqual(result.state, "stopped" if ignore_refusal else "done")
            self.assertLessEqual(result.cost, Decimal("0.05"))
            if not ignore_refusal:
                self.assertIn("Three sources", result.text)
