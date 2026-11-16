"""Pictures drawn in a Work turn for the document that shows them
(work_pictures.py; owner correction 2026-11-16: emoji are not the pictures a
request asks for). The picture server and the vision check are faked here;
tools/request_workflow_eval.py runs the whole workflow live.
"""

from __future__ import annotations

import base64
from decimal import Decimal
import io
import json
import os
import tempfile
import time
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock
from unittest.mock import AsyncMock

from PIL import Image

from packages.doc_render.schema import Content
from services.orchestrator import work_pictures
from services.orchestrator.pictures.cpu_server import Rendered
from services.orchestrator.presentation_delivery import build_project_document
from services.orchestrator.project_document_input import normalize
from services.orchestrator.project_document_tools import placed_pictures


def png(colour: str) -> str:
    output = io.BytesIO()
    Image.new("RGB", (64, 64), colour).save(output, "PNG")
    return base64.b64encode(output.getvalue()).decode()


class FakeServer:
    """One picture at a time, `per` seconds each, on a fake clock."""

    PICTURE_SECONDS = 22.0

    def __init__(self, clock: list[float], per: float = 20.0) -> None:
        self.clock, self.per = clock, per
        self.prompts: list[str] = []

    async def health(self) -> dict[str, Any]:
        return {"ready": True, "seconds_per_image": self.per, "busy_seconds": 0.0}

    def expected_wait(self, status: dict[str, Any], pictures: int) -> float:
        return self.per * pictures

    async def render_set(
        self, prompt: str, seeds: list[int], wait: float
    ) -> list[Rendered]:
        self.prompts.append(prompt)
        self.clock[0] += self.per
        colour = ("red", "green", "blue", "orange", "purple")[len(self.prompts) % 5]
        return [Rendered(png(colour), 0.01, self.per, 0.0, 0.1)]


class DownServer(FakeServer):
    async def health(self) -> dict[str, Any]:
        raise OSError("down")


def tools_for(scope: str, deadline: float) -> SimpleNamespace:
    model = SimpleNamespace(
        profile="atlas-qwen", strict=False, spent=Decimal(0), progress_deadline=deadline
    )
    return SimpleNamespace(
        check_model=model,
        picture_scope=scope,
        sink=None,
        lang="en",
        pictures_pending=[],
    )


def items(count: int) -> str:
    return json.dumps(
        {
            "items": [
                {"label": f"item {n}", "description": f"a plain object number {n}"}
                for n in range(count)
            ],
            "style": "flat illustration",
        }
    )


class WorkPictureTests(unittest.IsolatedAsyncioTestCase):
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
            SimpleNamespace(monotonic=lambda: self.clock[0], time=time.time),
        )
        clock.start()
        self.addCleanup(clock.stop)

    def checker(self, *verdicts: Any) -> AsyncMock:
        # Each check reads two statements: the item, and no writing.
        replies = [
            {
                "text": json.dumps(
                    {"met": [value, True] if isinstance(value, bool) else value}
                ),
                "cost_eur": "0.0004",
            }
            for value in verdicts
        ]
        return AsyncMock(side_effect=replies)

    async def test_every_picture_is_drawn_checked_and_kept(self) -> None:
        tools = tools_for("subject|chat-a", deadline=self.clock[0] + 600)
        server = FakeServer(self.clock)
        check = self.checker(True, True, True)
        result = await work_pictures.execute(
            tools, items(3), 900, server=server, call=check
        )
        self.assertEqual([p["checked"] for p in result["pictures"]], ["matches"] * 3)
        self.assertNotIn("not_drawn", result)
        self.assertEqual(tools.pictures_pending, [])
        self.assertTrue(all("flat illustration" in p for p in server.prompts))
        self.assertEqual(tools.check_model.spent, Decimal("0.0012"))
        for picture in result["pictures"]:
            jpeg = work_pictures.resolve(tools, picture["reference"])
            self.assertTrue(jpeg.startswith(b"\xff\xd8"))

    async def test_what_does_not_fit_in_the_turn_is_named_with_its_time(self) -> None:
        # 75 s are kept for the document and the answer: 140 s leave room for
        # two pictures of 20 s (+2 s of check) and not for the others.
        tools = tools_for("subject|chat-b", deadline=self.clock[0] + 75 + 50)
        tools.sink = AsyncMock()
        result = await work_pictures.execute(
            tools,
            items(5),
            900,
            server=FakeServer(self.clock),
            call=self.checker(*[True] * 5),
        )
        self.assertEqual(len(result["pictures"]), 2)
        self.assertEqual(result["not_drawn"], ["item 2", "item 3", "item 4"])
        self.assertEqual(tools.pictures_pending, ["item 2", "item 3", "item 4"])
        self.assertEqual(result["seconds_needed_for_the_rest"], 66)
        # Said when the drawing starts, not only at the end.
        first = tools.sink.await_args_list[0].args[0]["description"]
        self.assertEqual(
            first,
            "Drawing 2 of 5 pictures in this turn (about 1 min); the rest afterwards",
        )
        self.assertIn("offer to continue", result["note"])
        # Going on in the same conversation draws only the rest.
        self.clock[0] += 10
        later = tools_for("subject|chat-b", deadline=self.clock[0] + 600)
        server = FakeServer(self.clock)
        again = await work_pictures.execute(
            later, items(5), 900, server=server, call=self.checker(*[True] * 3)
        )
        self.assertEqual(len(again["pictures"]), 5)
        self.assertEqual(sum(p["reused"] for p in again["pictures"]), 2)
        self.assertEqual(len(server.prompts), 3)

    async def test_a_picture_that_misses_its_item_is_redrawn_once(self) -> None:
        tools = tools_for("subject|chat-c", deadline=self.clock[0] + 600)
        server = FakeServer(self.clock)
        result = await work_pictures.execute(
            tools, items(1), 900, server=server, call=self.checker(False, False)
        )
        self.assertEqual(len(server.prompts), 2)
        self.assertEqual(result["pictures"][0]["checked"], "does_not_match")
        self.assertIn("do not clearly show", result["note"])

    async def test_a_picture_with_garbled_lettering_is_left_out(self) -> None:
        # Staging -91: a poster's bicycle came with pseudo-words baked in.
        tools = tools_for("subject|chat-h", deadline=self.clock[0] + 600)
        server = FakeServer(self.clock)
        result = await work_pictures.execute(
            tools,
            items(1),
            900,
            server=server,
            call=self.checker([True, False], [True, False]),
        )
        self.assertEqual(len(server.prompts), 2)
        self.assertEqual(result["pictures"], [])
        self.assertEqual(result["left_out"], ["item 0"])
        self.assertNotIn("not_drawn", result)
        self.assertEqual(tools.pictures_pending, ["item 0"])
        self.assertIn("unreadable lettering", result["note"])
        self.assertTrue(server.prompts[0].endswith(work_pictures.NO_LETTERING))

    async def test_words_asked_inside_a_picture_are_drawn_and_checked(self) -> None:
        tools = tools_for("subject|chat-i", deadline=self.clock[0] + 600)
        server = FakeServer(self.clock)
        check = self.checker(True)
        arguments = json.dumps(
            {"items": [{"label": "sign", "description": "a shop sign", "text": "OPEN"}]}
        )
        await work_pictures.execute(tools, arguments, 900, server=server, call=check)
        self.assertIn('reads "OPEN"', server.prompts[0])
        statements = check.await_args_list[0].args[0]["messages"][1]["content"][0][
            "text"
        ]
        self.assertIn("OPEN", statements)

    async def test_an_unavailable_server_is_said_never_replaced(self) -> None:
        server = DownServer(self.clock)
        with self.assertRaisesRegex(ValueError, "picture_engine_unavailable"):
            await work_pictures.execute(
                tools_for("subject|chat-d", self.clock[0] + 600),
                items(2),
                900,
                server=server,
                call=self.checker(),
            )

    async def test_a_reference_resolves_only_in_its_conversation(self) -> None:
        tools = tools_for("subject|chat-e", deadline=self.clock[0] + 600)
        result = await work_pictures.execute(
            tools, items(1), 900, server=FakeServer(self.clock), call=self.checker(True)
        )
        reference = result["pictures"][0]["reference"]
        other = tools_for("other|chat-e", deadline=0)
        for scope, value in (
            (other, reference),
            (tools, "../index.json"),
            (tools, "picture-zz"),
        ):
            with self.assertRaisesRegex(ValueError, "unknown_picture_reference"):
                work_pictures.resolve(scope, value)

    async def test_the_document_embeds_the_pictures_of_its_sections(self) -> None:
        tools = tools_for("subject|chat-f", deadline=self.clock[0] + 600)
        result = await work_pictures.execute(
            tools,
            items(2),
            900,
            server=FakeServer(self.clock),
            call=self.checker(True, True),
        )
        first, second = (p["reference"] for p in result["pictures"])
        text = f"# Things\n\n## One\n\n![item 0]({first})\n\nthe first\n\n## Two\n\n![item 1]({second})\n\nthe second\n"
        slides = [
            {"title": "One", "bullets": ["the first"], "images": [first]},
            {"title": "Two", "bullets": ["the second"], "images": [second]},
        ]
        for suffix in (".pdf", ".docx", ".pptx"):
            with self.subTest(suffix=suffix):
                arguments: dict[str, Any] = (
                    dict(path="things.pptx", title="Things", slides=slides)
                    if suffix == ".pptx"
                    else dict(path="things" + suffix, content=text)
                )
                content = Content.model_validate(normalize(arguments)["content"])
                self.assertEqual(content.sections[0].images, (first,))
                placed, pictures = placed_pictures(tools, content)
                document = build_project_document(
                    placed,
                    suffix,
                    [],
                    max_bytes=16 * 1024 * 1024,
                    timeout=30,
                    pictures=pictures,
                )
                if suffix == ".pdf":
                    from pypdf import PdfReader

                    reader = PdfReader(io.BytesIO(document.content))
                    self.assertEqual(sum(len(p.images) for p in reader.pages), 2)
                else:
                    import zipfile

                    with zipfile.ZipFile(io.BytesIO(document.content)) as archive:
                        media = [n for n in archive.namelist() if "/media/" in n]
                    self.assertEqual(len(media), 2)

    def test_no_other_image_reaches_a_document(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported Markdown image"):
            normalize(dict(path="x.pdf", content="# T\n\n![x](/etc/passwd)\n"))
        content = Content.model_validate(
            {"title": "T", "sections": [{"heading": "H", "images": ["photo.png"]}]}
        )
        with self.assertRaisesRegex(ValueError, "unknown_picture_reference"):
            placed_pictures(tools_for("subject|chat-g", 0), content)
        with self.assertRaisesRegex(ValueError, "Scoped image resolution"):
            build_project_document(content, ".pdf", [], max_bytes=1_000_000, timeout=30)


if __name__ == "__main__":
    unittest.main()
