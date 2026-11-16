"""A built file is checked against the request before it is published and
claimed (owner rule 2026-11-16; staging: a picture book asked with one emoji
per word was delivered three times without a single emoji).

The model replies are recorded from the staging gateway (2026-11-16), on the
route that sees the first page (vision role) and on the text route (strict
privacy, workbooks): tests/fixtures/deliverable_check_replies.json.
"""

from __future__ import annotations

from decimal import Decimal
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from contextlib import nullcontext
import unittest
from unittest import mock
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from packages.doc_render import emoji
from packages.doc_render.schema import Content
from services.orchestrator import deliverable_check
from services.orchestrator.presentation_delivery import build_project_document
from services.orchestrator.project_document_input import normalize
from services.orchestrator.project_document_tools import execute
from services.orchestrator.terminal_client import ProjectPublisher
from services.orchestrator.work_closing import closing_step

RECORDED = json.loads(
    (Path(__file__).parent / "fixtures/deliverable_check_replies.json").read_text()
)["cases"]
WITH_GAPS = {"imagier_without_pictures", "docx_three_of_five_columns"}


def content_of(case: dict[str, Any]) -> Content:
    value = normalize(dict(path="file" + case["suffix"], content=case["markdown"]))
    return Content.model_validate(value["content"])


def built(case: dict[str, Any]) -> tuple[Content, Any]:
    content = content_of(case)
    document = build_project_document(
        content, case["suffix"], [], max_bytes=16 * 1024 * 1024, timeout=30
    )
    return content, document


def turn_tools(request: str) -> SimpleNamespace:
    model = SimpleNamespace(
        profile="atlas-qwen", strict=False, spent=Decimal(0), allowance=Decimal("0.1")
    )
    return SimpleNamespace(
        check_model=model,
        request_text=request,
        earlier_requests=["an earlier message"],
        deliverable_checks=0,
        deliverable_refused=False,
    )


def replay(*replies: dict[str, Any]) -> AsyncMock:
    return AsyncMock(side_effect=[dict(reply) for reply in replies])


class FactsTests(unittest.TestCase):
    def test_a_table_is_measured_with_its_columns_and_rows(self) -> None:
        case = RECORDED["docx_three_of_five_columns"]
        content, document = built(case)
        facts = deliverable_check.facts(
            ".docx", content, document.content, document.preview, []
        )
        table = facts["sections"][0]["tables"][0]
        self.assertEqual(table["columns"], 3)
        self.assertEqual(table["data_rows"], 3)
        self.assertEqual(facts["format"], "docx")
        self.assertGreater(facts["text_characters"], 0)

    def test_drawn_emoji_and_pages_are_counted(self) -> None:
        if emoji.font_path() is None:
            self.skipTest("no colour emoji font on this system")
        content, document = built(RECORDED["imagier_with_pictures"])
        facts = deliverable_check.facts(
            ".pdf", content, document.content, document.preview, []
        )
        self.assertEqual(facts["emoji_drawn_in_file"], 12)
        self.assertGreaterEqual(facts["pages"], 2)
        # The check reads what was drawn: the emoji are in its text although
        # the PDF's text layer has none (they are pictures).
        self.assertIn("\U0001f414", deliverable_check.drawn_text(content))

    def test_emoji_the_format_cannot_draw_are_named(self) -> None:
        try:
            with mock.patch.dict(os.environ, {"ATLAS_EMOJI_FONT": ""}):
                emoji.reset()
                content = {
                    "title": "Picture book",
                    "sections": [
                        {"heading": "\U0001f414 Hen", "paragraphs": ["\U0001f34e x"]}
                    ],
                }
                self.assertEqual(
                    deliverable_check.left_out(content), ["\U0001f414", "\U0001f34e"]
                )
                self.assertEqual(deliverable_check.left_out("plain text -> ok"), [])
        finally:
            emoji.reset()


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_replies_find_the_gaps_and_only_them(self) -> None:
        for name, case in RECORDED.items():
            for route in ("page", "text"):
                with self.subTest(case=name, route=route):
                    content, document = built(case)
                    tools = turn_tools(case["request"])
                    complete = replay(case["replies"][route])
                    page = (
                        patch(
                            "packages.doc_render.preview.first_page", return_value=None
                        )
                        if route == "text"
                        else nullcontext()
                    )
                    with page:
                        outcome = await deliverable_check.review(
                            tools,
                            case["suffix"],
                            content,
                            document.content,
                            document.preview,
                            case["left_out"],
                            complete=complete,
                        )
                    self.assertTrue(outcome.checked)
                    self.assertEqual(bool(outcome.gaps), name in WITH_GAPS)
                    awaited = complete.await_args
                    assert awaited is not None
                    payload = awaited.args[0]
                    self.assertEqual(payload["tools"], [])
                    self.assertEqual(
                        payload["response_format"], {"type": "json_object"}
                    )
                    self.assertEqual(payload["profile"], "atlas-qwen")
                    self.assertLessEqual(
                        Decimal(payload["budget_eur"]), Decimal("0.02")
                    )
                    self.assertLessEqual(
                        payload["timeout"], deliverable_check.CHECK_SECONDS
                    )
                    user = payload["messages"][1]["content"]
                    self.assertEqual(isinstance(user, list), route == "page")
                    self.assertEqual(outcome.image, route == "page")
                    self.assertEqual(
                        tools.check_model.spent,
                        Decimal(case["replies"][route]["cost_eur"]),
                    )
                    self.assertEqual(tools.deliverable_checks, 1)

    async def test_no_call_without_the_turn_model_or_past_the_bound(self) -> None:
        case = RECORDED["docx_three_of_five_columns"]
        content, document = built(case)
        complete = replay(case["replies"]["page"])
        for tools in (
            SimpleNamespace(request_text=case["request"]),
            dict_tools(turn_tools(case["request"]), deliverable_checks=3),
            dict_tools(turn_tools(""), deliverable_checks=0),
        ):
            outcome = await deliverable_check.review(
                tools,
                ".docx",
                content,
                document.content,
                document.preview,
                [],
                complete=complete,
            )
            self.assertEqual(outcome.gaps, [])
            self.assertFalse(outcome.checked)
        # No time left to spare in the publication: no call either.
        outcome = await deliverable_check.review(
            turn_tools(case["request"]),
            ".docx",
            content,
            document.content,
            document.preview,
            [],
            seconds=3,
            complete=complete,
        )
        self.assertFalse(outcome.checked)
        complete.assert_not_awaited()

    async def test_a_failed_call_keeps_the_measured_gaps(self) -> None:
        case = RECORDED["imagier_without_pictures"]
        content, document = built(case)
        tools = turn_tools(case["request"])
        outcome = await deliverable_check.review(
            tools,
            ".pdf",
            content,
            document.content,
            document.preview,
            ["\U0001f414"],
            complete=AsyncMock(side_effect=TimeoutError()),
        )
        self.assertFalse(outcome.checked)
        self.assertEqual(len(outcome.gaps), 1)
        self.assertIn("\U0001f414", outcome.gaps[0])
        # Measured but not judged: published with a note, never refused.
        self.assertIn("must say so", deliverable_check.decide(tools, outcome))
        for reply in ("not json", '{"gaps": "none"}'):
            self.assertIsNone(deliverable_check.gaps_of(reply))


def dict_tools(tools: SimpleNamespace, **values: Any) -> SimpleNamespace:
    for key, value in values.items():
        setattr(tools, key, value)
    return tools


def publishing_tools(request: str) -> Mock:
    execution = Mock()
    execution.check = AsyncMock()

    async def public(method: str, path: str) -> tuple[int, dict[str, Any]]:
        if path == "folders":
            return 200, dict(items=[dict(id="folder", revision=0)])
        return 200, dict(deliverables_folder_id="folder")

    execution.broker.public = AsyncMock(side_effect=public)
    tools = Mock(execution=execution)
    tools.deliverables = []
    tools.requested_suffix = None
    tools.numbered_copies = False
    tools.calculation_refusals = set()
    tools.selection = None
    for key, value in vars(turn_tools(request)).items():
        setattr(tools, key, value)
    return tools


class RepairFlowTests(unittest.IsolatedAsyncioTestCase):
    async def publish(
        self, tools: Mock, case: dict[str, Any], replies: AsyncMock
    ) -> Any:
        write = AsyncMock(
            side_effect=lambda document, name, *rest: dict(
                id=str(uuid4()), file_version_id=str(uuid4()), relative_path=name
            )
        )
        arguments = dict(
            path="people" + case["suffix"], content=case["markdown"], sources=[]
        )
        with (
            patch.object(ProjectPublisher, "publish", write),
            patch.object(deliverable_check, "_complete", replies),
            patch(
                "services.orchestrator.file_results.project_deliverable_links",
                return_value="links",
            ),
        ):
            try:
                return await execute(
                    tools, "publish_document", json.dumps(arguments), 120
                )
            finally:
                self.written = write.await_count

    async def test_gaps_are_refused_once_then_the_repair_is_published(self) -> None:
        short = RECORDED["docx_three_of_five_columns"]
        full = RECORDED["docx_five_columns"]
        tools = publishing_tools(short["request"])
        replies = replay(short["replies"]["page"], full["replies"]["page"])
        with self.assertRaisesRegex(ValueError, "deliverable_check: .*3 columns"):
            await self.publish(tools, short, replies)
        self.assertEqual(self.written, 0)  # nothing published before the repair
        result = await self.publish(tools, full, replies)
        self.assertEqual(self.written, 1)
        self.assertNotIn("gaps", result)
        self.assertNotIn("note", result)
        self.assertNotIn(
            "lacks", closing_step(tools, True, [("publish_document", result)]) or ""
        )

    async def test_a_gap_left_after_the_repair_is_said_in_the_answer(self) -> None:
        short = RECORDED["docx_three_of_five_columns"]
        tools = publishing_tools(short["request"])
        replies = replay(short["replies"]["page"], short["replies"]["text"])
        with self.assertRaises(ValueError):
            await self.publish(tools, short, replies)
        result = await self.publish(tools, short, replies)
        self.assertEqual(self.written, 1)
        self.assertTrue(result["gaps"])
        self.assertIn("must say so plainly", result["note"])
        instruction = closing_step(tools, True, [("publish_document", result)])
        assert instruction is not None
        self.assertIn("lacks", instruction)
        self.assertIn("3 columns", instruction)
        self.assertEqual(replies.await_count, 2)


if __name__ == "__main__":
    unittest.main()
