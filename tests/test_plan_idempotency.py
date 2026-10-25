"""Captured repeated Work plans must not consume execution steps or loop-stop."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import AsyncMock

from services.orchestrator.loop import Call, Limits, Query, Reservation, Turn, run


class PlanIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_captured_sequence_acknowledges_then_acts(self) -> None:
        rows = json.loads(Path("tests/fixtures/session-plan-loop.json").read_text())[
            "responses"
        ]
        turns = [
            Turn(
                row["text"],
                tuple(Call(**c) for c in row["calls"]),
                Reservation(1, Decimal(0)),
            )
            for row in rows
        ]
        turns.append(
            Turn(
                "The four-step plan is retained; shipment is restricted to France or Germany.",
                usage=Reservation(1, Decimal(0)),
            )
        )
        messages: list[Any] = []

        async def complete(value: Any, timeout: float) -> Turn:
            messages.append(deepcopy(value))
            return turns.pop(0)

        model = AsyncMock()
        model.estimate = lambda _: Reservation(1, Decimal(0))
        model.complete.side_effect = complete
        tools = AsyncMock()
        tools.estimate = lambda _: Reservation(0, Decimal(0))
        tools.execute.return_value = {"items": []}
        result = await run(
            Query(question="Retain the plan", lang="en"),
            model,
            tools,
            "",
            Limits(tool_calls=3),
        )
        self.assertEqual(result.state, "done")
        self.assertIn("France or Germany", result.text)
        self.assertEqual(result.tool_calls, 3)
        self.assertEqual(tools.execute.await_count, 3)
        outputs = [
            json.loads(str(m["content"])) for m in messages[-1] if m["role"] == "tool"
        ]
        repeated = [o for o in outputs if o.get("unchanged")]
        self.assertEqual(len(repeated), 3)
        self.assertIn("Act on the plan", repeated[-1]["next_step"])

    async def test_invalid_plan_is_not_cached_and_other_loops_still_stop(self) -> None:
        for name, output in (
            ("update_plan", {"error": "invalid_tool_arguments"}),
            ("list_dir", {"items": []}),
        ):
            with self.subTest(name=name):
                model = AsyncMock()
                model.estimate = lambda _: Reservation(1, Decimal(0))
                model.complete.return_value = Turn(
                    "", (Call("id", name, "{}"),), Reservation(1, Decimal(0))
                )
                tools = AsyncMock()
                tools.estimate = lambda _: Reservation(0, Decimal(0))
                tools.execute.return_value = output
                result = await run(
                    Query(question="Continue", lang="en"), model, tools, ""
                )
                self.assertEqual(result.reason, "loop_detected")
                self.assertEqual(tools.execute.await_count, 2)
