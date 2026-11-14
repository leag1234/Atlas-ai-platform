"""A researched Word or PDF report written as streamed text, then published by code.

The provider sends a long tool argument as one final frame: a document written
as publish_document's content showed nothing for 24 to 75 s and was lost whole
when cut. Behind ATLAS_WORK_STREAMED_DOCUMENTS, the writing step streams the
document as an answer and code publishes it through the same tool.
"""

from decimal import Decimal
import json
import os
from time import monotonic
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch

from packages.ports import atlas_ports
from services.orchestrator import document_stream
from services.orchestrator.code_mode import CodeModel
from services.orchestrator.document_stream import (
    CUT_NOTE,
    DocumentWriter,
    complete_part,
    file_name,
    retrieved_urls,
)
from services.orchestrator.loop import Reservation, Turn
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.work_progress import WorkProgress

PAGE = "https://example.org/offshore-wind"
OTHER = "https://invented.example.com/report"


def researched(
    *, switch_back: bool = True, research: bool = True
) -> list[dict[str, Any]]:
    def call(identifier: str, name: str) -> dict[str, Any]:
        return {
            "id": identifier,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "rapport pdf sur l'éolien en mer avec sources"},
    ]
    if research:
        messages += [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [call("f1", "web_fetch")],
            },
            {
                "role": "tool",
                "tool_call_id": "f1",
                "content": json.dumps({"url": PAGE, "text": "..."}),
            },
        ]
    last = "switch_tool_group" if switch_back else "read_document"
    messages += [
        {"role": "assistant", "content": "", "tool_calls": [call("s1", last)]},
        {"role": "tool", "tool_call_id": "s1", "content": "{}"},
    ]
    return messages


OFFERED = [
    {"type": "function", "function": {"name": name}}
    for name in ("switch_tool_group", "read_document", "publish_document")
]


def writer(**changes: Any) -> DocumentWriter:
    values: dict[str, Any] = dict(
        suffix=".pdf",
        question="rapport pdf sur l'éolien en mer avec sources",
        pending=lambda: True,
        group=lambda: "documents",
        retrieved=lambda: {PAGE},
    )
    values.update(changes)
    return DocumentWriter(**values)


def fake_model() -> SimpleNamespace:
    return SimpleNamespace(
        sink=None,
        answer_only=False,
        progress_deadline=monotonic() + 200,
        estimate=lambda messages: Reservation(9000, Decimal("0.05")),
    )


DOCUMENT = (
    "# Rapport sur l'éolien en mer\n\n## Résumé\n\nLa France a 2 GW en service.\n\n"
    "## Sources\n\n- [Éolien en mer](" + PAGE + ")\n- [Rapport](" + OTHER + ")\n"
)


class DueTests(unittest.TestCase):
    def test_flag_is_off_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(document_stream.enabled())
        with patch.dict(os.environ, {"ATLAS_WORK_STREAMED_DOCUMENTS": "1"}):
            self.assertTrue(document_stream.enabled())

    def test_due_only_when_research_is_done_and_the_agent_is_back_to_publish(
        self,
    ) -> None:
        self.assertTrue(writer().due(researched(), OFFERED))
        self.assertFalse(writer().due(researched(research=False), OFFERED))
        self.assertFalse(writer().due(researched(switch_back=False), OFFERED))
        self.assertFalse(
            writer(group=lambda: "spreadsheets").due(researched(), OFFERED)
        )
        self.assertFalse(writer(pending=lambda: False).due(researched(), OFFERED))
        self.assertFalse(writer().due(researched(), OFFERED[:2]))
        done = writer()
        done.attempted = True
        self.assertFalse(done.due(researched(), OFFERED))

    def test_only_word_and_pdf(self) -> None:
        with self.assertRaises(ValueError):
            writer(suffix=".xlsx")


class WriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_text_is_published_through_publish_document(self) -> None:
        model = fake_model()
        deadline = model.progress_deadline
        reports: list[dict[str, object]] = []
        seen: dict[str, Any] = {}

        async def report(event: dict[str, object]) -> None:
            reports.append(event)

        async def complete(messages: list[dict[str, Any]], timeout: float) -> Turn:
            seen.update(
                answer_only=model.answer_only,
                deadline=model.progress_deadline,
                instruction=messages[-1]["content"],
                timeout=timeout,
            )
            for piece in (DOCUMENT[:40], DOCUMENT[40:]):
                await model.sink({"delta": {"content": piece}})
            return Turn(DOCUMENT, (), Reservation(4000, Decimal("0.01")))

        with patch.object(document_stream, "PROGRESS_INTERVAL_SECONDS", 0):
            turn = await writer(report=report).write(model, researched(), 240, complete)
        assert turn is not None
        self.assertTrue(seen["answer_only"])
        self.assertAlmostEqual(seen["deadline"], deadline - 30, places=3)
        self.assertLessEqual(seen["timeout"], 240)
        for rule in (
            "'# '",
            "Sources",
            "at most 14 columns",
            "No images",
            "only pages retrieved",
        ):
            self.assertIn(rule, seen["instruction"])
        # The model's state is restored for the following calls.
        self.assertEqual(
            (model.sink, model.answer_only, model.progress_deadline),
            (None, False, deadline),
        )
        (call,) = turn.calls
        self.assertEqual(call.name, "publish_document")
        arguments = json.loads(call.arguments)
        self.assertEqual(arguments["path"], "Rapport_sur_l_eolien_en_mer.pdf")
        self.assertEqual(arguments["sources"], [])
        # D7: the retrieved page stays cited, the page never retrieved does not.
        self.assertIn(PAGE, arguments["content"])
        self.assertNotIn(OTHER, arguments["content"])
        self.assertEqual(turn.usage, Reservation(4000, Decimal("0.01")))
        # The status carries sizes only, never the text.
        self.assertTrue(reports)
        writing = reports[-1]["work_step"]["writing"]  # type: ignore[index]
        self.assertEqual(set(writing), {"sections", "words"})
        self.assertNotIn("éolien", json.dumps(reports, ensure_ascii=False))

    async def test_a_cut_call_keeps_its_complete_sections(self) -> None:
        model = fake_model()
        paragraph = "Une phrase sourcée sur les parcs en service. " * 40
        written = f"# Titre\n\n## Parcs\n\n{paragraph}\n\n## Projets\n\nLe parc de"

        async def complete(messages: list[dict[str, Any]], timeout: float) -> Turn:
            await model.sink({"delta": {"content": written}})
            raise TimeoutError()

        document = writer()
        messages = researched()
        turn = await document.write(model, messages, 240, complete)
        assert turn is not None
        content = json.loads(turn.calls[0].arguments)["content"]
        self.assertIn(paragraph.strip(), content)
        self.assertNotIn("## Projets", content)
        self.assertTrue(document.cut)
        # Unknown usage: the whole reservation stays spent.
        self.assertEqual(turn.usage, model.estimate(messages))
        self.assertEqual(
            document.annotate(messages)[-1], {"role": "user", "content": CUT_NOTE}
        )
        self.assertEqual(model.answer_only, False)

    async def test_a_cut_with_little_text_fails_as_before(self) -> None:
        model = fake_model()

        async def complete(messages: list[dict[str, Any]], timeout: float) -> Turn:
            await model.sink({"delta": {"content": "# Titre\n\nDébut"}})
            raise RuntimeError("gateway_error")

        with self.assertRaises(RuntimeError):
            await writer().write(model, researched(), 240, complete)

    async def test_an_empty_answer_goes_on_as_before(self) -> None:
        model = fake_model()

        async def complete(messages: list[dict[str, Any]], timeout: float) -> Turn:
            return Turn("", (), Reservation(100, Decimal("0.001")))

        document = writer()
        self.assertIsNone(await document.write(model, researched(), 240, complete))
        self.assertFalse(document.due(researched(), OFFERED))

    async def test_no_time_left_goes_on_as_before(self) -> None:
        model = fake_model()
        model.progress_deadline = monotonic() + 10
        complete = AsyncMock()
        self.assertIsNone(await writer().write(model, researched(), 240, complete))
        complete.assert_not_called()


class ModelTests(unittest.IsolatedAsyncioTestCase):
    def model(self) -> CodeModel:
        model = CodeModel(
            atlas_ports().url("gateway", "localhost"),
            Configuration(
                input_eur_per_mtok=Decimal("0.6"),
                output_eur_per_mtok=Decimal("3.6"),
                max_tokens=512,
            ),
        )
        model.work_cost = Decimal("0.50")
        return model

    async def test_answer_only_asks_the_gateway_for_no_tool(self) -> None:
        model = self.model()
        wire = {
            "text": "ok",
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            "calls": [],
        }
        post = AsyncMock(return_value=wire)
        with patch.object(model, "post", new=post):
            await model.complete([{"role": "user", "content": "x"}], 10)
            self.assertNotIn("tool_choice", post.call_args.args[1])
            model.answer_only = True
            await model.complete([{"role": "user", "content": "x"}], 10)
        payload = post.call_args.args[1]
        self.assertEqual(payload["tool_choice"], "none")
        # The tools stay declared: the gateway keeps treating the call as a Work step.
        self.assertTrue(payload["tools"])

    async def test_work_model_takes_the_writing_step_and_keeps_a_cut_reserved(
        self,
    ) -> None:
        model = self.model()
        published = Turn("", (), Reservation(1, Decimal("0.001")))
        stub = SimpleNamespace(
            cut=True,
            annotate=lambda messages: messages,
            due=lambda messages, offered: True,
            write=AsyncMock(return_value=published),
        )
        model.document_writer = stub  # type: ignore[assignment]
        with patch.object(GatewayModel, "complete", new=AsyncMock()) as regular:
            self.assertIs(
                await model.complete([{"role": "user", "content": "x"}], 10), published
            )
            regular.assert_not_called()
        self.assertTrue(model.work_unknown)
        self.assertGreater(model.work_unknown_eur, 0)
        stub.due = lambda messages, offered: False
        with patch.object(
            GatewayModel, "complete", new=AsyncMock(return_value=published)
        ) as regular:
            await model.complete([{"role": "user", "content": "x"}], 10)
            regular.assert_awaited_once()


class HelperTests(unittest.TestCase):
    def test_file_name_from_the_title(self) -> None:
        self.assertEqual(
            file_name("# L'AI Act : résumé\n\ntexte", ".docx"), "L_AI_Act_resume.docx"
        )
        self.assertEqual(
            file_name("texte sans titre ###", ".pdf"), "texte_sans_titre.pdf"
        )
        self.assertEqual(file_name("# ???", ".pdf"), "Document.pdf")

    def test_complete_part_drops_an_unfinished_block_and_an_empty_heading(self) -> None:
        self.assertEqual(
            complete_part("# T\n\n## A\n\nfait.\n\n## B\n\nmoit"),
            "# T\n\n## A\n\nfait.\n",
        )
        self.assertEqual(complete_part("début sans fin"), "")

    def test_retrieved_urls_as_the_loop_reads_them(self) -> None:
        self.assertEqual(retrieved_urls({"url": PAGE, "text": "x"}), {PAGE})
        self.assertEqual(
            retrieved_urls(
                {"data": {"results": [{"link": PAGE}, {"title": "no link"}]}}
            ),
            {PAGE},
        )
        self.assertEqual(
            retrieved_urls({"error": "search_unavailable", "url": PAGE}), set()
        )

    def test_writing_status_shows_sizes_in_the_conversation_language(self) -> None:
        progress = WorkProgress("fr", lambda: True)
        self.assertEqual(
            progress.writing(3, 420),
            ("work_step", "Rédaction du document… 3 sections, 420 mots"),
        )
        self.assertIsNone(progress.writing("3", 420))
        self.assertIsNone(progress.writing(-1, 0))


if __name__ == "__main__":
    unittest.main()
