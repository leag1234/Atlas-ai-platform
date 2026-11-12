"""REQ-PROJ-05: inline Markdown is read in titles, headings, paragraphs and
bullets of every format, and a source excerpt is still found as typed."""

from io import BytesIO
import json
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from docx import Document
from openpyxl import load_workbook
from pptx import Presentation
from pypdf import PdfReader

from packages.doc_render.cells import prose, shown
from packages.doc_render.schema import Content
from services.orchestrator.presentation_delivery import build_project_document
from services.orchestrator.project_document_input import normalize
from services.orchestrator.project_document_tools import (
    DESCRIPTIONS,
    execute,
    include_sources,
)
from services.orchestrator.terminal_client import ProjectPublisher

TITLE = "**Trip** report"
HEADING = "*Summary* of the `week`"
DATE = "**Date**: 12 March, see [the plan](https://example.org/plan)."
WHOLE = "**Everything below is provisional.**"
KEPT = "Kept as typed: 2*3*4, 5 * 3 * 2, *args and **kwargs, __init__, a_b_c."
BULLETS = (
    "**Bold** start and *italic* end",
    "patterns *.txt and *.md",
    "[https://example.org/data](https://example.org/data)",
)
# What a person reads in the file.
DISPLAYED = (
    "Trip report",
    "Summary of the week",
    "Date: 12 March, see the plan (https://example.org/plan).",
    "Everything below is provisional.",
    KEPT,
    "Bold start and italic end",
    "patterns *.txt and *.md",
    "https://example.org/data",
)
MARKERS = ("**Trip**", "*Summary*", "`week`", "**Date**", "[the plan]", "*italic*")

QUOTED = "the **first** clause applies to `all` members"
ABSENT = "see [the charter](https://example.org/charter) of *2024*"


def squash(text: str) -> str:
    return "".join(text.split())


def report(suffix: str) -> Content:
    if suffix == ".pptx":
        fields: dict[str, Any] = dict(
            title=TITLE,
            slides=[
                dict(title=HEADING, bullets=list(BULLETS), notes=DATE),
                dict(title="Notes", bullets=[WHOLE, KEPT]),
            ],
        )
    else:
        fields = dict(
            content="\n\n".join(
                ("# " + TITLE, "## " + HEADING, DATE, WHOLE, KEPT)
                + tuple("- " + bullet for bullet in BULLETS)
            )
        )
    return Content.model_validate(
        normalize(dict(path="report" + suffix, **fields))["content"]
    )


def delivered(value: Content, suffix: str, sources: tuple[str, ...] = ()) -> Any:
    return build_project_document(
        value, suffix, list(sources), max_bytes=16777216, timeout=30
    )


def reopened(raw: bytes, suffix: str) -> str:
    """The text of a delivered file, read back with the library of its format."""
    if suffix == ".docx":
        return "\n".join(p.text for p in Document(BytesIO(raw)).paragraphs)
    if suffix == ".pdf":
        return "\n".join(page.extract_text() for page in PdfReader(BytesIO(raw)).pages)
    if suffix == ".pptx":
        return "\n".join(
            shape.text_frame.text
            for slide in Presentation(BytesIO(raw)).slides
            for shape in slide.shapes
            if shape.has_text_frame
        )
    book = load_workbook(BytesIO(raw))
    try:
        return "\n".join(
            str(cell.value)
            for sheet in book
            for row in sheet.iter_rows()
            for cell in row
            if cell.value is not None
        )
    finally:
        book.close()


class ReadingTests(unittest.TestCase):
    def test_a_text_is_read_as_a_cell_and_a_link_keeps_its_address(self) -> None:
        cases = {
            "**Date**: 12 March": "Date: 12 March",
            "a *short* note with `code`": "a short note with code",
            "see [the plan](https://example.org/plan).": (
                "see the plan (https://example.org/plan)."
            ),
            # A link that shows its own address is written once.
            "[https://example.org/a](https://example.org/a)": "https://example.org/a",
            "[Ana Lorca](mailto:ana@example.org)": "Ana Lorca (ana@example.org)",
            # Not emphasis: products, arguments, patterns, identifiers.
            "2*3*4 and 5 * 3 * 2": "2*3*4 and 5 * 3 * 2",
            "*args, **kwargs": "*args, **kwargs",
            "patterns *.txt and *.md": "patterns *.txt and *.md",
            "__init__ and a_b_c": "__init__ and a_b_c",
            "`a * b * c` is code": "a * b * c is code",
            # A text that starts like a formula is kept as typed.
            "=A1*B1*C1 and **more**": "=A1*B1*C1 and **more**",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(shown(text), expected)
        self.assertEqual(
            [(span.text, span.bold, span.italic) for span in prose(DATE)],
            [
                ("Date", True, False),
                (": 12 March, see the plan (https://example.org/plan).", False, False),
            ],
        )

    def test_the_content_gives_its_texts_as_displayed_and_as_typed(self) -> None:
        value = report(".docx")
        self.assertEqual(value.texts(), DISPLAYED)
        self.assertEqual(value.typed(), (TITLE, HEADING, DATE, WHOLE, KEPT, *BULLETS))

    def test_a_heading_that_repeats_the_title_in_bold_is_written_once(self) -> None:
        value = Content.model_validate(
            dict(
                title="Trip report",
                sections=[dict(heading="**Trip report**", paragraphs=["One line."])],
            )
        )
        self.assertTrue(value.repeats_title())
        for suffix in (".docx", ".pdf", ".xlsx", ".pptx"):
            with self.subTest(suffix=suffix):
                text = reopened(delivered(value, suffix).content, suffix)
                self.assertEqual(squash(text).count("Tripreport"), 1)

    def test_the_tool_says_that_markers_are_read_everywhere(self) -> None:
        self.assertIn(
            "is read in headings, paragraphs, bullets and table cells of every "
            "format, never shown as markers",
            DESCRIPTIONS["publish_document"],
        )


class FormatTests(unittest.TestCase):
    def test_no_format_shows_the_markers(self) -> None:
        for suffix in (".docx", ".pdf", ".xlsx", ".pptx"):
            with self.subTest(suffix=suffix):
                document = delivered(report(suffix), suffix)
                for text in (
                    reopened(document.content, suffix),
                    document.preview.decode(),
                ):
                    found = squash(text)
                    for line in DISPLAYED:
                        self.assertIn(squash(line), found)
                    for marker in MARKERS:
                        self.assertNotIn(squash(marker), found)

    def test_a_document_writes_emphasis_as_runs(self) -> None:
        document = Document(BytesIO(delivered(report(".docx"), ".docx").content))
        runs = {
            paragraph.text: [
                (run.text, bool(run.bold), bool(run.italic)) for run in paragraph.runs
            ]
            for paragraph in document.paragraphs
        }
        self.assertEqual(
            runs[DISPLAYED[2]],
            [
                ("Date", True, False),
                (": 12 March, see the plan (https://example.org/plan).", False, False),
            ],
        )
        self.assertEqual(runs[DISPLAYED[3]], [(DISPLAYED[3], True, False)])
        self.assertEqual(runs[KEPT], [(KEPT, False, False)])
        self.assertEqual(
            runs["Bold start and italic end"],
            [
                ("Bold", True, False),
                (" start and ", False, False),
                ("italic", False, True),
                (" end", False, False),
            ],
        )
        # A heading keeps the weight of its style and takes italics.
        self.assertEqual(
            runs["Trip report"], [("Trip", False, False), (" report", False, False)]
        )
        self.assertEqual(
            runs["Summary of the week"],
            [("Summary", False, True), (" of the week", False, False)],
        )
        # The title of the file properties is the one displayed.
        self.assertEqual(document.core_properties.title, "Trip report")
        styles = {p.text: p.style.name for p in document.paragraphs if p.style}
        self.assertEqual(styles["Trip report"], "Title")
        self.assertEqual(styles["Summary of the week"], "Heading 1")
        self.assertEqual(styles["patterns *.txt and *.md"], "List Bullet")

    def test_a_workbook_styles_a_text_that_is_emphasised_as_a_whole(self) -> None:
        book = load_workbook(BytesIO(delivered(report(".xlsx"), ".xlsx").content))
        self.addCleanup(book.close)
        cells = {cell.value: cell for row in book["1"].iter_rows() for cell in row}
        self.assertEqual(list(cells), list(DISPLAYED))
        self.assertTrue(cells[DISPLAYED[3]].font.b)
        self.assertFalse(cells[DISPLAYED[2]].font.b)
        self.assertFalse(cells["Bold start and italic end"].font.i)
        # A text that is one link opens it.
        link = cells["https://example.org/data"].hyperlink
        assert link is not None
        self.assertEqual(link.target, "https://example.org/data")
        self.assertIsNone(cells[DISPLAYED[2]].hyperlink)
        self.assertEqual({cell.data_type for cell in cells.values()}, {"s"})
        self.assertEqual(book.properties.title, "Trip report")


def work_tools(segments: list[str]) -> Mock:
    """The tools of one Work turn, with one selected source to read."""
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
    tools.request_text = "write the notes"
    tools.calculation_refusals = set()
    tools.workspace.invoke = AsyncMock(
        return_value=dict(
            coverage=dict(complete=True),
            segments=[dict(text=text) for text in segments],
        )
    )
    return tools


class SourceCoverageTests(unittest.IsolatedAsyncioTestCase):
    def notes(self, suffix: str) -> Content:
        return Content.model_validate(
            normalize(
                dict(
                    path="notes" + suffix,
                    content=f"# Notes\n\nAs agreed, {QUOTED}.\n\n- one *open* point\n",
                )
            )["content"]
        )

    def test_an_excerpt_typed_with_markers_is_covered_and_shown_without(self) -> None:
        for suffix in (".docx", ".pdf", ".xlsx"):
            with self.subTest(suffix=suffix):
                value = self.notes(suffix)
                # Quoted as typed in a paragraph: nothing is appended.
                self.assertEqual(include_sources(value, [QUOTED]), value)
                annexed = include_sources(value, [QUOTED, ABSENT])
                self.assertEqual(annexed.sections[-1].heading, "Sources")
                self.assertEqual(annexed.sections[-1].paragraphs, (ABSENT,))
                # The check of source coverage accepts both excerpts as typed,
                # and as displayed.
                read_back = "the first clause applies to all members"
                for sources in ((QUOTED, ABSENT), (read_back,)):
                    document = delivered(annexed, suffix, sources)
                found = squash(reopened(document.content, suffix))
                self.assertIn(squash(read_back), found)
                self.assertIn(
                    squash("see the charter (https://example.org/charter) of 2024"),
                    found,
                )
                for marker in ("**first**", "`all`", "[the charter]", "*2024*"):
                    self.assertNotIn(marker, found)
                    self.assertNotIn(marker, squash(document.preview.decode()))
                # An excerpt that is nowhere in the document is still refused.
                with self.assertRaisesRegex(
                    ValueError, "Source coverage verification failed"
                ):
                    delivered(annexed, suffix, ("a clause that nobody quoted",))
                with self.assertRaisesRegex(
                    ValueError, "Source coverage verification failed"
                ):
                    delivered(value, suffix, (ABSENT,))

    async def test_publication_appends_the_missing_excerpt_and_passes(self) -> None:
        write = AsyncMock(
            side_effect=lambda document, name, *rest: dict(
                id=str(uuid4()), file_version_id=str(uuid4()), relative_path=name
            )
        )
        version = str(uuid4())
        resolved = dict(file_id=str(uuid4()), version=version, extension=".docx")
        for suffix in (".docx", ".pdf", ".xlsx"):
            tools = work_tools([QUOTED, ABSENT])
            arguments = dict(
                path="notes" + suffix,
                content=f"# Notes\n\nAs agreed, {QUOTED}.\n",
                sources=[dict(file_id=resolved["file_id"], version=version)],
            )
            with (
                self.subTest(suffix=suffix),
                patch.object(ProjectPublisher, "publish", write),
                patch(
                    "services.orchestrator.project_document_tools.resolve",
                    AsyncMock(return_value=resolved),
                ),
                patch(
                    "services.orchestrator.file_results.project_deliverable_links",
                    return_value="links",
                ),
            ):
                result = await execute(
                    tools, "publish_document", json.dumps(arguments), 60
                )
                self.assertEqual(result["text"], "links")
                written = write.await_args
                assert written is not None
                found = squash(reopened(written.args[0].content, suffix))
                self.assertIn(squash("the first clause applies to all members"), found)
                self.assertIn("Sources", found)
                self.assertIn(
                    squash("see the charter (https://example.org/charter) of 2024"),
                    found,
                )
                self.assertNotIn("**", found)
                self.assertNotIn("`", found)


if __name__ == "__main__":
    unittest.main()
