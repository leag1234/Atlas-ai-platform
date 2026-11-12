"""A Work turn asked to run code runs it, or says it did not (staging 2026-11-12)."""

import asyncio
import json
import unittest
from decimal import Decimal
from typing import Any, cast
from unittest import mock
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from services.orchestrator import work_opencode_turn as turn_module
from services.orchestrator.loop import Call, Limits, Query, Reservation, Turn, run
from services.orchestrator.work_execution import (
    NOT_EXECUTED,
    ExecutionGuard,
    run_requested,
)
from services.orchestrator.work_opencode import OpencodeDriver
from services.orchestrator.work_opencode_turn import run_opencode_turn


def _limits() -> Limits:
    return Limits(
        profile="atlas-qwen",
        tokens=None,
        tool_calls=4,
        wall_clock=30,
        cost=Decimal("1"),
        model_calls=4,
        code_mode=True,
    )


class GuardTests(unittest.TestCase):
    def test_a_run_request_is_sent_back_once_then_answered_honestly(self) -> None:
        guard = ExecutionGuard(True)
        step = guard.next_step("run_command", ["read_file"])
        self.assertIsNotNone(step)
        self.assertIn("run_command", str(step))
        self.assertIn("real output", str(step))
        self.assertIsNone(guard.next_step("run_command", ["read_file"]))
        text, changed = guard.answer("The output is 42.", "fr")
        self.assertTrue(changed)
        self.assertTrue(text.startswith(NOT_EXECUTED["fr"]))
        self.assertIn("The output is 42.", text)

    def test_an_executed_run_keeps_its_answer(self) -> None:
        guard = ExecutionGuard(True)
        self.assertIsNone(guard.next_step("the bash tool", ["bash"]))
        self.assertEqual(guard.answer("It prints 7.", "en"), ("It prints 7.", False))
        guard = ExecutionGuard(True)
        guard.record(["remote_run"])
        self.assertFalse(guard.unexecuted)

    def test_other_requests_are_never_held(self) -> None:
        guard = ExecutionGuard(False)
        self.assertIsNone(guard.next_step("run_command"))
        self.assertEqual(guard.answer("A summary.", "en"), ("A summary.", False))

    def test_unknown_language_gets_the_english_notice(self) -> None:
        guard = ExecutionGuard(True)
        text, _ = guard.answer("Done.", "pt")
        self.assertTrue(text.startswith(NOT_EXECUTED["en"]))

    def test_run_requested_follows_the_routed_decision_then_the_text(self) -> None:
        self.assertTrue(run_requested("run", "anything"))
        self.assertFalse(run_requested("file", "run this script and show the output"))
        self.assertTrue(
            run_requested(
                None, "please execute this script and show me what it prints: x = 3"
            )
        )
        self.assertFalse(run_requested(None, "explain what a closure is"))


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_answer_without_execution_goes_back_to_the_model(self) -> None:
        turns = [
            Turn("It prints 6."),
            Turn("", (Call("c1", "run_command", '{"command": "x"}'),)),
            Turn("I ran it: it prints 6."),
        ]
        seen: list[list[dict[str, Any]]] = []

        async def complete(messages: list[dict[str, Any]], timeout: float) -> Turn:
            seen.append([dict(m) for m in messages])
            return turns[len(seen) - 1]

        model = Mock(
            estimate=Mock(return_value=Reservation(100, Decimal("0.001"))),
            complete=AsyncMock(side_effect=complete),
        )
        tools = Mock(
            estimate=Mock(return_value=Reservation(0, Decimal(0))),
            execute=AsyncMock(return_value={"stdout": "6\n", "exit_code": 0}),
        )
        guard = ExecutionGuard(True)
        with patch("services.orchestrator.code_workspace.enabled", return_value=True):
            result = await run(
                Query(question="run this and show the output", lang="en"),
                model,
                tools,
                "",
                _limits(),
                before_final=lambda executed: guard.next_step("run_command", executed),
            )
        self.assertEqual(result.state, "done")
        self.assertEqual(result.text, "I ran it: it prints 6.")
        self.assertFalse(guard.unexecuted)
        second = seen[1]
        self.assertEqual(second[-2], {"role": "assistant", "content": "It prints 6."})
        self.assertEqual(second[-1]["role"], "user")
        self.assertIn("run_command", second[-1]["content"])

    async def test_a_second_answer_without_execution_is_accepted_once(self) -> None:
        model = Mock(
            estimate=Mock(return_value=Reservation(100, Decimal("0.001"))),
            complete=AsyncMock(side_effect=[Turn("It prints 6."), Turn("It is 6.")]),
        )
        tools = Mock(estimate=Mock(return_value=Reservation(0, Decimal(0))))
        guard = ExecutionGuard(True)
        with patch("services.orchestrator.code_workspace.enabled", return_value=True):
            result = await run(
                Query(question="run this and show the output", lang="en"),
                model,
                tools,
                "",
                _limits(),
                before_final=lambda executed: guard.next_step("run_command", executed),
            )
        self.assertEqual(result.text, "It is 6.")
        self.assertEqual(model.complete.await_count, 2)
        self.assertTrue(guard.unexecuted)

    async def test_without_a_guard_the_first_answer_ends_the_turn(self) -> None:
        model = Mock(
            estimate=Mock(return_value=Reservation(100, Decimal("0.001"))),
            complete=AsyncMock(return_value=Turn("A plain answer.")),
        )
        tools = Mock(estimate=Mock(return_value=Reservation(0, Decimal(0))))
        with patch("services.orchestrator.code_workspace.enabled", return_value=True):
            result = await run(
                Query(question="summarize the notes", lang="en"),
                model,
                tools,
                "",
                _limits(),
            )
        self.assertEqual(result.text, "A plain answer.")
        self.assertEqual(model.complete.await_count, 1)


def _event(payload: dict[str, Any]) -> bytes:
    return ("data: " + json.dumps(payload) + "\n\n").encode()


def _text(text: str) -> dict[str, Any]:
    return {
        "type": "message.part.updated",
        "properties": {"sessionID": "sess-1", "part": {"type": "text", "text": text}},
    }


def _bash(status: str) -> dict[str, Any]:
    return {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "sess-1",
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {"status": status, "input": {"command": "python3 a.py"}},
            },
        },
    }


IDLE = {"type": "session.idle", "properties": {"sessionID": "sess-1"}}


class TwoRoundOpencode:
    """A fake engine: one round of events per prompt received."""

    def __init__(self, rounds: list[list[dict[str, Any]]]) -> None:
        self.rounds = rounds
        self.prompts: list[str] = []
        self.received = asyncio.Queue[int]()

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/session", self.create)
        app.router.add_post("/session/{sid}/prompt_async", self.prompt)
        app.router.add_get("/event", self.stream)
        return app

    async def create(self, request: web.Request) -> web.Response:
        return web.json_response({"id": "sess-1"})

    async def prompt(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.prompts.append(body["parts"][0]["text"])
        await self.received.put(len(self.prompts) - 1)
        return web.json_response({})

    async def stream(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for _ in self.rounds:
            index = await self.received.get()
            # The engine echoes the prompt as a text part before answering.
            await response.write(_event(_text(self.prompts[index])))
            for payload in self.rounds[index]:
                await response.write(_event(payload))
        await response.write_eof()
        return response


class DriverFollowUpTests(unittest.IsolatedAsyncioTestCase):
    async def _drive(
        self, fake: TwoRoundOpencode, follow_up: Any
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        emitted: list[dict[str, Any]] = []

        async def emit(event: dict[str, Any]) -> None:
            emitted.append(event)

        async with TestServer(fake.app()) as server:
            driver = OpencodeDriver(
                str(server.make_url("/")), "pw", emit, follow_up=follow_up
            )
            result = await asyncio.wait_for(driver.run("run it"), 10)
        return result, emitted

    async def test_idle_with_a_follow_up_prompts_the_same_session(self) -> None:
        fake = TwoRoundOpencode(
            [
                [_text("It prints 6."), IDLE],
                [_bash("running"), _bash("completed"), _text("Ran: 6."), IDLE],
            ]
        )
        guard = ExecutionGuard(True)
        result, emitted = await self._drive(
            fake, lambda: guard.next_step("the bash tool")
        )
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(len(fake.prompts), 2)
        self.assertIn("the bash tool", fake.prompts[1])
        texts = [e["text"] for e in emitted if e["kind"] == "text"]
        self.assertEqual(texts, ["It prints 6.", "Ran: 6."])

    async def test_without_follow_up_the_first_idle_ends_the_turn(self) -> None:
        fake = TwoRoundOpencode([[_text("Done."), IDLE]])
        result, emitted = await self._drive(fake, None)
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(len(fake.prompts), 1)
        self.assertEqual([e["text"] for e in emitted if e["kind"] == "text"], ["Done."])


class _Tools:
    def __init__(self, required: bool) -> None:
        self.events: list[dict[str, Any]] = []
        self.run_guard = ExecutionGuard(required)

    async def emit(self, kind: str, **data: Any) -> None:
        self.events.append(dict(kind=kind, **data))


class _Request:
    user_text = "run this and tell me the output: print(2 + 4)"
    lang = "en"


class _Limits:
    cost = Decimal("3")
    wall_clock = 1200.0


class _Engine:
    url = "http://engine:4096/api/atlas/v1"
    password = "pw"
    name = "atlas-opencode-test"


class EngineTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(turn_module, "emit_changes", mock.AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _turn(
        self, tools: _Tools, events: list[dict[str, Any]]
    ) -> tuple[Any, mock.Mock]:
        driver_cls = mock.Mock()

        async def driver_run(prompt: str) -> dict[str, Any]:
            emit = driver_cls.call_args.args[2]
            follow_up = driver_cls.call_args.kwargs["follow_up"]
            for event in events:
                await emit(event)
            follow_up()
            return {"outcome": "complete", "cost": 0.0, "events": events}

        driver_cls.return_value.run = mock.AsyncMock(side_effect=driver_run)
        with (
            mock.patch.object(
                turn_module, "start", mock.AsyncMock(return_value=_Engine())
            ),
            mock.patch.object(turn_module, "stop", mock.AsyncMock()),
            mock.patch.object(turn_module, "OpencodeDriver", driver_cls),
        ):
            result = await run_opencode_turn(
                cast(Any, _Request()),
                cast(Any, mock.Mock()),
                cast(Any, tools),
                cast(Any, mock.Mock()),
                cast(Any, _Limits()),
                False,
            )
        return result, driver_cls

    async def test_an_unexecuted_run_says_so(self) -> None:
        tools = _Tools(True)
        result, _ = await self._turn(tools, [{"kind": "text", "text": "It is 6."}])
        self.assertTrue(result.text.startswith(NOT_EXECUTED["en"]))
        self.assertIn("It is 6.", result.text)
        self.assertEqual(result.state, "done")
        self.assertEqual(
            [e["label"] for e in tools.events if e["kind"] == "honesty"],
            ["Run not executed"],
        )

    async def test_a_bash_run_keeps_the_answer(self) -> None:
        tools = _Tools(True)
        result, _ = await self._turn(
            tools,
            [
                {"kind": "tool", "tool": "bash", "status": "running", "input": "x"},
                {"kind": "tool", "tool": "bash", "status": "completed", "input": "x"},
                {"kind": "text", "text": "It prints 6."},
            ],
        )
        self.assertEqual(result.text, "It prints 6.")
        self.assertEqual([e for e in tools.events if e["kind"] == "honesty"], [])

    async def test_a_request_that_is_not_a_run_is_untouched(self) -> None:
        tools = _Tools(False)
        result, _ = await self._turn(tools, [{"kind": "text", "text": "Fixed."}])
        self.assertEqual(result.text, "Fixed.")


if __name__ == "__main__":
    unittest.main()
