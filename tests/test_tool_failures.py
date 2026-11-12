"""A Work tool that keeps failing the same way is told to change approach, then stopped."""

import json
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from services.orchestrator.code_mode import (
    WORK_FAILURE,
    code_profile,
    open_failures,
    work_failure_category,
    work_failure_reply,
)
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import (
    Call,
    CodeResult,
    Limits,
    Message,
    Query,
    Reservation,
    Result,
    Turn,
    run,
)
from services.orchestrator.tool_failures import (
    NOTE_KEY,
    SAME_FAILURE_NUDGE,
    SAME_FAILURE_STOP,
    STOP_REASON,
    RepeatedFailures,
    error_code,
    relayed_failure_tools,
)

FORMULA = (
    "Document rendering or requested-content/source verification failed: "
    "formula_requires_numeric_cells (formulas support numbers, text)"
)
SHAPE = "{} validation errors for Content\nsections.0.table\n  Extra inputs are not permitted"
DONE: Message = {"deliverable": {"id": "file"}}


class ErrorCodeTests(unittest.TestCase):
    def test_same_kind_has_one_code_and_two_kinds_differ(self) -> None:
        self.assertIsNone(error_code({"exit_code": 1, "output": "3 failed"}))
        self.assertIsNone(error_code({"error": ""}))
        self.assertEqual(error_code({"error": "timeout"}), "timeout")
        # The count of a validation report varies; its kind does not.
        self.assertEqual(
            error_code({"error": SHAPE.format(3)}),
            error_code({"error": SHAPE.format(8).replace("table", "content")}),
        )
        self.assertNotEqual(
            error_code({"error": FORMULA}),
            error_code(
                {"error": FORMULA.replace("requires_numeric_cells", "range_limit")}
            ),
        )
        self.assertNotEqual(
            error_code({"error": "old_text not found in notes/alpha.py"}),
            error_code({"error": "old_text not found in notes/beta.py"}),
        )
        # A number inside a name is part of the name; a measured value is not.
        self.assertNotEqual(
            error_code({"error": "old_text not found in notes/file_3.py"}),
            error_code({"error": "old_text not found in notes/file_7.py"}),
        )
        self.assertEqual(
            error_code({"error": "syntax error at line 12 after 0.5 seconds"}),
            error_code({"error": "syntax error at line 150 after 2 seconds"}),
        )
        self.assertEqual(
            error_code({"error": {"code": "path_conflict", "status": 409}}),
            error_code({"error": {"status": 409, "code": "path_conflict"}}),
        )


class RepeatedFailureTests(unittest.TestCase):
    def test_thresholds_are_ordered_named_constants(self) -> None:
        self.assertEqual((SAME_FAILURE_NUDGE, SAME_FAILURE_STOP), (3, 5))

    def test_same_failure_is_noted_then_stops(self) -> None:
        failures = RepeatedFailures()
        seen = [
            failures.record("publish_document", {"error": FORMULA})
            for _ in range(SAME_FAILURE_STOP)
        ]
        self.assertEqual(
            [NOTE_KEY in output for output, _ in seen],
            [False, False, True, True, True],
        )
        self.assertEqual([stop for _, stop in seen], [0, 0, 0, 0, SAME_FAILURE_STOP])
        note = str(seen[2][0][NOTE_KEY])
        self.assertIn("publish_document has now failed 3 times", note)
        self.assertIn("change approach", note)
        self.assertIn(f"At {SAME_FAILURE_STOP} such failures the turn stops", note)
        # The error itself is still what the model reads first.
        self.assertEqual(seen[2][0]["error"], FORMULA)

    def test_a_different_error_each_time_is_never_cut(self) -> None:
        failures = RepeatedFailures()
        for number in range(12):
            output, stop = failures.record(
                "edit_file", {"error": f"old_text not found in notes/file_{number}.py"}
            )
            self.assertNotIn(NOTE_KEY, output)
            self.assertEqual(stop, 0)

    def test_a_success_of_the_tool_clears_its_counts(self) -> None:
        failures = RepeatedFailures()
        for _ in range(10):
            for _ in range(SAME_FAILURE_NUDGE - 1):
                output, stop = failures.record("publish_document", {"error": FORMULA})
                self.assertNotIn(NOTE_KEY, output)
            self.assertEqual(failures.record("publish_document", DONE), (DONE, 0))
        # The success of another tool clears nothing.
        failures.record("publish_document", {"error": FORMULA})
        failures.record("publish_document", {"error": FORMULA})
        failures.record("run_command", {"exit_code": 0})
        output, stop = failures.record("publish_document", {"error": FORMULA})
        self.assertIn(NOTE_KEY, output)

    def test_counts_are_per_tool(self) -> None:
        failures = RepeatedFailures()
        for tool in ("publish_document", "edit_file", "write_file") * 2:
            output, stop = failures.record(tool, {"error": "invalid_tool_arguments"})
            self.assertNotIn(NOTE_KEY, output)

    def test_alternating_refusals_of_one_tool_still_stop(self) -> None:
        # The order of the refused publications of the staging turn of
        # 2026-11-12: two reasons alternate, so neither is ever five in a row.
        failures = RepeatedFailures()
        stops = [
            failures.record(
                "publish_document",
                {"error": FORMULA if kind == "F" else SHAPE.format(3)},
            )[1]
            for kind in "FFFVVFVF"
        ]
        self.assertEqual(stops, [0] * 7 + [SAME_FAILURE_STOP])

    def test_a_failed_read_or_fetch_is_noted_but_never_stops_the_turn(self) -> None:
        self.assertLessEqual(
            {"read_file", "web_search", "web_fetch"}, relayed_failure_tools()
        )
        for tool in ("web_fetch", "read_file", "machine_run"):
            if tool not in relayed_failure_tools():
                continue
            failures = RepeatedFailures()
            for number in range(1, 9):
                output, stop = failures.record(tool, {"error": "timeout"})
                self.assertEqual(stop, 0)
                self.assertEqual(NOTE_KEY in output, number >= SAME_FAILURE_NUDGE)
            self.assertNotIn("the turn stops", str(output[NOTE_KEY]))

    def test_a_machine_limit_keeps_its_own_explanation(self) -> None:
        failures = RepeatedFailures()
        for _ in range(SAME_FAILURE_STOP + 2):
            output, stop = failures.record(
                "remote_run", {"error": "runner_concurrency_limit"}
            )
            self.assertEqual(stop, 0)
        self.assertIn(NOTE_KEY, output)
        self.assertEqual(
            work_failure_category("runner_concurrency_limit", tool_failure=True),
            "machine_busy",
        )
        # Any other refusal of the same tool stops like the others.
        stops = [
            failures.record("remote_run", {"error": "unsupported_shell_syntax"})[1]
            for _ in range(SAME_FAILURE_STOP)
        ]
        self.assertEqual(stops[-1], SAME_FAILURE_STOP)

    def test_open_failures_uses_the_same_relayed_tools(self) -> None:
        self.assertEqual(
            open_failures(
                {"web_fetch": "timeout", "read_file": "missing", "edit_file": "x"}, []
            ),
            {"edit_file": "x"},
        )

    def test_a_refused_document_read_is_a_result_but_still_stops_when_repeated(
        self,
    ) -> None:
        # The turn that then publishes its file is not a failed turn.
        refused = {"read_document": "unknown_document_input: file_id and version"}
        self.assertEqual(open_failures(refused, [{"id": "file"}]), {})
        self.assertEqual(open_failures(refused, []), {})
        # A refused publication or conversion is still open.
        for tool in ("publish_document", "convert_document"):
            self.assertEqual(open_failures({tool: "x"}, []), {tool: "x"})
        failures = RepeatedFailures()
        stops = [
            failures.record("read_document", {"error": "not_found"})[1]
            for _ in range(SAME_FAILURE_STOP)
        ]
        self.assertEqual(stops, [0] * (SAME_FAILURE_STOP - 1) + [SAME_FAILURE_STOP])


class Model:
    """Publishes a slightly different document each time, as a model does."""

    def __init__(self, script: list[str]) -> None:
        self.script, self.calls = script, 0
        self.seen: list[list[Message]] = []

    def estimate(self, messages: list[Message]) -> Reservation:
        return Reservation(1, Decimal(0))

    async def complete(self, messages: list[Message], timeout: float) -> Turn:
        self.seen.append(list(messages))
        if self.calls >= len(self.script):
            return Turn("Here is what could be done.", ())
        name = self.script[self.calls]
        self.calls += 1
        return Turn(
            "",
            (Call(str(self.calls), name, json.dumps({"attempt": self.calls})),),
        )


class Tools:
    def __init__(self, outputs: list[Message]) -> None:
        self.outputs, self.calls = outputs, 0

    def estimate(self, call: Call) -> Reservation:
        return Reservation(0, Decimal(0))

    async def execute(self, call: Call, timeout: float) -> Message:
        self.calls += 1
        return dict(self.outputs[self.calls - 1])


async def work_turn(model: Model, tools: Tools, *, code_mode: bool = True) -> Result:
    with patch.dict(os.environ, {"ATLAS_PROJECT_MODE": "1"}):
        limits = (
            Limits(
                profile=code_profile(),
                tokens=None,
                code_mode=True,
                tool_calls=40,
                model_calls=40,
                cost=Decimal(1),
            )
            if code_mode
            else Limits()
        )
        return await run(
            Query(question="Add a column for February.", lang="en"),
            model,
            tools,
            "system",
            limits,
        )


def tool_messages(messages: list[Message]) -> list[dict[str, object]]:
    return [
        json.loads(str(message["content"]))
        for message in messages
        if message.get("role") == "tool"
    ]


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_refusal_with_changing_arguments_stops_the_turn(self) -> None:
        model = Model(["publish_document"] * 17)
        tools = Tools([{"error": FORMULA}] * 17)
        result = await work_turn(model, tools)
        assert isinstance(result, CodeResult)
        self.assertEqual((result.state, result.reason), ("stopped", STOP_REASON))
        # Five publications ran, not seventeen.
        self.assertEqual(tools.calls, SAME_FAILURE_STOP)
        self.assertEqual(model.calls, SAME_FAILURE_STOP)
        self.assertEqual(
            result.limit_stop,
            {
                "kind": STOP_REASON,
                "measured": SAME_FAILURE_STOP,
                "limit": SAME_FAILURE_STOP,
                "unit": "calls",
            },
        )
        # The model read the note from the third refusal on.
        read = tool_messages(model.seen[-1])
        self.assertEqual(
            [NOTE_KEY in output for output in read], [False, False, True, True]
        )
        self.assertEqual(
            [NOTE_KEY in event["output"] for event in result.trace],  # type: ignore[operator]
            [False, False, True, True, True],
        )

    async def test_note_lets_the_model_change_approach_and_finish(self) -> None:
        model = Model(["publish_document"] * 4)
        refused: Message = {"error": FORMULA}
        tools = Tools([refused] * 3 + [DONE])
        result = await work_turn(model, tools)
        self.assertEqual((result.state, result.reason), ("done", ""))
        self.assertEqual(result.text, "Here is what could be done.")

    async def test_retries_with_progress_or_a_success_between_are_not_cut(
        self,
    ) -> None:
        outputs: list[Message] = []
        for _ in range(6):
            outputs += [{"error": FORMULA}, {"error": FORMULA}, DONE]
        outputs += [{"error": "reason_" + letter} for letter in "abcdefgh"]
        model = Model(["publish_document"] * len(outputs))
        tools = Tools(outputs)
        result = await work_turn(model, tools)
        self.assertEqual((result.state, result.reason), ("done", ""))
        self.assertEqual(tools.calls, len(outputs))
        self.assertFalse(
            any(NOTE_KEY in output for output in tool_messages(model.seen[-1]))
        )

    async def test_failed_fetches_are_noted_and_the_turn_goes_on(self) -> None:
        model = Model(["read_file"] * 7)
        tools = Tools([{"error": "not_found"}] * 7)
        result = await work_turn(model, tools)
        self.assertEqual((result.state, result.reason), ("done", ""))
        self.assertEqual(tools.calls, 7)
        self.assertIn(NOTE_KEY, tool_messages(model.seen[-1])[-1])

    async def test_the_chat_loop_is_unchanged(self) -> None:
        model = Model(["calculator"] * 7)
        tools = Tools([{"error": "invalid_expression"}] * 7)
        result = await work_turn(model, tools, code_mode=False)
        self.assertEqual((result.state, result.reason), ("done", ""))
        self.assertFalse(
            any(NOTE_KEY in output for output in tool_messages(model.seen[-1]))
        )


class ExplanationTests(unittest.TestCase):
    def test_stopped_turn_is_explained_in_every_language_with_what_was_produced(
        self,
    ) -> None:
        self.assertEqual(
            work_failure_category(STOP_REASON, tool_failure=False), "repeated_failure"
        )
        link = "[Download budget.xlsx](/api/atlas/orgs/o/projects/p/deliverables/d/download)"
        for lang, words in WORK_FAILURE.items():
            with self.subTest(lang=lang):
                item = Interaction()
                item.state = "failed"
                item.work_failure = dict(category="repeated_failure", answer=link)
                reply = work_failure_reply(item, lang)
                assert reply is not None
                self.assertTrue(reply.startswith(link))
                self.assertTrue(
                    reply.endswith(
                        words["template"].format(reason=words["repeated_failure"])
                    )
                )
        self.assertEqual(
            {frozenset(words) for words in WORK_FAILURE.values()},
            {frozenset(WORK_FAILURE["en"])},
        )


class ChatBehindWorkTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_stopped_turn_is_explained_and_the_chat_does_not_answer_alone(
        self,
    ) -> None:
        # The tool-less chat answering after the stop would spend again and
        # could claim a file that does not exist.
        from unittest.mock import AsyncMock

        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.chat_work import escalate_to_work

        req = ChatRequest.model_validate(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Fais-moi un Excel de suivi de dépenses",
                    }
                ],
                "ui_locale": "fr",
            }
        )
        req.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174030"
        req.__dict__["artifact_owner"] = "user@example.com"
        item = Interaction()
        link = "[budget.xlsx](/api/atlas/orgs/o/projects/p/deliverables/d/download)"

        async def stopped_work(r, i, **kwargs):  # type: ignore[no-untyped-def]
            i.state, i.reponse = "failed", "Work failed: " + STOP_REASON
            i.erreurs.append(STOP_REASON)
            i.work_failure = {
                "category": work_failure_category(STOP_REASON, tool_failure=False),
                "answer": link,
            }

        with (
            patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
            patch("services.orchestrator.chat_work.work_gate_open", return_value=True),
            patch(
                "services.orchestrator.chat_work._identity",
                new=AsyncMock(
                    return_value={
                        "memberships": [
                            {"org_id": "123e4567-e89b-12d3-a456-426614174000"}
                        ]
                    }
                ),
            ),
            patch(
                "services.orchestrator.chat_work._ensure_scratch_project",
                new=AsyncMock(return_value="123e4567-e89b-12d3-a456-426614174002"),
            ),
            patch(
                "services.orchestrator.chat_work._find_binding",
                new=AsyncMock(
                    return_value={
                        "conversation_id": "123e4567-e89b-12d3-a456-426614174003",
                        "work_session_id": "123e4567-e89b-12d3-a456-426614174004",
                    }
                ),
            ),
            patch("services.orchestrator.code_mode.process_work", new=stopped_work),
        ):
            self.assertTrue(await escalate_to_work(req, item))
        self.assertEqual((item.state, item.task_type), ("done", "work"))
        words = WORK_FAILURE["fr"]
        self.assertEqual(
            item.reponse,
            link + "\n\n" + words["template"].format(reason=words["repeated_failure"]),
        )
        self.assertNotIn("Work failed", item.reponse)


if __name__ == "__main__":
    unittest.main()
