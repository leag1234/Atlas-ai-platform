"""REQ-PROJ-05: in a workbook a table starts at cell A1 of its sheet, so a
formula reference means the same cell to its author, in the file and to the
person who opens it.

Staging, 2026-11-12: a monthly budget was delivered with its title and heading
above the table. The totals, written from the header of the table
(=SUM(B2:B5)), summed the heading, the header and two of the four categories;
with empty cells nothing failed, and every total was wrong once filled in.
"""

from io import BytesIO
from pathlib import Path
import tempfile
from typing import Any
import unittest

from openpyxl import Workbook, load_workbook
from packages.doc_render import render
from packages.doc_render.formulas import cache_formulas
from packages.doc_render.schema import Content
from packages.doc_render.spreadsheet import table_areas, total_findings
from services.orchestrator.presentation_delivery import build_project_document
from services.orchestrator.project_document_input import normalize

MONTHS = ("Janvier", "Février", "Mars", "Avril", "Mai", "Juin")
CATEGORIES = ("Loyer", "Courses", "Transport", "Loisirs")


def budget(amounts: dict[str, tuple[str, ...]] | None = None) -> Content:
    """The budget of the defect, as it was written: rows counted from the header."""
    lines = [
        "# Budget Mensuel",
        "## Suivi des dépenses par catégorie",
        "| Catégorie | " + " | ".join(MONTHS) + " | Total | Moyenne |",
        "|" + " --- |" * 9,
    ]
    for row, category in enumerate(CATEGORIES, 2):
        cells = (amounts or {}).get(category, ("",) * 6)
        lines.append(
            f"| {category} | "
            + " | ".join(cells)
            + f' | =SUM(B{row}:G{row}) | =IF(COUNT(B{row}:G{row})>0,AVERAGE(B{row}:G{row}),"") |'
        )
    lines.append(
        "| Total | "
        + " | ".join(f"=SUM({column}2:{column}5)" for column in "BCDEFGH")
        + ' | =IF(COUNT(B6:G6)>0,AVERAGE(B6:G6),"") |'
    )
    return workbook("\n".join(lines))


def workbook(markdown: str) -> Content:
    value = normalize(dict(path="budget.xlsx", formulas=True, content=markdown))
    return Content.model_validate(value["content"])


def strict(*sections: dict[str, Any]) -> Content:
    return Content.model_validate(dict(title="Accounts", sections=list(sections)))


def grid(headers: list[str], *rows: list[str], formulas: bool = True) -> dict[str, Any]:
    return dict(headers=headers, rows=list(rows), formulas=formulas)


class Rendered:
    """A rendered workbook reopened twice: its formulas and their cached values."""

    def __init__(self, test: unittest.TestCase, content: Content) -> None:
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "budget.xlsx"
        render(content, self.path, image_root=self.path.parent)
        self.raw = self.path.read_bytes()
        self.formulas: Any = load_workbook(BytesIO(self.raw), data_only=False)
        self.values: Any = load_workbook(BytesIO(self.raw), data_only=True)
        test.addCleanup(self.formulas.close)
        test.addCleanup(self.values.close)


class TableAtA1Tests(unittest.TestCase):
    def test_the_budget_of_the_defect_has_its_header_in_row_1(self) -> None:
        content = budget()
        book = Rendered(self, content)
        sheet = book.formulas["1"]
        self.assertEqual(
            [cell.value for cell in sheet[1]],
            ["Catégorie", *MONTHS, "Total", "Moyenne"],
        )
        self.assertEqual(
            [sheet.cell(row, 1).value for row in range(2, 7)], [*CATEGORIES, "Total"]
        )
        # The formulas are in the file as they were written.
        self.assertEqual(sheet["B6"].value, "=SUM(B2:B5)")
        self.assertEqual(sheet["H2"].value, "=SUM(B2:G2)")
        self.assertEqual(sheet["H6"].value, "=SUM(H2:H5)")
        # The title and the heading stay in the sheet, under the table.
        self.assertEqual(
            [sheet.cell(row, 1).value for row in range(7, 10)],
            [None, "Budget Mensuel", "Suivi des dépenses par catégorie"],
        )
        self.assertEqual(sheet.max_row, 9)
        # Nothing filled in yet: every total is 0 and no average is shown.
        values = book.values["1"]
        self.assertEqual(
            [values.cell(6, column).value for column in range(2, 9)], [0] * 7
        )
        self.assertIsNone(values["I2"].value)
        self.assertEqual(total_findings(book.raw, table_areas(content)), [])

    def test_the_person_fills_the_sheet_in_and_every_total_is_right(self) -> None:
        content = budget()
        book = Rendered(self, content)
        spent = {
            "Loyer": (800, 800, 800, 800, 800, 800),
            "Courses": (310.5, 280, 295.25, 301, 322.4, 290),
            "Transport": (60, 60, 75, 60, 60, 90),
            "Loisirs": (120, 45, 0, 210, 80, 150),
        }
        # What the person does in a spreadsheet program: type amounts in the
        # empty cells of the delivered file, and let the formulas compute.
        sheet = book.formulas["1"]
        for row, category in enumerate(CATEGORIES, 2):
            self.assertEqual(sheet.cell(row, 1).value, category)
            for column, amount in enumerate(spent[category], 2):
                self.assertIsNone(sheet.cell(row, column).value)
                sheet.cell(row, column, amount)
        book.formulas.save(book.path)
        cache_formulas(book.path, tables=table_areas(content))
        filled = load_workbook(book.path, data_only=True)
        self.addCleanup(filled.close)
        values: Any = filled["1"]
        for row, category in enumerate(CATEGORIES, 2):
            with self.subTest(category=category):
                self.assertAlmostEqual(values.cell(row, 8).value, sum(spent[category]))
                self.assertAlmostEqual(
                    values.cell(row, 9).value, sum(spent[category]) / 6
                )
        for column in range(2, 8):
            with self.subTest(column=column):
                self.assertAlmostEqual(
                    values.cell(6, column).value,
                    sum(spent[category][column - 2] for category in CATEGORIES),
                )
        self.assertAlmostEqual(values["H6"].value, sum(map(sum, spent.values())))
        self.assertAlmostEqual(values["I6"].value, sum(map(sum, spent.values())) / 6)

    def test_amounts_given_with_the_request_are_added_up_in_the_cache(self) -> None:
        book = Rendered(
            self,
            budget(
                {
                    "Loyer": ("800", "800", "", "", "", ""),
                    "Courses": ("310,50", "280", "", "", "", ""),
                    "Transport": ("60", "", "", "", "", ""),
                    "Loisirs": ("120", "45", "", "", "", ""),
                }
            ),
        )
        values = book.values["1"]
        self.assertEqual(
            [values.cell(6, column).value for column in (2, 3, 4, 8)],
            [1290.5, 1125, 0, 2415.5],
        )
        self.assertEqual(values["H3"].value, 590.5)
        self.assertEqual(values["I4"].value, 60)

    def test_texts_and_other_sheets_never_move_a_table(self) -> None:
        content = strict(
            dict(
                heading="Rates",
                paragraphs=["One rate per line.", "Reviewed each quarter."],
                bullets=["Net of tax"],
                tables=[grid(["Item", "Rate"], ["Hour", "40"], ["Day", "300"])],
            ),
            dict(
                heading="Invoice",
                paragraphs=["Hours and days of the month."],
                tables=[
                    grid(
                        ["Item", "Units", "Amount"],
                        ["Hours", "12", "=B2*1!B2"],
                        ["Days", "3", "=B3*'1'!$B$3"],
                        ["Total", "=SUM(B2:B3)", "=SUM(C$2:C$3)"],
                        ["Highest rate", "", "=MAX('1'!B2:B3)"],
                    )
                ],
            ),
        )
        book = Rendered(self, content)
        first, second = book.formulas["1"], book.formulas["2"]
        self.assertEqual([cell.value for cell in first[1]], ["Item", "Rate"])
        self.assertEqual(
            [first.cell(row, 1).value for row in range(4, 10)],
            [
                None,
                "Accounts",
                "Rates",
                "One rate per line.",
                "Reviewed each quarter.",
                "Net of tax",
            ],
        )
        self.assertEqual(
            [cell.value for cell in second[1]], ["Item", "Units", "Amount"]
        )
        self.assertEqual(second["C2"].value, "=B2*1!B2")
        self.assertEqual(second["C3"].value, "=B3*'1'!$B$3")
        self.assertEqual(
            [second.cell(row, 1).value for row in range(6, 9)],
            [None, "Invoice", "Hours and days of the month."],
        )
        values = book.values["2"]
        self.assertEqual(
            [values[cell].value for cell in ("C2", "C3", "B4", "C4", "C5")],
            [480, 900, 15, 1380, 300],
        )

    def test_a_section_without_a_table_keeps_its_title_first(self) -> None:
        book = Rendered(
            self,
            strict(dict(heading="Notes", paragraphs=["Nothing to compute here."])),
        )
        self.assertEqual(
            [cell.value for row in book.formulas["1"].iter_rows() for cell in row],
            ["Accounts", "Notes", "Nothing to compute here."],
        )

    def test_only_the_first_table_of_a_section_holds_formulas(self) -> None:
        rates = grid(["Item", "Rate"], ["Hour", "40"], ["Day", "300"])
        plain = grid(["Month", "Days"], ["May", "3"], ["June", "5"])
        book = Rendered(self, strict(dict(heading="Rates", tables=[rates, plain])))
        sheet = book.formulas["1"]
        # A later table without formulas follows the texts.
        self.assertEqual(
            [[cell.value for cell in row] for row in sheet.iter_rows(4, 9)],
            [
                [None, None],
                ["Accounts", None],
                ["Rates", None],
                ["Month", "Days"],
                ["May", 3],
                ["June", 5],
            ],
        )
        computed = grid(["Month", "Days"], ["May", "3"], ["Total", "=SUM(B2:B2)"])
        with self.assertRaisesRegex(
            ValueError,
            r"formula_table_not_first:sheet_1 \(the code ends with the sheet: only "
            r"the first table of a section holds formulas.*its own section",
        ):
            build_project_document(
                strict(dict(heading="Rates", tables=[rates, computed])),
                ".xlsx",
                [],
                max_bytes=16777216,
                timeout=30,
            )
        # The same two tables, each in its own section, are two sheets.
        book = Rendered(
            self,
            strict(
                dict(heading="Rates", tables=[rates]),
                dict(heading="Months", tables=[computed]),
            ),
        )
        self.assertEqual(book.values["2"]["B3"].value, 3)
        # Without the formulas option a cell that starts with "=" is text.
        literal = dict(computed, formulas=False)
        book = Rendered(self, strict(dict(heading="Rates", tables=[rates, literal])))
        self.assertEqual(book.formulas["1"]["B9"].data_type, "s")

    def test_a_reference_must_name_a_cell_of_a_table(self) -> None:
        notes = dict(heading="Notes", paragraphs=["42"])
        for formula, reason in (
            # A sheet without a table has no cell a formula may use.
            ("=2!A1", "formula_reference_outside_table:B3:sheet_1"),
            ("=SUM(B2:B3)", "circular_or_deep_formula"),
            ("=B4", "formula_reference_outside_table:B3:sheet_1"),
            ("=C2", "formula_reference_outside_table:B3:sheet_1"),
            ("=3!A1", "unknown_formula_sheet"),
        ):
            with self.subTest(formula=formula):
                content = strict(
                    dict(
                        heading="Rates",
                        tables=[grid(["Item", "Rate"], ["Hour", "40"], ["x", formula])],
                    ),
                    notes,
                )
                with self.assertRaisesRegex(ValueError, "^" + reason + "$"):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "rates.xlsx"
                        render(content, path, image_root=path.parent)


class TotalFindingsTests(unittest.TestCase):
    def findings(self, *totals: str, formulas: bool = True) -> list[str]:
        content = strict(
            dict(
                heading="Costs",
                tables=[
                    grid(
                        ["Item", "First", "Second", "Third", "Fourth"],
                        ["a", "1", "10", "100", "1000"],
                        ["b", "2", "20", "200", "2000"],
                        ["c", "3", "30", "300", "3000"],
                        ["Total", *totals],
                        formulas=formulas,
                    )
                ],
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "costs.xlsx"
            render(content, path, image_root=path.parent)
            return total_findings(path.read_bytes(), table_areas(content))

    def test_whole_columns_are_fine_and_bad_ranges_are_named(self) -> None:
        self.assertEqual(
            self.findings(
                "=SUM(B2:B4)", "=AVERAGE(C2:C4)", "=MIN($D$2:$D$4)", "=MAX(E2:E4)"
            ),
            [],
        )
        self.assertEqual(
            self.findings(
                # Starts on the header; stops one row early; both; another
                # column or more than a range is not a total of its column.
                "=SUM(B1:B4)",
                "=AVERAGE(C2:C3)",
                "=MAX(D1:D2)",
                "=SUM(B2:B3)+E4",
            ),
            [
                "total_includes_header:B5:sheet_1",
                "total_stops_early:C5:sheet_1",
                "total_includes_header:D5:sheet_1",
                "total_stops_early:D5:sheet_1",
            ],
        )
        self.assertEqual(self.findings("1", "2", "3", "4", formulas=False), [])

    def test_the_file_delivered_with_texts_above_its_table_is_found(self) -> None:
        # The layout of the delivered file: title, heading, then the table,
        # with totals counted from the header of the table.
        book = Workbook()
        sheet: Any = book.active
        sheet.title = "1"
        for row in (
            ["Budget Mensuel"],
            ["Suivi des dépenses par catégorie"],
            ["Catégorie", "Janvier", "Février"],
            ["Loyer"],
            ["Courses"],
            ["Transport"],
            ["Loisirs"],
            ["Total", "=SUM(B2:B5)", "=SUM(C2:C5)"],
        ):
            sheet.append(row)
        raw = BytesIO()
        book.save(raw)
        book.close()
        self.assertEqual(
            total_findings(raw.getvalue(), {"1": (8, 3)}),
            ["total_stops_early:B8:sheet_1", "total_stops_early:C8:sheet_1"],
        )


class ContractWordingTests(unittest.TestCase):
    def test_the_tool_says_where_a_table_starts(self) -> None:
        from services.orchestrator.project_document_tools import DESCRIPTIONS

        text = DESCRIPTIONS["publish_document"]
        self.assertIn("its first table starts at cell A1 of that sheet", text)
        self.assertIn("a total under four data rows is =SUM(B2:B5)", text)
        self.assertIn("Only that first table holds formulas", text)
        # The former layout made the model count the rows above the table.
        self.assertNotIn("first sheet starts with title", text)

    def test_the_refusals_explain_the_contract(self) -> None:
        from services.orchestrator.presentation_delivery import render_failure_reason

        outside = render_failure_reason(
            "ValueError: formula_reference_outside_table:B6:sheet_1"
        )
        self.assertTrue(
            outside.startswith(
                "formula_reference_outside_table:B6:sheet_1 (the code ends with "
                "the cell that holds the formula and its sheet"
            )
        )
        self.assertIn("=SUM(B2:B5)", outside)
        later = render_failure_reason("ValueError: formula_table_not_first:sheet_2")
        self.assertTrue(
            later.startswith("formula_table_not_first:sheet_2 (the code ends with")
        )


if __name__ == "__main__":
    unittest.main()
