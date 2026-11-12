"""REQ-PROJ-05: a workbook asked to calculate holds the formulas that do it,
and a heading that repeats the title is written once."""

from io import BytesIO
import json
import tempfile
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from docx import Document
from openpyxl import load_workbook
from pptx import Presentation
from pypdf import PdfReader

from packages.doc_render import render
from packages.doc_render.calculated import (
    COLUMN,
    MISSING,
    ROW,
    Finding,
    asks_calculation,
    finding,
    names_derived,
)
from packages.doc_render.schema import Content
from services.orchestrator.project_document_input import normalize
from services.orchestrator.project_document_tools import (
    DESCRIPTIONS,
    calculation_refusal,
    execute,
)
from services.orchestrator.terminal_client import ProjectPublisher
from services.orchestrator.tool_failures import NOTE_KEY, RepeatedFailures, error_code

# The two requests of the corpus and what was delivered for them.
RUNNING = (
    "Create an Excel spreadsheet to track my running: date, distance, time, "
    "pace calculated automatically"
)
BUDGET = (
    "crée un tableur excel pour suivre mon budget mensuel avec les catégories "
    "loyer, courses, transport, loisirs et les totaux"
)
RUNNING_HEADER = (
    "# Running Tracker\n"
    "| Date | Distance (km) | Time (min) | Pace (min/km) |\n"
    "| --- | --- | --- | --- |\n"
)
RUNNING_EMPTY = RUNNING_HEADER + "| | | | |\n" * 10
RUNNING_CALCULATED = (
    RUNNING_HEADER
    + '| 2026-03-01 | 10 | 55 | =IFERROR(C2/B2,"") |\n'
    + "".join(f'| | | | =IFERROR(C{row}/B{row},"") |\n' for row in range(3, 12))
)
BUDGET_HEADER = (
    "# Budget Mensuel\n"
    "| Catégorie | Budget prévu | Dépense réelle | Différence |\n"
    "| --- | --- | --- | --- |\n"
)
BUDGET_TOTALS = "| Total | =SUM(B2:B5) | =SUM(C2:C5) | =SUM(D2:D5) |\n"
BUDGET_EMPTY_DIFFERENCE = (
    BUDGET_HEADER
    + "| Loyer | 800 | 780 | |\n| Courses | | | |\n"
    + "| Transport | | | |\n| Loisirs | | | |\n"
    + BUDGET_TOTALS
)
BUDGET_CALCULATED = (
    BUDGET_HEADER
    + '| Loyer | 800 | 780 | =IF(B2="","",B2-C2) |\n'
    + '| Courses | | | =IF(B3="","",B3-C3) |\n'
    + '| Transport | | | =IF(B4="","",B4-C4) |\n'
    + '| Loisirs | | | =IF(B5="","",B5-C5) |\n'
    + BUDGET_TOTALS
)
BOOKS = (
    "# Books\n| Title | Author | Year |\n| --- | --- | --- |\n"
    "| The Glass Orchard | Mira Dalen | 2019 |\n| | | |\n"
)


def content(
    markdown: str, *, formulas: bool = False, path: str = "sheet.xlsx"
) -> Content:
    fields: dict[str, Any] = dict(path=path, content=markdown)
    if formulas:
        fields["formulas"] = True
    return Content.model_validate(normalize(fields)["content"])


class RequestWordsTests(unittest.TestCase):
    def test_requests_that_ask_for_calculated_values(self) -> None:
        for request in (
            RUNNING,
            BUDGET,
            "fais moi un excel pr mes depenses avec le total par mois",
            "un tableau des notes avec la moyenne de chaque élève stp",
            "tableur de suivi conso, calculé automatiquement",
            "n'oublie pas les totaux",
            "do not forget the totals",
            "a sheet with a subtotal per category and a formula for the balance",
            "Erstelle eine Excel-Tabelle für meine Ausgaben mit Summe",
            "Haushaltsbuch, der Durchschnitt soll automatisch berechnet werden",
            "hoja de cálculo de gastos con el promedio y los totales",
            "foglio excel delle spese con il totale calcolato",
            "No, calculate the total this time",
        ):
            with self.subTest(request=request):
                self.assertTrue(asks_calculation(request))

    def test_requests_that_ask_for_none(self) -> None:
        for request in (
            "Create an Excel list of my books: title, author, year",
            "liste de courses en excel",
            "crée un formulaire d'inscription dans un tableur",
            "a content plan for our social media",
            "nous sommes trois colocataires, fais un tableau des tâches",
            "spreadsheet of my contacts, no formulas",
            "an inventory template without any totals or formulas",
            "un tableau simple, sans formules ni totaux",
            "pas de calcul automatique, juste la liste",
            "Tabelle ohne Formeln",
            "una tabla sin fórmulas",
            "un elenco senza formule",
            "",
        ):
            with self.subTest(request=request):
                self.assertFalse(asks_calculation(request))

    def test_headers_that_name_a_derived_quantity(self) -> None:
        for header in (
            "Pace (min/km)",
            "Différence",
            "Janvier Diff",
            "**Total**",
            "Sous-total",
            "Moyenne",
            "Écart",
            "Solde",
            "Reste à payer",
            "Summe",
            "Durchschnitt",
            "Promedio",
            "Diferencia",
            "Totale",
            "Percentuale",
            "% of total",
        ):
            with self.subTest(header=header):
                self.assertTrue(names_derived(header))
        for header in (
            "Date",
            "Distance (km)",
            "Time (min)",
            "Budget prévu",
            "Catégorie",
            "Media",
            "Tempo (min)",
            "Rest (min)",
            "Notes",
            "",
        ):
            with self.subTest(header=header):
                self.assertFalse(names_derived(header))


class FindingTests(unittest.TestCase):
    def test_requested_calculation_without_any_formula(self) -> None:
        found = finding(content(RUNNING_EMPTY), RUNNING)
        assert found is not None
        self.assertEqual(found.code, MISSING)
        self.assertEqual(found.columns, (("1", "D", "Pace (min/km)"),))
        self.assertFalse(found.unflagged)
        # Formulas typed without formulas=true are text in the file.
        typed = RUNNING_HEADER + "| 2026-03-01 | 10 | 55 | =C2/B2 |\n"
        found = finding(content(typed), RUNNING)
        assert found is not None
        self.assertEqual((found.code, found.unflagged), (MISSING, True))
        self.assertIn("only with formulas=true", calculation_refusal(found))

    def test_derived_column_left_empty_beside_formulas(self) -> None:
        found = finding(content(BUDGET_EMPTY_DIFFERENCE, formulas=True), BUDGET)
        self.assertEqual(found, Finding(COLUMN, "1", "D", "Différence"))
        # A later turn asks for no calculation: the table holds formulas.
        months = (
            "# Budget\n"
            "| Catégorie | Janvier Budget | Janvier Réel | Janvier Diff "
            "| Février Budget | Février Réel | Février Diff |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "| Loyer | | | | | | |\n| Courses | | | | | | |\n"
            "| TOTAL | =SUM(B2:B3) | =SUM(C2:C3) | =SUM(D2:D3) | =SUM(E2:E3) "
            "| =SUM(F2:F3) | =SUM(G2:G3) |\n"
        )
        found = finding(
            content(months, formulas=True), "ajoute une colonne pour février"
        )
        self.assertEqual(found, Finding(COLUMN, "1", "D", "Janvier Diff"))

    def test_amount_column_is_found_only_for_a_calculation_request(self) -> None:
        invoices = content(
            "# Invoices\n| Invoice | Date | Total |\n| --- | --- | --- |\n"
            "| | | |\n| | | |\n| Sum | | =SUM(C2:C3) |\n",
            formulas=True,
        )
        # The total of an invoice is typed: a total under it does not say more.
        self.assertIsNone(finding(invoices, "add two more rows"))
        self.assertEqual(
            finding(invoices, "three columns and the total calculated per invoice"),
            Finding(COLUMN, "1", "C", "Total"),
        )

    def test_total_row_left_empty_under_its_rows(self) -> None:
        table = (
            "# Trips\n| Trip | Km | Litres | Consumption |\n| --- | --- | --- | --- |\n"
            '| North | 120 | 8 | =IFERROR(C2/B2*100,"") |\n'
            '| South | 300 | 19 | =IFERROR(C3/B3*100,"") |\n'
            "| **Total** | | | |\n"
        )
        found = finding(content(table, formulas=True), "add the south trip")
        assert found is not None
        self.assertEqual(found, Finding(ROW, "1", "4", "Total"))
        self.assertIn("=SUM(B2:B3) in column B", calculation_refusal(found))

    def test_files_that_calculate_are_not_found(self) -> None:
        self.assertIsNone(finding(content(RUNNING_CALCULATED, formulas=True), RUNNING))
        self.assertIsNone(finding(content(BUDGET_CALCULATED, formulas=True), BUDGET))

    def test_lists_and_templates_without_a_calculation_request(self) -> None:
        cases = (
            # A plain list, and the same template when no calculation is asked.
            (BOOKS, False, "Create an Excel list of my books: title, author, year"),
            (
                RUNNING_EMPTY,
                False,
                "Create an Excel spreadsheet to log my runs: date, distance, time, pace",
            ),
            (RUNNING_EMPTY, False, "a running log template, no formulas"),
            # A derived column that holds typed values or formulas.
            (
                "# Sales\n| Item | Jan | Feb | Total |\n| --- | --- | --- | --- |\n"
                "| Pens | 3 | 4 | 7 |\n| All | =SUM(B2:B2) | =SUM(C2:C2) | 7 |\n",
                True,
                "",
            ),
            # The first column holds the labels of the rows.
            (
                "# Sums\n| Total | Jan |\n| --- | --- |\n| | 3 |\n| | =SUM(B2:B2) |\n",
                True,
                "",
            ),
            # A total taken from another sheet is content, not an empty column.
            (
                "# Year\n## Summary\n| Month | Total |\n| --- | --- |\n"
                "| Jan | =2!B2 |\n| Feb | |\n"
                "## Detail\n| Day | Amount |\n| --- | --- |\n| 1 | 12 |\n",
                True,
                "",
            ),
        )
        for markdown, formulas, request in cases:
            with self.subTest(markdown=markdown[:30], request=request):
                self.assertIsNone(
                    finding(content(markdown, formulas=formulas), request)
                )
        # A workbook without a table has nothing to calculate with.
        notes = Content.model_validate(
            dict(title="Notes", sections=[dict(heading="Notes", paragraphs=["One"])])
        )
        self.assertIsNone(finding(notes, RUNNING))


def work_tools(request: str) -> Mock:
    """The tools of one Work turn for this request, with a project to publish in."""
    execution = Mock()
    execution.check = AsyncMock()
    execution.session = dict(base_file_versions=[])

    async def public(method: str, path: str) -> tuple[int, dict[str, Any]]:
        if path == "folders":
            return 200, dict(items=[dict(id="folder", revision=0)])
        return 200, dict(deliverables_folder_id="folder")

    execution.broker.public = AsyncMock(side_effect=public)
    tools = Mock(execution=execution)
    tools.deliverables = []
    tools.requested_suffix = None
    tools.numbered_copies = False
    tools.request_text = request
    tools.calculation_refusals = set()
    return tools


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.write = AsyncMock(
            side_effect=lambda document, name, *rest: dict(
                id=str(uuid4()), file_version_id=str(uuid4()), relative_path=name
            )
        )
        for patcher in (
            patch.object(ProjectPublisher, "publish", self.write),
            patch(
                "services.orchestrator.file_results.project_deliverable_links",
                return_value="links",
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def publish(
        self,
        tools: Mock,
        markdown: str,
        *,
        formulas: bool = False,
        path: str = "sheet.xlsx",
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = dict(path=path, content=markdown, sources=[])
        if formulas:
            arguments["formulas"] = True
        return await execute(tools, "publish_document", json.dumps(arguments), 60)

    async def refused(self, tools: Mock, markdown: str, **options: Any) -> str:
        with self.assertRaises(ValueError) as caught:
            await self.publish(tools, markdown, **options)
        message = str(caught.exception)
        self.assertNotIn("\n", message)
        return message

    def workbook(self, *, values: bool = False) -> Any:
        """The last file handed to the publisher, reopened."""
        written = self.write.await_args
        assert written is not None
        raw = written.args[0].content
        book = load_workbook(BytesIO(raw), data_only=values)
        self.addCleanup(book.close)
        return book["1"]

    async def test_running_tracker_is_refused_once_then_calculates(self) -> None:
        tools = work_tools(RUNNING)
        message = await self.refused(tools, RUNNING_EMPTY)
        self.assertTrue(message.startswith("formulas_requested_but_missing: "))
        self.assertIn('column D "Pace (min/km)" of sheet 1', message)
        self.assertIn('=IFERROR(C2/B2,"")', message)
        self.assertIn("rows left empty to fill in included", message)
        # Nothing was fetched, rendered or written for the refused file.
        tools.execution.broker.public.assert_not_awaited()
        self.write.assert_not_awaited()

        result = await self.publish(tools, RUNNING_CALCULATED, formulas=True)
        self.assertNotIn("note", result)
        sheet, cached = self.workbook(), self.workbook(values=True)
        for row in range(2, 12):
            self.assertEqual(sheet.cell(row, 4).data_type, "f")
            self.assertEqual(sheet.cell(row, 4).value, f'=IFERROR(C{row}/B{row},"")')
        self.assertEqual(cached["D2"].value, 5.5)
        # A row still to fill in shows nothing, as in Excel.
        self.assertIn(cached["D3"].value, (None, ""))
        # Under the table, the title that is also the heading is written once.
        self.assertEqual(
            (sheet["A13"].value, sheet["A14"].value), ("Running Tracker", None)
        )

    async def test_the_same_file_sent_again_is_published_without_a_claim(self) -> None:
        tools = work_tools(RUNNING)
        await self.refused(tools, RUNNING_EMPTY)
        result = await self.publish(tools, RUNNING_EMPTY)
        self.assertIn("holds no formula and calculates nothing", result["note"])
        self.assertIn("must not describe a formula", result["note"])
        self.assertFalse(
            any(cell.data_type == "f" for row in self.workbook() for cell in row)
        )
        # The reason is not given twice in a turn; a new turn starts again.
        await self.publish(tools, RUNNING_EMPTY, path="again.xlsx")
        self.assertEqual(self.write.await_count, 2)
        message = await self.refused(work_tools(RUNNING), RUNNING_EMPTY)
        self.assertTrue(message.startswith("formulas_requested_but_missing: "))

    async def test_budget_difference_is_named_then_calculated(self) -> None:
        tools = work_tools(BUDGET)
        message = await self.refused(tools, BUDGET_EMPTY_DIFFERENCE, formulas=True)
        self.assertTrue(
            message.startswith(
                'calculated_column_empty: column D "Différence" of sheet 1 is empty '
                "in every data row"
            )
        )
        self.assertIn('=IF(B2="","",B2-C2)', message)
        self.write.assert_not_awaited()

        result = await self.publish(tools, BUDGET_CALCULATED, formulas=True)
        self.assertNotIn("note", result)
        sheet, cached = self.workbook(), self.workbook(values=True)
        self.assertEqual(sheet["D2"].value, '=IF(B2="","",B2-C2)')
        self.assertEqual(cached["D2"].value, 20)
        self.assertIn(cached["D3"].value, (None, ""))
        # The total of the column adds up what the rows calculate.
        self.assertEqual(sheet["D6"].value, "=SUM(D2:D5)")
        self.assertEqual(cached["D6"].value, 20)

    async def test_at_most_one_refusal_per_reason_in_a_turn(self) -> None:
        tools = work_tools(BUDGET)
        without_formulas = BUDGET_HEADER + "| Loyer | | | |\n| Total | | | |\n"
        first = await self.refused(tools, without_formulas)
        self.assertTrue(first.startswith("formulas_requested_but_missing: "))
        self.assertIn('column D "Différence" of sheet 1', first)
        second = await self.refused(tools, BUDGET_EMPTY_DIFFERENCE, formulas=True)
        self.assertTrue(second.startswith("calculated_column_empty: "))
        result = await self.publish(tools, BUDGET_EMPTY_DIFFERENCE, formulas=True)
        self.assertEqual(
            result["note"],
            'Column D "Différence" of sheet 1 holds no formula: the answer must '
            "not say that it is calculated.",
        )
        self.assertEqual(
            tools.calculation_refusals, {MISSING, "calculated_cells_empty"}
        )
        # One refusal of a kind is below what counts as a repeated failure.
        failures = RepeatedFailures()
        codes = set()
        for message in (first, second):
            output, stop = failures.record("publish_document", dict(error=message))
            self.assertNotIn(NOTE_KEY, output)
            self.assertEqual(stop, 0)
            codes.add(error_code(output))
        self.assertEqual(len(codes), 2)

    async def test_lists_other_formats_and_other_callers_are_published(self) -> None:
        tools = work_tools("Create an Excel list of my books: title, author, year")
        self.assertNotIn("note", await self.publish(tools, BOOKS))
        # The same template when the request asks for no calculation.
        tools = work_tools("a running log template to fill in by hand")
        self.assertNotIn("note", await self.publish(tools, RUNNING_EMPTY))
        # A table in a document holds no formula in any case.
        tools = work_tools(RUNNING)
        self.assertNotIn(
            "note", await self.publish(tools, RUNNING_EMPTY, path="log.docx")
        )
        self.assertEqual(tools.calculation_refusals, set())
        # A caller that carries no request (not a Work turn) is not checked.
        bare = work_tools(RUNNING)
        del bare.request_text, bare.calculation_refusals
        self.assertNotIn("note", await self.publish(bare, RUNNING_EMPTY))

    def test_a_work_turn_starts_with_no_refusal_given(self) -> None:
        from services.orchestrator.code_tools import WorkTools

        tools = WorkTools(Mock(), Mock())
        self.assertEqual(tools.request_text, "")
        self.assertEqual(tools.calculation_refusals, set())
        self.assertIsNot(
            tools.calculation_refusals, WorkTools(Mock(), Mock()).calculation_refusals
        )


class WordingTests(unittest.TestCase):
    def test_the_tool_says_how_a_calculated_column_is_written(self) -> None:
        text = DESCRIPTIONS["publish_document"]
        self.assertIn(
            "A column or total the person wants calculated is a formula in every "
            "data row, rows left empty to fill in included",
            text,
        )
        self.assertIn('=IFERROR(C2/B2,"") for a ratio', text)
        self.assertIn('=IF(B2="","",B2-C2) otherwise', text)
        self.assertIn(
            "A header alone (Total, Difference, Pace) calculates nothing", text
        )

    def test_the_patterns_given_show_nothing_on_an_empty_row(self) -> None:
        table = (
            "# Log\n| Day | Km | Min | Pace | Gap |\n| --- | --- | --- | --- | --- |\n"
            '| Mon | 10 | 55 | =IFERROR(C2/B2,"") | =IF(B2="","",B2-C2) |\n'
            '| | | | =IFERROR(C3/B3,"") | =IF(B3="","",B3-C3) |\n'
            "| Sum | =SUM(B2:B3) | =SUM(C2:C3) | =SUM(D2:D3) | =SUM(E2:E3) |\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.xlsx"
            render(content(table, formulas=True), path, image_root=path.parent)
            cached = load_workbook(path, data_only=True)
            self.addCleanup(cached.close)
            sheet = cached["1"]
            self.assertEqual((sheet["D2"].value, sheet["E2"].value), (5.5, -45))
            self.assertIn(sheet["D3"].value, (None, ""))
            self.assertIn(sheet["E3"].value, (None, ""))
            # A total skips the rows that show nothing.
            self.assertEqual((sheet["D4"].value, sheet["E4"].value), (5.5, -45))


class TitleOnceTests(unittest.TestCase):
    def rendered(self, value: Content, suffix: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / ("file" + suffix)
        render(value, path, image_root=path.parent)
        return path

    def test_workbook_writes_a_repeated_heading_once(self) -> None:
        book = load_workbook(
            self.rendered(content(BUDGET_CALCULATED, formulas=True), ".xlsx")
        )
        self.addCleanup(book.close)
        column = [row[0].value for row in book["1"].iter_rows()]
        self.assertEqual(column[:2], ["Catégorie", "Loyer"])
        self.assertEqual(column[6:], [None, "Budget Mensuel"])
        # A title and a heading that differ are both written, as before.
        book = load_workbook(
            self.rendered(
                content(
                    "# Budget\n## Costs\n| Item | Amount |\n| --- | --- |\n| Rent | 5 |"
                ),
                ".xlsx",
            )
        )
        self.addCleanup(book.close)
        self.assertEqual(
            [row[0].value for row in book["1"].iter_rows()],
            ["Item", "Rent", None, "Budget", "Costs"],
        )

    def test_document_page_and_slide_write_it_once(self) -> None:
        memo = content(
            "# Budget Mensuel\nLoyer et courses.\n## Suite\nTransport.", path="a.docx"
        )
        self.assertTrue(memo.repeats_title())
        document = Document(str(self.rendered(memo, ".docx")))
        texts = [paragraph.text for paragraph in document.paragraphs]
        self.assertEqual(texts.count("Budget Mensuel"), 1)
        style = document.paragraphs[0].style
        assert style is not None
        self.assertEqual(style.name, "Title")
        # Later headings are written as before.
        self.assertEqual(texts.count("Suite"), 1)
        pages = PdfReader(self.rendered(memo, ".pdf")).pages
        self.assertEqual(pages[0].extract_text().count("Budget Mensuel"), 1)
        self.assertIn("Loyer et courses.", pages[0].extract_text())
        deck = content_slides()
        slides = Presentation(str(self.rendered(deck, ".pptx"))).slides
        first = [shape.text for shape in slides[0].shapes if shape.has_text_frame]
        self.assertEqual(first.count("Plan"), 1)
        second = [shape.text for shape in slides[1].shapes if shape.has_text_frame]
        self.assertEqual(second.count("Next"), 1)

    def test_only_the_first_heading_equal_to_the_title_is_dropped(self) -> None:
        both = Content.model_validate(
            dict(
                title="Budget",
                sections=[
                    dict(heading="Costs", paragraphs=["One"]),
                    dict(heading="Budget", paragraphs=["Two"]),
                ],
            )
        )
        self.assertFalse(both.repeats_title())
        document = Document(str(self.rendered(both, ".docx")))
        texts = [paragraph.text for paragraph in document.paragraphs]
        self.assertEqual(texts.count("Budget"), 2)
        # Case is part of the text: a heading written differently is kept.
        shouted = Content.model_validate(
            dict(title="Budget", sections=[dict(heading="BUDGET", paragraphs=["One"])])
        )
        self.assertFalse(shouted.repeats_title())


def content_slides() -> Content:
    return Content.model_validate(
        normalize(
            dict(
                path="deck.pptx",
                title="Plan",
                slides=[
                    dict(title="Plan", bullets=["One"]),
                    dict(title="Next", bullets=["Two"]),
                ],
            )
        )["content"]
    )


if __name__ == "__main__":
    unittest.main()
