"""The pictures a file needs are planned and drawn by code before the agent
writes, and the publication checks that they are placed (staging -87,
2026-11-16: a picture book written with emoji, a poster without its
illustration, a follow-up "pictures are missing" that multiplied the emoji).

The plan replies are recorded on the staging gateway for varied requests on
two profiles (tests/fixtures/picture_plan_replies.json); the picture server is
faked (tests/test_work_pictures.py).
"""

from __future__ import annotations

from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock
from unittest.mock import AsyncMock

from packages.doc_render.layout import font, wrap
from packages.doc_render.schema import Content
from services.orchestrator import deliverable_check, work_pictures
from test_work_pictures import FakeServer

RECORDED = json.loads(
    (Path(__file__).parent / "fixtures/picture_plan_replies.json").read_text()
)["profiles"]


def tools_for(scope: str, request: str = "", earlier: list[str] | None = None) -> Any:
    model = SimpleNamespace(
        profile="atlas-qwen", strict=False, spent=Decimal(0), progress_deadline=None
    )
    return SimpleNamespace(
        check_model=model,
        picture_scope=scope,
        sink=None,
        lang="fr",
        pictures_pending=[],
        earlier_requests=earlier or [],
        earlier_answers=[],
        instructions="",
        picture_plan=None,
        pictures_drawn={},
        request_text=request,
        deliverable_checks=0,
        deliverable_refused=False,
    )


class PlanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.dict(os.environ, {"ATLAS_WORK_PICTURE_DIR": folder.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = [1000.0]
        clock = mock.patch.object(
            work_pictures,
            "time",
            SimpleNamespace(monotonic=lambda: self.clock[0], time=lambda: 0.0),
        )
        clock.start()
        self.addCleanup(clock.stop)
        enabled = mock.patch.object(work_pictures, "enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    async def test_recorded_plans_ask_pictures_only_when_the_request_needs_them(
        self,
    ) -> None:
        for profile, cases in RECORDED.items():
            for name, case in cases.items():
                with self.subTest(profile=profile, case=name):
                    plan = work_pictures.parse_plan(case["reply"]["text"])
                    assert plan is not None
                    expected = case["expected_pictures"]
                    if expected:
                        self.assertGreaterEqual(len(plan.pictures), expected)
                    else:
                        self.assertEqual(plan.pictures, [])
                    self.assertEqual(plan.emoji_requested, name == "emoji_asked_en")

    async def test_planned_pictures_are_drawn_before_the_agent_writes(self) -> None:
        case = RECORDED["atlas-qwen"]["poster_en"]
        tools = tools_for("subject|poster", case["request"])
        server = FakeServer(self.clock)
        replies = AsyncMock(
            side_effect=[
                dict(case["reply"]),
                {"text": json.dumps({"met": [True, True]}), "cost_eur": "0.0004"},
            ]
        )
        plan = await work_pictures.prepare(
            tools, case["request"], 280, server=server, call=replies
        )
        assert plan is not None
        self.assertEqual(len(server.prompts), 1)
        (reference,) = tools.pictures_drawn
        self.assertIn(reference, tools.instructions)
        self.assertIn("Do not replace a picture with an emoji", tools.instructions)
        payload = replies.await_args_list[0].args[0]
        self.assertEqual(payload["tools"], [])
        self.assertLessEqual(payload["timeout"], work_pictures.PLAN_SECONDS)

    async def test_a_file_without_pictures_draws_nothing(self) -> None:
        case = RECORDED["atlas-mistral"]["table_no_pictures_fr"]
        tools = tools_for("subject|table", case["request"])
        server = FakeServer(self.clock)
        await work_pictures.prepare(
            tools,
            case["request"],
            280,
            server=server,
            call=AsyncMock(return_value=dict(case["reply"])),
        )
        self.assertEqual(server.prompts, [])
        self.assertEqual(tools.instructions, "")

    async def test_a_follow_up_completes_the_missing_pictures_only(self) -> None:
        cases = RECORDED["atlas-qwen"]
        first, follow = cases["picture_book_fr"], cases["follow_up_missing_pictures_fr"]
        checks = [{"text": json.dumps({"met": [True, True]}), "cost_eur": "0"}] * 40
        server = FakeServer(self.clock)
        # A chat turn: 280 s hold the plan's reserve and 8 pictures of 24 s.
        tools = tools_for("subject|book", first["request"])
        await work_pictures.prepare(
            tools,
            first["request"],
            280,
            server=server,
            call=AsyncMock(side_effect=[dict(first["reply"]), *checks]),
        )
        drawn_first = len(server.prompts)
        self.assertLess(drawn_first, 12)
        self.assertIn("could not be drawn in this turn", tools.instructions)
        later = tools_for("subject|book", follow["request"], [first["request"]])
        await work_pictures.prepare(
            later,
            follow["request"],
            600,
            server=server,
            call=AsyncMock(side_effect=[dict(follow["reply"]), *checks]),
        )
        # The follow-up plans the same items in other words: only the rest
        # is drawn, and every picture of the book is placed.
        self.assertEqual(len(server.prompts), 12)
        self.assertEqual(len(later.pictures_drawn), 12)


class PlanFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Staging -91: the plan timed out at 45 s on the slow turn profile, the
    turn went on without pictures and nothing said so."""

    async def test_the_fast_profile_comes_first_then_the_turn_profile(self) -> None:
        tools = tools_for("subject|z", "a poster with a big illustration")
        case = RECORDED["deliverable_plan role"]["poster_en"]
        replies = AsyncMock(side_effect=[TimeoutError(), dict(case["reply"])])
        plan = await work_pictures.plan(tools, case["request"], call=replies)
        assert plan is not None
        self.assertEqual(len(plan.pictures), 1)
        first, second = (c.args[0] for c in replies.await_args_list)
        self.assertNotEqual(first["profile"], second["profile"])
        self.assertEqual(second["profile"], "atlas-qwen")  # the turn's own
        self.assertLessEqual(first["timeout"], work_pictures.FAST_PLAN_SECONDS)
        self.assertTrue(first["reasoning_disabled"])

    async def test_no_plan_keeps_the_guarantee(self) -> None:
        tools = tools_for("subject|w", "un imagier avec une image par mot")
        with mock.patch.object(work_pictures, "enabled", return_value=True):
            planned = await work_pictures.prepare(
                tools,
                tools.request_text,
                280,
                server=FakeServer([0.0]),
                call=AsyncMock(side_effect=[TimeoutError(), {"text": "not json"}]),
            )
        self.assertIsNone(planned)
        self.assertTrue(tools.picture_plan_failed)
        self.assertIn("draw them first with make_pictures", tools.instructions)
        # The check of the file still judges the pictures from the request;
        # when it cannot run either, the answer must not claim the file whole.
        content = Content.model_validate(
            {
                "title": "Imagier",
                "sections": [{"heading": "Poule", "paragraphs": ["poule"]}],
            }
        )
        outcome = await deliverable_check.review(
            tools,
            ".pdf",
            content,
            b"%PDF",
            b"poule",
            [],
            complete=AsyncMock(side_effect=TimeoutError()),
        )
        self.assertFalse(outcome.checked)
        self.assertIn("could not be checked against the request", outcome.gaps[0])
        self.assertIn("must say so", deliverable_check.decide(tools, outcome))


class SettledTests(unittest.IsolatedAsyncioTestCase):
    async def test_once_planned_the_agent_publishes_instead_of_drawing(self) -> None:
        # Staging -93: after the plan drew the follow-up's pictures, the agent
        # called make_pictures eight more times and the turn ran out of time
        # without publishing the file.
        case = RECORDED["deliverable_plan role"]["table_no_pictures_fr"]
        tools = tools_for("subject|settled", case["request"])
        tools.selection = SimpleNamespace(
            groups={
                "documents": ("read_file", "publish_document", "make_pictures"),
                "code": ("run_command",),
            }
        )
        with mock.patch.object(work_pictures, "enabled", return_value=True):
            await work_pictures.prepare(
                tools,
                case["request"],
                280,
                server=FakeServer([0.0]),
                call=AsyncMock(return_value=dict(case["reply"])),
            )
        self.assertEqual(
            tools.selection.groups["documents"], ("read_file", "publish_document")
        )
        with self.assertRaisesRegex(ValueError, "pictures_already_planned"):
            await work_pictures.execute(
                tools, json.dumps({"items": [{"label": "x", "description": "y"}]}), 60
            )

    async def test_without_a_plan_the_agent_keeps_the_tool(self) -> None:
        tools = tools_for("subject|unsettled", "un imagier")
        tools.selection = SimpleNamespace(groups={"documents": ("make_pictures",)})
        with mock.patch.object(work_pictures, "enabled", return_value=True):
            await work_pictures.prepare(
                tools,
                "un imagier",
                280,
                server=FakeServer([0.0]),
                call=AsyncMock(side_effect=TimeoutError()),
            )
        self.assertEqual(tools.selection.groups["documents"], ("make_pictures",))


class PlacedTests(unittest.TestCase):
    def content(self, text: str) -> Content:
        return Content.model_validate(
            {"title": "Book", "sections": [{"heading": "Hen", "paragraphs": [text]}]}
        )

    def test_emoji_standing_in_for_planned_pictures_is_refused_by_code(self) -> None:
        tools = tools_for("subject|x")
        tools.picture_plan = work_pictures.Plan(
            pictures=[work_pictures.Item(label="hen", description="a hen")]
        )
        tools.pictures_drawn = {"picture-0123456789ab": "hen"}
        gaps = deliverable_check.planned_gaps(tools, self.content("\U0001f414 hen"))
        self.assertEqual(len(gaps), 2)  # the drawn picture is not placed, emoji are
        outcome = deliverable_check.Outcome(gaps=gaps, certain=gaps)
        with self.assertRaisesRegex(ValueError, "deliverable_check: .*emoji"):
            deliverable_check.decide(tools, outcome)

    def test_emoji_asked_for_are_not_a_gap(self) -> None:
        tools = tools_for("subject|y")
        tools.picture_plan = work_pictures.Plan(pictures=[], emoji_requested=True)
        self.assertEqual(
            deliverable_check.planned_gaps(tools, self.content("\U0001f414 hen")), []
        )


class WrapTests(unittest.TestCase):
    def test_lines_break_between_words(self) -> None:
        # Staging -87: headings and cells broke inside words ("WORKSHO / P").
        font()
        for text in (
            "NEIGHBOURHOOD BIKE REPAIR WORKSHOP",
            "Bœuf bourguignon de la maison, servi avec des pommes de terre grillées",
        ):
            lines = wrap(text, 200, 19)
            self.assertGreater(len(lines), 1)
            self.assertEqual(" ".join(lines), text)
        long = "https://example.org/" + "a" * 120
        self.assertEqual("".join(wrap(long, 160, 12)), long)


if __name__ == "__main__":
    unittest.main()
