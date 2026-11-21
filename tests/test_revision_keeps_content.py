"""A change of a delivered file starts from its stored version and keeps the rest.

Staging 2026-11-20 (after PR 588): "write a report as a Word document", then
"change the title", then "add a table" gave versions of one file, but each
was written again from the conversation, so what was not asked could drift.
The writer now receives the current version read from the stored bytes
(current_file.py, readback.py), and each new version is compared with it
before it is claimed (deliverable_check.py). The files here are real files
built by the renderer, with fictional content.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock

from packages.doc_render import readback
from packages.doc_render.schema import Content
from services.orchestrator import current_file, deliverable_check
from services.orchestrator.presentation_delivery import build_project_document
from services.orchestrator.project_document_input import normalize

REPORT = """Our orchard cooperative grew from four to eleven members this season, and the cider press now runs two days a week.

## Harvest

The harvest reached 3 400 kg of apples, most of them Reinette.

- Picking started on 14 September
- The last crates were stored on 2 October

## Members

Ada Fernhill coordinates the picking teams and Bruno Calvel keeps the accounts.
"""
DECK = [
    {"title": "Orchard season", "bullets": ["Eleven members", "Two press days a week"]},
    {
        "title": "Budget",
        "bullets": ["Seeds 1 250 €", "Tools 300 €"],
        "notes": "Figures for the next season.",
    },
    {"title": "Next steps", "bullets": ["Plant 40 trees", "Open a shop on Saturdays"]},
]
SHEET = """## Costs

| Item | Cost |
| --- | --- |
| Seeds | 1 250,00 € |
| Tools | 300 |
| Total | =SUM(B2:B3) |

Prices include delivery.
"""


def build(arguments: dict[str, Any], suffix: str) -> bytes:
    value = normalize(dict(arguments, path="file" + suffix, sources=[]))
    content = Content.model_validate(value["content"])
    return build_project_document(
        content, suffix, [], max_bytes=16 * 1024 * 1024, timeout=60, pictures={}
    ).content


def current(raw: bytes, suffix: str, name: str = "") -> current_file.CurrentFile:
    return current_file.CurrentFile(
        name or "report" + suffix,
        suffix,
        "file-id",
        "version-id",
        readback.read(raw, suffix),
    )


def edited(raw: bytes, suffix: str) -> dict[str, Any]:
    """What the writer starts from: the arguments read back from the file."""
    value: dict[str, Any] = json.loads(
        json.dumps(readback.editable(readback.read(raw, suffix)))
    )
    return value


class ReadbackTests(unittest.TestCase):
    def test_each_format_reads_back_to_the_same_file(self) -> None:
        """The current version, written back unchanged, is the same file."""
        long_table = "\n".join(
            ["| Row | Value |", "| --- | --- |"]
            + [f"| Plot {n} | {n * 7} |" for n in range(1, 46)]
        )
        long_text = " ".join(["The cooperative keeps a record of every crate."] * 40)
        cases: dict[str, dict[str, Any]] = {
            ".docx": {"title": "Orchard report", "content": REPORT},
            ".pdf": {"title": "Orchard report", "content": REPORT},
            # Several pages: a table and a text continued on the next page.
            ".pdf ": {
                "title": "Long report",
                "content": f"{long_text}\n\n## Plots\n\n{long_table}\n\n## End\n\n"
                + long_text,
            },
            ".xlsx": {"title": "Orchard costs", "content": SHEET},
            ".pptx": {"title": "Orchard plan", "slides": DECK},
            ".md": {"content": "# Plan\n\nPlant 40 trees.\n"},
        }
        for key, arguments in cases.items():
            suffix = key.strip()
            with self.subTest(case=key):
                if suffix == ".md":
                    raw = arguments["content"].encode()
                    written = readback.editable(readback.read(raw, suffix))
                    self.assertEqual(written, arguments)
                    continue
                raw = build(arguments, suffix)
                again = build(edited(raw, suffix), suffix)
                self.assertEqual(
                    hashlib.sha256(again).hexdigest(), hashlib.sha256(raw).hexdigest()
                )

    def test_a_deck_continued_on_the_next_slide_is_one_slide_to_edit(self) -> None:
        bullets = [
            f"Plot {n} needs new trees and a fence this winter" for n in range(8)
        ]
        deck = [{"title": "Plots", "bullets": bullets, "notes": " ".join(bullets)}]
        raw = build({"title": "Plots", "slides": deck}, ".pptx")
        document = readback.read(raw, ".pptx")
        self.assertEqual(len(document.sections), 1)
        self.assertEqual(document.sections[0].bullets, bullets)

    def test_figures_are_written_back_as_the_workbook_shows_them(self) -> None:
        self.assertEqual(readback.number_text(1250, '#,##0.00 "€"'), "1,250.00 €")
        self.assertEqual(readback.number_text(0.125, "0.0%"), "12.5%")
        self.assertEqual(readback.number_text(-300, '"$"#,##0'), "-$300")
        self.assertEqual(readback.number_text(7.0, "General"), "7")


class ChangesTests(unittest.TestCase):
    def test_a_title_change_keeps_the_body(self) -> None:
        raw = build({"title": "Orchard report", "content": REPORT}, ".docx")
        arguments = edited(raw, ".docx")
        self.assertEqual(arguments["title"], "Orchard report")
        self.assertIn("3 400 kg", arguments["content"])
        arguments["title"] = "Season review"
        changes = current(raw, ".docx").changes(build(arguments, ".docx"))
        self.assertEqual(
            changes["replaced"],
            [{"before": "Orchard report", "after": "Season review", "following": ""}],
        )
        self.assertEqual((changes["removed"], changes["added"]), ([], []))
        # The body, word for word.
        after = readback.read(build(arguments, ".docx"), ".docx")
        self.assertEqual(after.blocks()[1:], readback.read(raw, ".docx").blocks()[1:])

    def test_adding_a_table_keeps_the_earlier_title_change(self) -> None:
        first = build({"title": "Orchard report", "content": REPORT}, ".docx")
        arguments = edited(first, ".docx")
        arguments["title"] = "Season review"
        second = build(arguments, ".docx")
        # The next turn starts from the second version, not from the request
        # of the first one: the title change is in what the writer receives.
        context = current(second, ".docx").context("add a table of the harvest")
        self.assertEqual(context["current"]["title"], "Season review")
        start = context["current"]
        start["content"] += (
            "\n\n| Variety | Kg |\n| --- | --- |\n| Reinette | 2 100 |\n| Gala | 1 300 |\n"
        )
        third = build(start, ".docx")
        changes = current(second, ".docx").changes(third)
        self.assertEqual((changes["removed"], changes["replaced"]), ([], []))
        self.assertIn("Reinette | 2 100", changes["added"][0]["text"])
        self.assertEqual(readback.read(third, ".docx").title, "Season review")
        # Written again from the conversation instead, the earlier change is
        # undone: the comparison names it.
        drifted = build(
            {
                "title": "Orchard report",
                "content": REPORT
                + "\n| Variety | Kg |\n| --- | --- |\n| Reinette | 2 100 |\n",
            },
            ".docx",
        )
        undone = current(second, ".docx").changes(drifted)
        self.assertEqual(undone["replaced"][0]["before"], "Season review")
        self.assertEqual(undone["replaced"][0]["after"], "Orchard report")

    def test_a_figure_change_in_one_slide_keeps_the_other_slides(self) -> None:
        raw = build({"title": "Orchard plan", "slides": DECK}, ".pptx")
        arguments = edited(raw, ".pptx")
        self.assertEqual(arguments["slides"], DECK)
        arguments["slides"][1]["bullets"][0] = "Seeds 1 400 €"
        new = build(arguments, ".pptx")
        changes = current(raw, ".pptx", "plan.pptx").changes(new)
        self.assertEqual(
            [(c["before"], c["after"]) for c in changes["replaced"]],
            [("250", "400")],
        )
        before, after = readback.read(raw, ".pptx"), readback.read(new, ".pptx")
        for index in (0, 2):
            self.assertEqual(
                before.sections[index].blocks(), after.sections[index].blocks()
            )

    def test_a_request_in_another_language_changes_one_cell(self) -> None:
        """« Mets 320 € pour les outils » on a workbook: one cell changes."""
        raw = build({"title": "Coûts du verger", "content": SHEET}, ".xlsx")
        file = current(raw, ".xlsx", "couts.xlsx")
        context = file.context("Mets 320 € pour les outils, le reste ne bouge pas")
        self.assertIn("| Tools | 300 |", context["current"]["content"])
        self.assertIn("| Total | =SUM(B2:B3) |", context["current"]["content"])
        start = context["current"]
        start["content"] = start["content"].replace(
            "| Tools | 300 |", "| Tools | 320 € |"
        )
        changes = file.changes(build(start, ".xlsx"))
        self.assertEqual(
            [(c["before"], c["after"]) for c in changes["replaced"]], [("300", "320 €")]
        )
        self.assertEqual((changes["removed"], changes["added"]), ([], []))

    def test_layout_and_typography_are_not_changes(self) -> None:
        raw = build({"title": "Orchard report", "content": REPORT}, ".pdf")
        retyped = REPORT.replace("this season, and", "this season,   and").replace(
            "Ada Fernhill", "Ada Fernhill"
        )
        changes = current(raw, ".pdf").changes(
            build({"title": "Orchard report", "content": retyped}, ".pdf")
        )
        self.assertTrue(changes["identical"])

    def test_a_moved_part_is_not_a_loss(self) -> None:
        raw = build({"title": "Orchard report", "content": REPORT}, ".docx")
        harvest, members = REPORT.split("## Harvest")[1].split("## Members")
        moved = (
            REPORT.split("## Harvest")[0]
            + "## Members"
            + members.rstrip()
            + "\n\n## Harvest"
            + harvest
        )
        changes = current(raw, ".docx").changes(
            build({"title": "Orchard report", "content": moved}, ".docx")
        )
        self.assertEqual(changes["removed"], [])
        self.assertTrue(changes["moved"])


class BoundTests(unittest.TestCase):
    def test_a_long_file_gives_its_outline_and_the_parts_the_request_names(
        self,
    ) -> None:
        sections = "\n\n".join(
            f"## Plot {n}\n\n"
            + " ".join([f"Plot {n} keeps its old trees and gets a new fence."] * 30)
            for n in range(1, 41)
        )
        raw = build({"title": "Plots", "content": sections}, ".docx")
        file = current(raw, ".docx")
        # French request naming a part by its heading.
        context = file.context("Dans « Plot 37 », remplace la clôture par une haie")
        self.assertNotIn("current", context)
        self.assertEqual(len(context["outline"]), 40)
        self.assertIn("too long to show in full", context["instruction"])
        shown = [part["part"] for part in context["parts"]]
        self.assertIn(37, shown)
        size = len(json.dumps(context["parts"], ensure_ascii=False))
        self.assertLessEqual(size, current_file.PARTS_CHARACTERS)
        self.assertLessEqual(
            len(json.dumps(context["outline"], ensure_ascii=False)),
            current_file.OUTLINE_CHARACTERS,
        )

    def test_a_short_file_is_given_whole(self) -> None:
        raw = build({"title": "Orchard report", "content": REPORT}, ".docx")
        context = current(raw, ".docx").context("change the title")
        self.assertEqual(context["name"], "report.docx")
        self.assertIn("apply only the change", context["instruction"])
        self.assertNotIn("outline", context)


def turn_tools(request: str) -> SimpleNamespace:
    model = SimpleNamespace(
        profile="atlas-qwen", strict=False, spent=Decimal(0), allowance=Decimal("0.1")
    )
    return SimpleNamespace(
        check_model=model,
        request_text=request,
        earlier_requests=["Write a report on the orchard season as a Word document"],
        deliverable_checks=0,
        deliverable_refused=False,
    )


class CheckTests(unittest.IsolatedAsyncioTestCase):
    async def review(
        self, request: str, before: bytes, after_arguments: dict[str, Any], reply: Any
    ) -> tuple[Any, SimpleNamespace, AsyncMock]:
        tools = turn_tools(request)
        after = build(after_arguments, ".docx")
        value = normalize(dict(after_arguments, path="report.docx", sources=[]))
        content = Content.model_validate(value["content"])
        complete = AsyncMock(
            side_effect=TimeoutError() if reply is None else [dict(reply)]
        )
        revision = current(before, ".docx").changes(after)
        outcome = await deliverable_check.review(
            tools,
            ".docx",
            content,
            after,
            b"text",
            [],
            complete=complete,
            revision=revision,
        )
        return outcome, tools, complete

    async def test_an_unrequested_change_is_judged_and_refused_once(self) -> None:
        before = build({"title": "Orchard report", "content": REPORT}, ".docx")
        drifted = REPORT.replace("3 400 kg", "3 200 kg")
        gap = 'the harvest figure "3 400 kg" was changed to "3 200 kg" without being asked'
        outcome, tools, complete = await self.review(
            "Change the title to Season review",
            before,
            {"title": "Season review", "content": drifted},
            {"text": json.dumps({"gaps": [gap], "variants": []}), "cost_eur": "0.0004"},
        )
        awaited = complete.await_args
        assert awaited is not None
        payload = awaited.args[0]
        self.assertIn("new version of a file", payload["messages"][0]["content"])
        user = payload["messages"][1]["content"]
        data = json.loads(
            user if isinstance(user, str) else user[0]["text"].split("\n(")[0]
        )
        replaced = data["facts"]["changes_from_previous_version"]["replaced"]
        self.assertIn(("400", "200"), [(r["before"], r["after"]) for r in replaced])
        self.assertEqual(outcome.gaps, [gap])
        with self.assertRaises(ValueError) as refused:
            deliverable_check.decide(tools, outcome)
        self.assertIn("3 400 kg", str(refused.exception))
        # The repair still lacking it is published with a note to say so.
        self.assertIn("must say so plainly", deliverable_check.decide(tools, outcome))

    async def test_without_the_check_the_answer_names_the_changes(self) -> None:
        before = build({"title": "Orchard report", "content": REPORT}, ".docx")
        drifted = REPORT.replace(
            "Bruno Calvel keeps the accounts", "Bruno Calvel helps"
        )
        outcome, tools, _ = await self.review(
            "Change the title to Season review",
            before,
            {"title": "Season review", "content": drifted},
            None,
        )
        self.assertFalse(outcome.checked)
        note = deliverable_check.decide(tools, outcome)
        self.assertIn('replaced "Orchard report" with "Season review"', note)
        self.assertIn("keeps the accounts", note)
        self.assertIn("must not claim that everything else was kept", note)
        self.assertEqual(deliverable_check.verification(outcome)["status"], "not_run")

    async def test_an_unchanged_version_is_refused_once(self) -> None:
        before = build({"title": "Orchard report", "content": REPORT}, ".docx")
        outcome, tools, _ = await self.review(
            "Add a table of the harvest",
            before,
            {"title": "Orchard report", "content": REPORT},
            {"text": json.dumps({"gaps": [], "variants": []}), "cost_eur": "0.0004"},
        )
        self.assertIn("identical to its current version", outcome.certain[0])
        with self.assertRaises(ValueError):
            deliverable_check.decide(tools, outcome)

    async def test_the_requested_change_alone_passes(self) -> None:
        before = build({"title": "Orchard report", "content": REPORT}, ".docx")
        outcome, tools, _ = await self.review(
            "Change the title to Season review",
            before,
            {"title": "Season review", "content": REPORT},
            {"text": json.dumps({"gaps": [], "variants": []}), "cost_eur": "0.0004"},
        )
        self.assertEqual(deliverable_check.decide(tools, outcome), "")
        self.assertEqual(deliverable_check.verification(outcome)["status"], "passed")


class LoadTests(unittest.IsolatedAsyncioTestCase):
    def tools(self, raw: bytes, sha: str | None = None) -> SimpleNamespace:
        project, folder, file_id, version = "p-1", "f-1", "file-1", "version-1"
        link = f"[Download report.docx](/api/atlas/orgs/o/projects/{project}/files/{file_id})"

        async def public(method: str, path: str) -> tuple[int, dict[str, Any]]:
            if path.startswith("files?"):
                return 200, {
                    "items": [
                        dict(
                            id=file_id,
                            folder_id=folder,
                            relative_path="report.docx",
                            version_id=version,
                            sha256=sha,
                        )
                    ]
                }
            return 200, dict(
                id=file_id,
                version_id=version,
                relative_path="report.docx",
                size_bytes=len(raw),
                sha256=sha or hashlib.sha256(raw).hexdigest(),
            )

        broker = SimpleNamespace(
            project=project, public=public, content=AsyncMock(return_value=raw)
        )
        return SimpleNamespace(
            revises_delivered=True,
            numbered_copies=True,
            earlier_answers=["Here is your report. " + link],
            requested_suffix=None,
            execution=SimpleNamespace(broker=broker),
        )

    async def test_the_file_is_read_from_its_stored_version(self) -> None:
        raw = build({"title": "Orchard report", "content": REPORT}, ".docx")
        tools = self.tools(raw)
        loaded = await current_file.load(tools, {"deliverables_folder_id": "f-1"})
        assert loaded is not None
        self.assertEqual((loaded.name, loaded.version_id), ("report.docx", "version-1"))
        self.assertEqual(loaded.document.title, "Orchard report")
        tools.execution.broker.content.assert_awaited_once_with(
            "file-1", "version-1", readback.MAX_BYTES
        )

    async def test_bytes_that_do_not_match_their_record_are_not_used(self) -> None:
        raw = build({"title": "Orchard report", "content": REPORT}, ".docx")
        tools = self.tools(raw, sha="0" * 64)
        self.assertIsNone(
            await current_file.load(tools, {"deliverables_folder_id": "f-1"})
        )

    async def test_a_new_request_reads_nothing(self) -> None:
        raw = build({"title": "Orchard report", "content": REPORT}, ".docx")
        tools = self.tools(raw)
        tools.revises_delivered = False
        self.assertIsNone(
            await current_file.load(tools, {"deliverables_folder_id": "f-1"})
        )
        tools.execution.broker.content.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
