"""Tests for the opencode Work engine turn wiring (step 1d)."""

import os
import unittest
from decimal import Decimal
from typing import Any, cast
from unittest import mock

from services.orchestrator import work_opencode_turn as turn_module
from services.orchestrator.work_opencode_turn import (
    OpencodeTurnResult,
    engine_turn_selected,
    run_opencode_turn,
    token_prices,
)


class _Selection:
    def __init__(self, group: str) -> None:
        self.group = group
        self.research_enabled = True


class _Tools:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.deliverables: list[dict[str, Any]] = []

    async def emit(self, kind: str, **data: Any) -> None:
        self.events.append(dict(kind=kind, **data))


class _Request:
    user_text = "fix the failing test"


class _Item:
    def __init__(self) -> None:
        self.reponse = ""
        self.state = ""
        self.cout_eur = 0.0
        self.tokens: dict[str, int] = {}
        self.erreurs: list[str] = []


class _Limits:
    cost = Decimal("3")
    wall_clock = 1200.0


class _Engine:
    url = "http://engine:4096/api/atlas/v1"
    password = "pw"
    name = "atlas-opencode-test"


def _patched(
    outcome: dict[str, Any] | None = None, run_side_effect: Any = None
) -> tuple[Any, Any, Any, mock.Mock]:
    driver = mock.Mock()
    if run_side_effect is not None:
        driver.run = mock.AsyncMock(side_effect=run_side_effect)
    else:
        driver.run = mock.AsyncMock(return_value=outcome)
    return (
        mock.patch.object(turn_module, "start", mock.AsyncMock(return_value=_Engine())),
        mock.patch.object(turn_module, "stop", mock.AsyncMock()),
        mock.patch.object(
            turn_module, "OpencodeDriver", mock.Mock(return_value=driver)
        ),
        driver,
    )


class EngineTurnSelectedTests(unittest.TestCase):
    def test_selected_only_for_code_group_when_setting_on(self) -> None:
        with mock.patch.dict(os.environ, {"ATLAS_WORK_ENGINE": "opencode"}):
            self.assertTrue(engine_turn_selected(_Selection("code")))
            self.assertFalse(engine_turn_selected(_Selection("documents")))
            self.assertFalse(engine_turn_selected(None))

    def test_not_selected_when_setting_off(self) -> None:
        env = dict(os.environ)
        env.pop("ATLAS_WORK_ENGINE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(engine_turn_selected(_Selection("code")))


class TokenPricesTests(unittest.TestCase):
    def test_default_and_custom(self) -> None:
        env = dict(os.environ)
        env.pop("ATLAS_WORK_ENGINE_PRICES", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(token_prices(), (Decimal("0.5"), Decimal("1.5")))
        with mock.patch.dict(os.environ, {"ATLAS_WORK_ENGINE_PRICES": "0.1, 2.5"}):
            self.assertEqual(token_prices(), (Decimal("0.1"), Decimal("2.5")))


class RunOpencodeTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_mapping_and_dedupe(self) -> None:
        events = [
            {"kind": "text", "text": "partial"},
            {"kind": "text", "text": "final answer"},
            {"kind": "tool", "tool": "bash", "status": "running", "input": "ls"},
            {"kind": "tool", "tool": "bash", "status": "running", "input": "ls"},
            {
                "kind": "tool",
                "tool": "bash",
                "status": "completed",
                "input": "ls",
                "output": "ok",
            },
            {
                "kind": "tool",
                "tool": "bash",
                "status": "completed",
                "input": "ls",
                "output": "ok",
            },
            {
                "kind": "tool",
                "tool": "read",
                "status": "error",
                "input": "f",
                "output": "boom",
            },
        ]
        outcome = {
            "outcome": "complete",
            "cost": 0.01,
            "input_tokens": 10,
            "output_tokens": 5,
            "events": events,
        }
        tools = _Tools()
        start_mock, stop_mock, driver_cls, driver = _patched()

        async def run(prompt: str) -> dict[str, Any]:
            self.assertEqual(prompt, "fix the failing test")
            emit = driver_cls_await.call_args.args[2]
            for event in events:
                await emit(event)
            return outcome

        driver.run = mock.AsyncMock(side_effect=run)
        with start_mock, stop_mock as stop_await, driver_cls as driver_cls_await:
            result = await run_opencode_turn(
                cast(Any, _Request()),
                cast(Any, _Item()),
                cast(Any, tools),
                cast(Any, mock.Mock()),
                cast(Any, _Limits()),
                True,
            )
        statuses = [
            (e["tool"], e["status"]) for e in tools.events if e["kind"] == "tool"
        ]
        self.assertEqual(
            statuses,
            [("bash", "running"), ("bash", "completed"), ("read", "failed")],
        )
        self.assertEqual(result.text, "final answer")
        self.assertEqual(result.state, "done")
        self.assertEqual(result.reason, "")
        self.assertEqual(result.cost_eur, 0.01)
        self.assertEqual(result.input_tokens, 10)
        self.assertEqual(result.output_tokens, 5)
        self.assertEqual(result.tool_calls, 2)
        stop_await.assert_awaited_once_with("atlas-opencode-test")

    async def test_outcome_mapping(self) -> None:
        for outcome_name, state, reason in [
            ("complete", "done", ""),
            ("aborted_budget", "failed", "cost_limit"),
            ("aborted_time", "failed", "time_limit"),
            ("error", "failed", "engine_error"),
        ]:
            with self.subTest(outcome=outcome_name):
                tools = _Tools()
                start_mock, stop_mock, driver_cls, _ = _patched(
                    {
                        "outcome": outcome_name,
                        "cost": 0.0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "events": [],
                    }
                )
                with start_mock, stop_mock as stop_await, driver_cls:
                    result = await run_opencode_turn(
                        cast(Any, _Request()),
                        cast(Any, _Item()),
                        cast(Any, tools),
                        cast(Any, mock.Mock()),
                        cast(Any, _Limits()),
                        True,
                    )
                self.assertIsInstance(result, OpencodeTurnResult)
                self.assertEqual(result.state, state)
                self.assertEqual(result.reason, reason)
                stop_await.assert_awaited_once()

    async def test_stop_called_on_error(self) -> None:
        tools = _Tools()
        start_mock, stop_mock, driver_cls, _ = _patched(
            run_side_effect=RuntimeError("boom")
        )
        with start_mock, stop_mock as stop_await, driver_cls:
            with self.assertRaises(RuntimeError):
                await run_opencode_turn(
                    cast(Any, _Request()),
                    cast(Any, _Item()),
                    cast(Any, tools),
                    cast(Any, mock.Mock()),
                    cast(Any, _Limits()),
                    True,
                )
        stop_await.assert_awaited_once_with("atlas-opencode-test")


if __name__ == "__main__":
    unittest.main()
