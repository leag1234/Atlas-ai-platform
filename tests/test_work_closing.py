"""A Work turn ends shortly once its requested file is published (work_closing.py).

Staging corpus 2026-11-13: after publication the loop made one more model call
that answered the request again, 38 to 60 seconds before the turn ended.
"""

import json
import unittest
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from services.orchestrator.loop import Call, Limits, Query, Reservation, Turn, run
from services.orchestrator.work_closing import (
    CLOSING_INSTRUCTION,
    closing_instruction,
    closing_step,
    published,
)

RECEIPT = {
    "id": "f1",
    "file_version_id": "v1",
    "relative_path": "tarot-rules.pdf",
}
LINKS = "[Download tarot-rules.pdf](/d) · [Preview](/p)"
PUBLISHED = {
    "trust": "untrusted",
    "deliverable": RECEIPT,
    "text": LINKS,
    "file_version_ids": ["v1"],
    "read_input": {"file_id": "f1", "version": "v1"},
}
PUBLISH = Call(
    "c1",
    "publish_document",
    json.dumps(
        {"path": "tarot-rules.pdf", "content": "# Rules\n\nBody.", "sources": []}
    ),
)


def _tools(pending: bool = False) -> Any:
    return Mock(selection=Mock(deliverable_pending=Mock(return_value=pending)))


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


class ClosingStepTests(unittest.TestCase):
    def test_a_published_file_closes_a_file_turn(self) -> None:
        step = closing_step(_tools(), True, [("publish_document", PUBLISHED)])
        self.assertIsNotNone(step)
        assert step is not None
        self.assertIn("tarot-rules.pdf", step)
        self.assertIn("do not write them", step)
        self.assertIn("one or two short sentences", step)
        # The rest of a request that asked for more is still done.
        self.assertIn("do that now instead", step)

    def test_no_closing_without_a_publication(self) -> None:
        self.assertIsNone(
            closing_step(_tools(), True, [("web_fetch", {"data": {"text": "x"}})])
        )

    def test_a_refused_publication_does_not_close(self) -> None:
        refused = {"trust": "untrusted", "error": "Invalid deliverable path or format"}
        self.assertIsNone(closing_step(_tools(), True, [("publish_document", refused)]))
        self.assertIsNone(published("publish_document", {"text": LINKS}))

    def test_a_turn_that_asked_for_no_file_is_left_alone(self) -> None:
        self.assertIsNone(
            closing_step(_tools(), False, [("publish_document", PUBLISHED)])
        )

    def test_a_requested_file_still_missing_keeps_the_turn_open(self) -> None:
        self.assertIsNone(
            closing_step(_tools(pending=True), True, [("publish_document", PUBLISHED)])
        )

    def test_several_files_are_named(self) -> None:
        second = dict(RECEIPT, relative_path="tarot-rules.docx")
        text = closing_instruction([RECEIPT, second])
        self.assertIn("tarot-rules.pdf, tarot-rules.docx", text)
        self.assertEqual(
            closing_instruction([{"id": "x"}]),
            CLOSING_INSTRUCTION.format(files="one file"),
        )


class LoopClosingTests(unittest.IsolatedAsyncioTestCase):
    async def turn(
        self, output: dict[str, Any], after_tools: Any
    ) -> tuple[Any, list[list[dict[str, Any]]]]:
        turns = [Turn("", (PUBLISH,)), Turn("Here is the PDF of the tarot rules.")]
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
            execute=AsyncMock(return_value=output),
        )
        with patch("services.orchestrator.code_workspace.enabled", return_value=True):
            result = await run(
                Query(question="I would like a PDF of the tarot rules", lang="en"),
                model,
                tools,
                "",
                _limits(),
                final_only=True,
                after_tools=after_tools,
            )
        return result, seen

    async def test_the_call_after_publication_reads_the_closing_last(self) -> None:
        result, seen = await self.turn(
            PUBLISHED, lambda executed: closing_step(_tools(), True, executed)
        )
        self.assertEqual(result.state, "done")
        self.assertEqual(result.text, "Here is the PDF of the tarot rules.")
        self.assertEqual(len(seen), 2)
        closing = seen[1]
        self.assertEqual(closing[-2]["role"], "tool")
        self.assertEqual(closing[-2]["tool_call_id"], "c1")
        self.assertEqual(closing[-1]["role"], "user")
        self.assertIn("tarot-rules.pdf", closing[-1]["content"])
        self.assertIn("do not write them", closing[-1]["content"])

    async def test_a_refused_publication_reaches_the_model_unchanged(self) -> None:
        refused = {"trust": "untrusted", "error": "document_tool_timeout"}
        _, seen = await self.turn(
            refused, lambda executed: closing_step(_tools(), True, executed)
        )
        self.assertEqual(seen[1][-1]["role"], "tool")
        self.assertNotIn(
            "one or two short sentences", json.dumps(seen[1], ensure_ascii=False)
        )

    async def test_without_the_hook_the_loop_is_unchanged(self) -> None:
        result, seen = await self.turn(PUBLISHED, None)
        self.assertEqual(result.text, "Here is the PDF of the tarot rules.")
        self.assertEqual(seen[1][-1]["role"], "tool")

    async def test_unretrieved_addresses_are_still_removed_from_the_closing(
        self,
    ) -> None:
        turns = [
            Turn("", (PUBLISH,)),
            Turn("Here is the PDF. See https://invented.example/rules for more."),
        ]
        model = Mock(
            estimate=Mock(return_value=Reservation(100, Decimal("0.001"))),
            complete=AsyncMock(side_effect=turns),
        )
        tools = Mock(
            estimate=Mock(return_value=Reservation(0, Decimal(0))),
            execute=AsyncMock(return_value=PUBLISHED),
        )
        with patch("services.orchestrator.code_workspace.enabled", return_value=True):
            result = await run(
                Query(question="I would like a PDF of the tarot rules", lang="en"),
                model,
                tools,
                "",
                _limits(),
                final_only=True,
                after_tools=lambda executed: closing_step(_tools(), True, executed),
            )
        self.assertNotIn("invented.example", result.text)


class ExecuteTurnWiringTests(unittest.IsolatedAsyncioTestCase):
    async def captured(self, produces_files: bool) -> Any:
        from services.orchestrator import code_mode
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.code_tools import WorkTools
        from services.orchestrator.loop import CodeResult

        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "A PDF of the tarot rules"}]}
        )
        tools = WorkTools(Mock(), Mock())
        tools.selection = Mock(deliverable_pending=Mock(return_value=False))
        model = Mock(produces_files=produces_files)
        done = CodeResult()
        done.state, done.text = "done", "Here is the PDF."
        with (
            patch("services.orchestrator.code_workspace.enabled", return_value=True),
            patch.object(code_mode, "run", AsyncMock(return_value=done)) as loop,
        ):
            await code_mode.execute_turn(request, model, tools, _limits())
        assert loop.await_args is not None
        return loop.await_args.kwargs["after_tools"]

    async def test_a_file_turn_gets_the_closing_hook(self) -> None:
        hook = await self.captured(True)
        self.assertIn("tarot-rules.pdf", hook([("publish_document", PUBLISHED)]) or "")

    async def test_a_turn_without_a_requested_file_never_closes(self) -> None:
        hook = await self.captured(False)
        self.assertIsNone(hook([("publish_document", PUBLISHED)]))


if __name__ == "__main__":
    unittest.main()
