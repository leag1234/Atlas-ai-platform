"""Streaming keeps the existing pre-I/O reservations, including failed streams."""

from decimal import Decimal
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator.loop import Limits, Query, run
from services.orchestrator.model import Configuration, GatewayModel
from test_budgets import Tools


class StreamBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_recovery_stays_non_reasoning_after_tool_turn(self) -> None:
        model = self.model()
        model.configure_quality("atlas-glm", 3000)
        first = {
            "text": "",
            "calls": [
                {"id": "calc", "name": "calculator", "arguments": '{"expr":"8/2"}'}
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 100},
            "cost_eur": "0.001",
            "reasoning_retried": True,
        }
        second = {
            "text": "The result is 4.",
            "calls": [],
            "usage": {"prompt_tokens": 30, "completion_tokens": 10},
            "cost_eur": "0.001",
        }
        messages: list[dict[str, object]] = [
            {"role": "user", "content": "Calculate this quantity."}
        ]
        with patch("services.orchestrator.model.receive", new_callable=AsyncMock) as io:
            io.side_effect = [first, second]
            await model.complete(messages, 100)
            answer = await model.complete(
                [
                    *messages,
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "calc",
                                "type": "function",
                                "function": {
                                    "name": "calculator",
                                    "arguments": '{"expr":"8/2"}',
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "calc", "content": "4"},
                ],
                40,
            )
        self.assertEqual(answer.text, "The result is 4.")
        self.assertEqual(io.call_args_list[0].args[1]["reasoning_effort"], "none")
        recovered = io.call_args_list[1].args[1]
        self.assertEqual(recovered["reasoning_effort"], "none")
        self.assertEqual(recovered["max_tokens"], 3000)
        self.assertEqual(recovered["messages"][0], messages[0])
        self.assertTrue(model.reasoning_retried)

    def model(self) -> GatewayModel:
        model = GatewayModel(
            "http://127.0.0.1:1",
            Configuration(
                input_eur_per_mtok=Decimal("0.6"),
                output_eur_per_mtok=Decimal("3.6"),
                max_tokens=2048,
            ),
        )
        model.sink = AsyncMock()
        return model

    async def test_budget_refuses_before_stream_io(self) -> None:
        with patch("services.orchestrator.model.receive", new_callable=AsyncMock) as io:
            result = await run(
                Query(question="Bonjour", lang="fr"),
                self.model(),
                Tools(),
                "",
                Limits(cost=Decimal("0")),
            )
            self.assertEqual(result.reason, "cost")
            io.assert_not_awaited()

    async def test_missing_usage_keeps_reservation(self) -> None:
        with patch("services.orchestrator.model.receive", new_callable=AsyncMock) as io:
            io.return_value = {"text": "partial", "calls": []}
            result = await run(
                Query(question="Bonjour", lang="fr"), self.model(), Tools(), ""
            )
            self.assertEqual(result.reason, "provider_error")
            self.assertGreater(result.cost, Decimal("0.007"))
            self.assertLessEqual(result.cost, Decimal("0.05"))

    async def test_reasoning_shares_output_and_phase_is_explicit(self) -> None:
        events: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        model = self.model()
        model.sink, model.reasoning_effort = sink, "low"
        with patch("services.orchestrator.model.receive", new_callable=AsyncMock) as io:
            io.return_value = {
                "text": "Bonjour",
                "calls": [],
                "usage": {"prompt_tokens": 20, "completion_tokens": 100},
            }
            result = await run(Query(question="Bonjour", lang="fr"), model, Tools(), "")
            self.assertEqual(result.state, "done")
            self.assertEqual(model.output_tokens, 100)
            self.assertEqual(io.call_args.args[1]["reasoning_effort"], "low")
            self.assertEqual(events[0]["max_output_tokens"], 2048)
            self.assertEqual(events[-1]["phase"], "final")
