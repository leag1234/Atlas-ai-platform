"""A Work turn asked for a file publishes it, or says it did not (work_delivery.py).

Staging battery 2026-11-20: a project Work turn asked for a five-slide PowerPoint
file answered that the presentation "was generated successfully" with the slide
texts, and no deliverable existed.
"""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from services.orchestrator.loop import Call, Limits, Query, Reservation, Turn, run
from services.orchestrator.work_delivery import (
    MISSING_KEY,
    DeliverableGuard,
    file_guard,
    requested_name,
)

QUESTION = (
    "À partir du fichier notes/projet.txt, prépare une présentation PowerPoint "
    "presentation-hirondelle.pptx de 5 diapositives pour présenter le projet au "
    "comité de pilotage."
)
CLAIM = (
    "La présentation PowerPoint a été générée avec succès. Le fichier "
    "`presentation-hirondelle.pptx` est disponible.\n\n"
    "**Diapositive 1 : Projet Hirondelle**\n- Covoiturage sur le campus"
)
RECEIPT = {"id": "f1", "file_version_id": "v1", "relative_path": "deck.pptx"}
PUBLISHED = {
    "trust": "untrusted",
    "deliverable": RECEIPT,
    "text": "[Download deck.pptx](/d)",
    "file_version_ids": ["v1"],
    "verification": {
        "status": "passed",
        "command": None,
        "exit_code": None,
        "reason": "x",
    },
}
PUBLISH = Call(
    "c1",
    "publish_document",
    json.dumps({"path": "deck.pptx", "title": "Hirondelle", "slides": []}),
)


class Files:
    """The receipts of a turn: the requested file exists once published."""

    def __init__(self) -> None:
        self.deliverables: list[dict[str, Any]] = []
        self.failures: dict[str, str] = {}

    def pending(self) -> bool:
        return not self.deliverables


class GuardTests(unittest.TestCase):
    def test_the_first_final_answer_without_the_file_goes_back_once(self) -> None:
        files = Files()
        guard = DeliverableGuard(files.pending, ".pptx", lambda: files.failures)
        step = guard.next_step()
        assert step is not None
        self.assertIn("a PPTX file", step)
        self.assertIn("no file has been published in this turn", step)
        self.assertIn("publish_document", step)
        self.assertIsNone(guard.next_step())

    def test_a_published_file_changes_nothing(self) -> None:
        files = Files()
        files.deliverables.append(RECEIPT)
        guard = DeliverableGuard(files.pending, ".pptx", lambda: files.failures)
        self.assertIsNone(guard.next_step())
        self.assertEqual(guard.answer(CLAIM, "fr", QUESTION), (CLAIM, False))

    def test_no_file_means_no_success_claim(self) -> None:
        files = Files()
        guard = DeliverableGuard(files.pending, ".pptx", lambda: files.failures)
        text, changed = guard.answer(CLAIM, "fr", QUESTION)
        self.assertTrue(changed)
        self.assertTrue(
            text.startswith(
                "**Le fichier demandé (presentation-hirondelle.pptx) n'a pas été créé**"
            )
        )
        self.assertIn("aucune publication de fichier n'a été faite", text)
        # The content stays, as text only; the announcement of the file goes.
        self.assertIn("Covoiturage sur le campus", text)
        self.assertNotIn("générée avec succès", text)

    def test_a_failed_publication_is_the_reason_given(self) -> None:
        files = Files()
        files.failures["publish_document"] = "Invalid deliverable path or format"
        guard = DeliverableGuard(files.pending, ".pptx", lambda: files.failures)
        step = guard.next_step()
        assert step is not None
        self.assertIn("Invalid deliverable path or format", step)
        text, _ = guard.answer("Slides.", "en", "make me slides.pptx")
        self.assertIn("its publication failed in this turn", text)
        self.assertIn("(slides.pptx)", text)

    def test_the_names_and_languages(self) -> None:
        self.assertEqual(requested_name(QUESTION), "presentation-hirondelle.pptx")
        self.assertEqual(requested_name("un rapport Word sur le projet"), "")
        guard = DeliverableGuard(lambda: True, None)
        for lang in ("fr", "en", "de", "es", "it", "pt"):
            text, changed = guard.answer("", lang, "a report")
            self.assertTrue(changed)
            self.assertEqual(text.count("\n\n"), 1, lang)

    def test_only_a_turn_that_asked_for_a_file_is_held(self) -> None:
        tools = Mock(
            selection=Mock(deliverable_pending=Mock(return_value=True)),
            requested_suffix=".pptx",
            validation_failures={},
        )
        self.assertIsNotNone(file_guard(tools, Mock(produces_files=True)))
        self.assertIsNone(file_guard(tools, Mock(produces_files=False)))
        self.assertIsNone(file_guard(Mock(selection=None), Mock(produces_files=True)))


def _limits() -> Limits:
    return Limits(
        profile="atlas-qwen",
        tokens=None,
        tool_calls=4,
        wall_clock=30,
        cost=Decimal("1"),
        model_calls=6,
        code_mode=True,
    )


class LoopTests(unittest.IsolatedAsyncioTestCase):
    """The real loop: a final answer without the file goes back once."""

    async def turn(self, turns: list[Turn]) -> tuple[Any, list[Any], DeliverableGuard]:
        files = Files()
        guard = DeliverableGuard(files.pending, ".pptx", lambda: files.failures)
        seen: list[list[dict[str, Any]]] = []

        async def complete(messages: list[dict[str, Any]], timeout: float) -> Turn:
            seen.append([dict(m) for m in messages])
            return turns[len(seen) - 1]

        async def execute(call: Call, timeout: float) -> dict[str, Any]:
            files.deliverables.append(RECEIPT)
            return PUBLISHED

        model = Mock(
            estimate=Mock(return_value=Reservation(100, Decimal("0.001"))),
            complete=AsyncMock(side_effect=complete),
        )
        tools = Mock(
            estimate=Mock(return_value=Reservation(0, Decimal(0))),
            execute=AsyncMock(side_effect=execute),
        )
        with patch("services.orchestrator.code_workspace.enabled", return_value=True):
            result = await run(
                Query(question=QUESTION, lang="fr"),
                model,
                tools,
                "",
                _limits(),
                final_only=True,
                before_final=lambda executed: guard.next_step(),
            )
        return result, seen, guard

    async def test_the_repair_publishes_the_file(self) -> None:
        result, seen, guard = await self.turn(
            [Turn(CLAIM), Turn("", (PUBLISH,)), Turn("Voici la présentation.")]
        )
        self.assertEqual(result.text, "Voici la présentation.")
        self.assertIn("no file has been published", seen[1][-1]["content"])
        self.assertEqual(seen[1][-2], {"role": "assistant", "content": CLAIM})
        self.assertEqual(guard.answer(result.text, "fr", QUESTION)[1], False)

    async def test_a_second_answer_without_the_file_is_said_plainly(self) -> None:
        result, seen, guard = await self.turn([Turn(CLAIM), Turn(CLAIM)])
        self.assertEqual(len(seen), 2)
        text, changed = guard.answer(result.text, "fr", QUESTION)
        self.assertTrue(changed)
        self.assertTrue(text.startswith("**Le fichier demandé"))


class ExecuteTurnTests(unittest.IsolatedAsyncioTestCase):
    """execute_turn holds the answer of a file turn, never of another turn."""

    async def turn(self, pending: bool, produces_files: bool = True) -> Any:
        from services.orchestrator import code_mode
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.code_tools import WorkTools
        from services.orchestrator.loop import CodeResult

        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": QUESTION}]}
        )
        tools = WorkTools(Mock(), Mock())
        tools.selection = Mock(deliverable_pending=Mock(return_value=pending))
        tools.requested_suffix = ".pptx"
        emit = AsyncMock()
        model = Mock(produces_files=produces_files)
        done = CodeResult()
        done.state, done.text = "done", CLAIM
        with (
            patch("services.orchestrator.code_workspace.enabled", return_value=True),
            patch.object(code_mode, "run", AsyncMock(return_value=done)) as loop,
            patch.object(tools, "emit", emit),
        ):
            result = await code_mode.execute_turn(request, model, tools, _limits())
        assert loop.await_args is not None
        self.before_final = loop.await_args.kwargs["before_final"]
        self.emit = emit
        return result, request

    async def test_requested_file_not_published_no_success_claim(self) -> None:
        result, request = await self.turn(pending=True)
        # The notice comes first, written by code in the person's language.
        self.assertTrue(result.text.startswith("**"))
        self.assertIn("Covoiturage sur le campus", result.text)
        self.assertIn("(presentation-hirondelle.pptx)", result.text)
        self.assertNotIn("générée avec succès", result.text)
        self.assertTrue(request.__dict__.get(MISSING_KEY))
        labels = [call.kwargs.get("label") for call in self.emit.await_args_list]
        self.assertIn("Requested file not created", labels)
        self.assertIn("publish_document", self.before_final(["web_search"]) or "")

    async def test_published_file_normal_answer(self) -> None:
        result, request = await self.turn(pending=False)
        self.assertEqual(result.text, CLAIM)
        self.assertNotIn(MISSING_KEY, request.__dict__)
        self.assertIsNone(self.before_final([]))

    async def test_a_turn_without_a_requested_file_is_left_alone(self) -> None:
        result, request = await self.turn(pending=True, produces_files=False)
        self.assertEqual(result.text, CLAIM)
        self.assertNotIn(MISSING_KEY, request.__dict__)


if __name__ == "__main__":
    unittest.main()
