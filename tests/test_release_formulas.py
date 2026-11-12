"""REQ-PROJ-05: explicit formulas, computed caches and rejected external formulas."""

import tempfile
from pathlib import Path
import unittest

from openpyxl import load_workbook
from packages.doc_render import render
from packages.doc_render.schema import Content
from services.orchestrator.project_document_input import normalize


class FormulaDeliveryTests(unittest.TestCase):
    def content(self, formula: str = '=SUMIF(A2:A5,"Formation",B2:B5)') -> Content:
        value = normalize(
            dict(
                path="totals.xlsx",
                formulas=True,
                content="# Costs\n## Expenses\n| Category | Amount |\n| --- | --- |\n| Formation | 200 |\n| Formation | 300 |\n| Equipement | 600 |\n| Equipement | 400 |\n| Formation total | "
                + formula
                + ' |\n| Equipement total | =SUMIF(A2:A5,"Equipement",B2:B5) |\n| Grand total | =SUM(B6:B7) |',
            )
        )
        return Content.model_validate(value["content"])

    def test_real_formulas_and_calculated_values_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "totals.xlsx"
            render(self.content(), path, image_root=path.parent)
            formulas = load_workbook(path, data_only=False)
            values = load_workbook(path, data_only=True)
            try:
                self.assertEqual(formulas["1"]["B6"].data_type, "f")
                self.assertEqual(formulas["1"]["B2"].data_type, "n")
                self.assertEqual(
                    [values["1"][f"B{i}"].value for i in (6, 7, 8)], [500, 1000, 1500]
                )
            finally:
                formulas.close()
                values.close()

    def test_formula_publication_reopens_and_reports_only_safe_validation_reason(
        self,
    ) -> None:
        from services.orchestrator.presentation_delivery import build_project_document

        document = build_project_document(
            self.content(),
            ".xlsx",
            ["Formation", "200"],
            max_bytes=16777216,
            timeout=30,
        )
        self.assertGreater(len(document.content), 100)
        with self.assertRaisesRegex(ValueError, "unsupported_formula_function"):
            build_project_document(
                self.content("=UNKNOWN(B2:B5)"),
                ".xlsx",
                [],
                max_bytes=16777216,
                timeout=30,
            )

    def test_fractional_formula_cache_round_trips(self) -> None:
        value = self.content("=SUM(0.1,0.2)")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fraction.xlsx"
            render(value, path, image_root=path.parent)
            book = load_workbook(path, data_only=True)
            try:
                self.assertAlmostEqual(book["1"]["B6"].value, 0.3)
            finally:
                book.close()

    def test_untrusted_functions_external_references_and_cycles_fail_closed(
        self,
    ) -> None:
        for formula in (
            '=WEBSERVICE("https://example.org")',
            "='[other.xlsx]1'!A1",
            "=SUM(B6)",
            "=SUM(A1:A99999)",
        ):
            with (
                self.subTest(formula=formula),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "totals.xlsx"
                with self.assertRaises(ValueError):
                    render(self.content(formula), path, image_root=path.parent)
                self.assertFalse(path.exists())

    def test_a_table_that_was_not_flagged_gives_the_same_workbook(self) -> None:
        value = self.content().model_dump()
        value["sections"][0]["tables"][0]["formulas"] = False
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unflagged.xlsx"
            render(Content.model_validate(value), path, image_root=path.parent)
            flagged = Path(directory) / "flagged.xlsx"
            render(self.content(), flagged, image_root=path.parent)
            # Formulas, cached values and formats: the file of a flagged table.
            self.assertEqual(path.read_bytes(), flagged.read_bytes())
            formulas = load_workbook(path, data_only=False)
            values = load_workbook(path, data_only=True)
            try:
                self.assertEqual(formulas["1"]["B6"].data_type, "f")
                self.assertEqual(
                    [values["1"][f"B{i}"].value for i in (6, 7, 8)], [500, 1000, 1500]
                )
            finally:
                formulas.close()
                values.close()

    def test_markdown_empty_cell_stays_empty_without_padding_rows(self) -> None:
        value = normalize(
            dict(
                path="summary.docx",
                content="| Name | Amount | Note |\n| --- | --- | --- |\n"
                "| Total | 1500 |  |\n| Rest || |\n| Later | 20 ||",
            )
        )
        content = Content.model_validate(value["content"])
        self.assertEqual(
            content.sections[0].tables[0].rows,
            (("Total", "1500", ""), ("Rest", "", ""), ("Later", "20", "")),
        )
        with self.assertRaises(ValueError):
            normalize(
                dict(
                    path="summary.docx",
                    content="| Name | Amount |\n| --- | --- |\n| Total |",
                )
            )


BUDGET = (
    "# Budget\n## Monthly follow-up\n"
    "| Category | January | February | Total |\n| --- | --- | --- | --- |\n"
    "| Rent | 800 | | =B2+C2 |\n"
    "| Groceries | 300.5 |  | =B3+C3 |\n"
    "| Total | =SUM(B2:B3) | =SUM(C2:C3) | =SUM(D2:D3) |\n"
    "{extra}"
)


class ImpliedFormulaTests(unittest.TestCase):
    """In a workbook a cell that starts with "=" is a formula without the flag."""

    HEAD = "# Costs\n## Expenses\n| Item | Amount | Note |\n| --- | --- | --- |\n"
    ROWS = "| Rent | 800,00 | fixed |\n| Power | 60,50 | |\n"

    def content(self, extra: str, suffix: str = ".xlsx") -> Content:
        # No formulas option: the flag is implied by the cells.
        value = normalize(
            dict(path="costs" + suffix, content=self.HEAD + self.ROWS + extra)
        )
        return Content.model_validate(value["content"])

    def delivered(self, extra: str, suffix: str = ".xlsx") -> bytes:
        from services.orchestrator.presentation_delivery import build_project_document

        return build_project_document(
            self.content(extra, suffix), suffix, [], max_bytes=16777216, timeout=30
        ).content

    def test_formulas_are_evaluated_and_cached_without_the_flag(self) -> None:
        from io import BytesIO

        raw = self.delivered(
            '| **Total** | **=SUM(B2:B3)** | =IF(B4>800,"over","under") |\n'
        )
        formulas = load_workbook(BytesIO(raw), data_only=False)
        values = load_workbook(BytesIO(raw), data_only=True)
        try:
            self.assertFalse(
                self.content("| Total | =SUM(B2:B3) | |\n")
                .sections[0]
                .tables[0]
                .formulas
            )
            sheet, cached = formulas["1"], values["1"]
            self.assertEqual(sheet["B4"].value, "=SUM(B2:B3)")
            self.assertEqual(sheet["B4"].data_type, "f")
            self.assertEqual(sheet["C4"].data_type, "f")
            self.assertEqual(cached["B4"].value, 860.5)
            self.assertEqual(cached["C4"].value, "over")
            # The total is shown as the amounts it adds up.
            self.assertEqual(sheet["B3"].number_format, "0.00")
            self.assertEqual(sheet["B4"].number_format, "#,##0.00")
        finally:
            formulas.close()
            values.close()

    def test_the_refusals_are_those_of_a_flagged_table(self) -> None:
        apostrophe = r".*a leading = as text starts with an apostrophe, as in '=A1\)"
        for extra, reason in (
            ("| Total | =UNKNOWN(B2:B3) | |\n", "unsupported_formula_function"),
            ("| Total | =SUM(B2:B3 | |\n", "unsupported_formula"),
            # A text that only starts like a formula is named, with the way
            # to keep it a text.
            ("| Total | 860,50 | => see the lease |\n", "unsupported_formula"),
        ):
            with (
                self.subTest(extra=extra),
                self.assertRaisesRegex(
                    ValueError, reason + r" \(formulas support" + apostrophe
                ),
            ):
                self.delivered(extra)
        for extra, reason in (
            ("| Total | =SUM(B2:B9) | |\n", "formula_reference_outside_table:B4:"),
            ("| Total | =A2*2 | |\n", "formula_requires_numeric_cells"),
            ("| Total | =B4+1 | |\n", "circular_or_deep_formula"),
        ):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, reason):
                self.delivered(extra)

    def test_an_apostrophe_keeps_a_leading_equals_sign_as_text(self) -> None:
        from io import BytesIO

        extra = (
            "| Total | =SUM(B2:B3) | '=SUM(B2:B3) |\n| Rule | | **'=> see *.txt** |\n"
        )
        raw = self.delivered(extra)
        formulas = load_workbook(BytesIO(raw), data_only=False)
        values = load_workbook(BytesIO(raw), data_only=True)
        try:
            for book in (formulas, values):
                self.assertEqual(book["1"]["C4"].value, "=SUM(B2:B3)")
                self.assertEqual(book["1"]["C4"].data_type, "s")
                self.assertEqual(book["1"]["C5"].value, "=> see *.txt")
            # Marked as Excel marks a cell entered with an apostrophe.
            self.assertTrue(formulas["1"]["C4"].quotePrefix)
            self.assertFalse(formulas["1"]["B4"].quotePrefix)
            self.assertEqual(formulas["1"]["B4"].data_type, "f")
            self.assertEqual(values["1"]["B4"].value, 860.5)
        finally:
            formulas.close()
            values.close()
        # The apostrophe is never displayed, in any format.
        from docx import Document

        cells = [
            cell.text
            for row in Document(BytesIO(self.delivered(extra, ".docx"))).tables[0].rows
            for cell in row.cells
        ]
        self.assertEqual(
            cells[-6:],
            ["Total", "=SUM(B2:B3)", "=SUM(B2:B3)"] + ["Rule", "", "=> see *.txt"],
        )

    def test_other_formats_show_the_cell_and_refuse_the_flag(self) -> None:
        from io import BytesIO

        from docx import Document
        from pptx import Presentation
        from pypdf import PdfReader
        from services.orchestrator.presentation_delivery import build_project_document

        extra = "| Total | =SUM(B2:B3) | |\n"
        document = Document(BytesIO(self.delivered(extra, ".docx")))
        self.assertEqual(document.tables[0].rows[3].cells[1].text, "=SUM(B2:B3)")
        page = PdfReader(BytesIO(self.delivered(extra, ".pdf"))).pages[0]
        self.assertIn("=SUM(B2:B3)", page.extract_text())
        slides = dict(
            title="Costs",
            slides=[
                dict(
                    title="Expenses",
                    bullets=["Two lines"],
                    table=dict(
                        headers=["Item", "Amount"],
                        rows=[["Rent", "800"], ["Total", "=SUM(B2:B2)"]],
                    ),
                )
            ],
        )
        deck = build_project_document(
            Content.model_validate(
                normalize(dict(path="costs.pptx", **slides))["content"]
            ),
            ".pptx",
            [],
            max_bytes=16777216,
            timeout=30,
        )
        shown = [
            cell.text
            for slide in Presentation(BytesIO(deck.content)).slides
            for shape in slide.shapes
            if shape.has_table
            for row in shape.table.rows
            for cell in row.cells
        ]
        self.assertIn("=SUM(B2:B2)", shown)
        # The option itself is still refused outside a workbook.
        for fields in (
            dict(path="costs.docx", content=self.HEAD + self.ROWS + extra),
            dict(path="costs.pdf", content=self.HEAD + self.ROWS + extra),
            dict(path="costs.pptx", **slides),
        ):
            with (
                self.subTest(path=fields["path"]),
                self.assertRaisesRegex(ValueError, "Formulas require XLSX"),
            ):
                normalize(dict(fields, formulas=True))

    def test_the_tool_says_it_in_one_sentence(self) -> None:
        from services.orchestrator.project_document_tools import DESCRIPTIONS

        text = DESCRIPTIONS["publish_document"]
        self.assertIn(
            "In an XLSX table a cell starting with = is a formula, with or "
            "without formulas=true (a cell that must show a leading = as text "
            "starts with an apostrophe: '=A1)",
            text,
        )
        self.assertNotIn("set formulas=true", text)


class EmptyCellTests(unittest.TestCase):
    """A cell left empty is an empty cell, read by formulas as Excel reads it."""

    def content(self, extra: str = "", suffix: str = "xlsx") -> Content:
        value = normalize(
            dict(
                path="budget." + suffix,
                formulas=suffix == "xlsx",
                content=BUDGET.format(extra=extra),
            )
        )
        return Content.model_validate(value["content"])

    def test_a_month_not_started_is_empty_and_its_totals_are_zero(self) -> None:
        from zipfile import ZipFile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.xlsx"
            result = render(self.content(), path, image_root=path.parent)
            # No dash and no other character was invented for the empty cells.
            self.assertNotIn("—", "".join(result.text))
            formulas = load_workbook(path, data_only=False)
            values = load_workbook(path, data_only=True)
            try:
                sheet = values["1"]
                for cell in ("C2", "C3"):
                    self.assertIsNone(formulas["1"][cell].value)
                    self.assertIsNone(sheet[cell].value)
                self.assertEqual(formulas["1"]["C4"].value, "=SUM(C2:C3)")
                self.assertEqual(
                    [sheet[cell].value for cell in ("D2", "D3", "B4", "C4", "D4")],
                    [800, 300.5, 1100.5, 0, 1100.5],
                )
            finally:
                formulas.close()
                values.close()
            with ZipFile(path) as archive:
                xml = archive.read("xl/worksheets/sheet1.xml").decode()
        # Truly empty: the cell is absent from the file, not an empty string.
        self.assertIn('r="B2"', xml)
        self.assertNotIn('r="C2"', xml)
        self.assertNotIn('r="C3"', xml)

    def test_the_publication_path_delivers_the_sheet_with_an_empty_month(self) -> None:
        from services.orchestrator.presentation_delivery import build_project_document

        document = build_project_document(
            self.content(), ".xlsx", [], max_bytes=16777216, timeout=30
        )
        preview = document.preview.decode()
        self.assertIn("=SUM(C2:C3)", preview)
        self.assertNotIn("—", preview)

    def test_formulas_over_empty_cells_compute_what_excel_shows(self) -> None:
        cases = {
            # Arithmetic reads an empty cell as 0.
            "=C2+C3": 0,
            "=B2-C2": 800,
            "=C2*B2": 0,
            "=-C2": 0,
            "=C2*-1": 0,
            "=C2%": 0,
            "=ABS(C2)": 0,
            "=ROUND(C2,2)": 0,
            # Aggregates skip it; over no number they give 0.
            "=SUM(C2:C3)": 0,
            "=SUM(B2:C3)": 1100.5,
            "=COUNT(C2:C3)": 0,
            "=COUNT(B2:C3)": 2,
            "=MIN(C2:C3)": 0,
            "=MAX(C2:C3)": 0,
            "=MIN(B2:C3)": 300.5,
            "=AVERAGE(B2:C3)": 550.25,
            '=SUMIF(A2:A3,"Rent",C2:C3)': 0,
            # Comparisons and conditions: 0, "" and FALSE.
            "=C2=0": True,
            '=C2=""': True,
            "=C2=FALSE": True,
            "=C2>0": False,
            "=IF(C2,1,2)": 2,
            "=NOT(C2)": True,
            '=IF(C2="","to fill in",C2)': "to fill in",
            # Concatenation reads it as "".
            '=C2&"x"': "x",
            # A reference to an empty cell shows 0.
            "=C2": 0,
            # The error of an average or a division over cells still empty is
            # caught by IFERROR, as in Excel.
            '=IFERROR(AVERAGE(C2:C3),"")': None,
            "=IFERROR(B2/C2,0)": 0,
        }
        for formula, expected in cases.items():
            with self.subTest(formula=formula):
                extra = f"| Check | {formula} | | |\n"
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "budget.xlsx"
                    render(self.content(extra), path, image_root=path.parent)
                    book = load_workbook(path, data_only=True)
                    try:
                        found = book["1"]["B5"].value
                    finally:
                        book.close()
                self.assertEqual(found, expected)
                # TRUE is not 1 and "" is not 0 in the stored result.
                self.assertEqual(isinstance(found, bool), isinstance(expected, bool))

    def test_what_excel_shows_as_an_error_is_still_refused_by_name(self) -> None:
        from services.orchestrator.presentation_delivery import build_project_document

        for extra, reason in (
            ("| Check | =AVERAGE(C2:C3) | | |\n", "formula_division_by_zero"),
            ("| Check | =B2/C2 | | |\n", "formula_division_by_zero"),
            # Text is not an empty cell: a written dash still stops arithmetic.
            ("| Check | — | =B5+1 | |\n", "formula_requires_numeric_cells"),
            ("| Check | =A2*2 | | |\n", "formula_requires_numeric_cells"),
        ):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(
                    ValueError,
                    reason + r" \(formulas support.*leave a cell to fill in empty",
                ):
                    build_project_document(
                        self.content(extra),
                        ".xlsx",
                        [],
                        max_bytes=16777216,
                        timeout=30,
                    )

    def test_a_reference_outside_the_table_is_refused_with_its_cell(self) -> None:
        from services.orchestrator.presentation_delivery import build_project_document

        for extra, cell in (
            # Beyond the last row, beyond the last column, the title written
            # under the table, and a branch IF does not evaluate today.
            ("| Check | =SUM(B2:B40) | | |\n", "B5"),
            ("| Check | | =F2 | |\n", "C5"),
            ("| Check | | | =A7 |\n", "D5"),
            ('| Check | =IF(B2>0,"",B9) | | |\n', "B5"),
            # Rows counted from the title, as when texts were above the table.
            ("| Check | =SUM(B4:B5)+B6 | | |\n", "B5"),
        ):
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(
                    ValueError,
                    f"formula_reference_outside_table:{cell}:sheet_1 "
                    r"\(the code ends with the cell that holds the formula.*"
                    r"its header is row 1, its first data row is row 2",
                ):
                    build_project_document(
                        self.content(extra),
                        ".xlsx",
                        [],
                        max_bytes=16777216,
                        timeout=30,
                    )

    def test_blank_rows_to_fill_in_below_a_total_are_inside_the_table(self) -> None:
        value = normalize(
            dict(
                path="log.xlsx",
                formulas=True,
                content="# Log\n## Expenses\n| Date | Amount |\n| --- | --- |\n"
                "| Total | =SUM(B3:B5) |\n| | |\n| | |\n| | |",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.xlsx"
            render(
                Content.model_validate(value["content"]), path, image_root=path.parent
            )
            book = load_workbook(path, data_only=True)
            try:
                sheet = book["1"]
                self.assertEqual(sheet["B2"].value, 0)
                # The three rows to fill in are in the table and hold nothing;
                # the texts follow them after one empty row.
                self.assertEqual(
                    [sheet.cell(row, 1).value for row in range(3, 9)],
                    [None, None, None, None, "Log", "Expenses"],
                )
                self.assertEqual(sheet.max_row, 8)
            finally:
                book.close()

    def test_strict_content_accepts_empty_cells_and_needs_column_names(self) -> None:
        table = dict(headers=["", "January"], rows=[["Rent", ""], ["Groceries", "  "]])
        content = Content.model_validate(
            dict(title="Budget", sections=[dict(heading="Months", tables=[table])])
        )
        self.assertEqual(
            content.sections[0].tables[0].rows, (("Rent", ""), ("Groceries", ""))
        )
        self.assertEqual(
            content.texts(), ("Budget", "Months", "January", "Rent", "Groceries")
        )
        for bad in (
            dict(table, headers=["", " "]),
            dict(table, rows=[["Rent"]]),
            dict(table, rows=[["Rent", None]]),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Content.model_validate(
                    dict(
                        title="Budget", sections=[dict(heading="Months", tables=[bad])]
                    )
                )
        # Text outside table cells is still required.
        with self.assertRaises(ValueError):
            Content.model_validate(
                dict(title="Budget", sections=[dict(heading="Months", bullets=[""])])
            )

    def test_other_formats_keep_the_cell_empty_without_a_dash(self) -> None:
        from docx import Document
        from pptx import Presentation

        from packages.doc_render.schema import PresentationOutline

        for suffix in ("docx", "pdf"):
            with (
                self.subTest(suffix=suffix),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / ("budget." + suffix)
                result = render(
                    self.content("| | | | |\n", suffix), path, image_root=path.parent
                )
                text = "".join(result.text)
                self.assertNotIn("—", text)
                self.assertIn("Rent", text)
                self.assertIn("=SUM(C2:C3)", "".join(text.split()))
                if suffix == "docx":
                    grid = Document(str(path)).tables[0]
                    self.assertEqual(
                        [cell.text for cell in grid.rows[1].cells],
                        ["Rent", "800", "", "=B2+C2"],
                    )
                    self.assertEqual(
                        [cell.text for cell in grid.rows[4].cells], [""] * 4
                    )
        outline = PresentationOutline.model_validate(
            dict(
                title="Budget",
                slides=[
                    dict(
                        title="Months",
                        bullets=["February is not started"],
                        table=dict(
                            headers=["Category", "February"], rows=[["Rent", ""]]
                        ),
                    )
                ],
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.pptx"
            render(outline.content(), path, image_root=path.parent)
            cells = [
                cell.text
                for shape in Presentation(str(path)).slides[0].shapes
                if shape.has_table
                for row in shape.table.rows
                for cell in row.cells
            ]
        self.assertEqual(cells, ["Category", "February", "Rent", ""])


class SourceAppendixTests(unittest.TestCase):
    def test_summary_keeps_cited_source_excerpts_in_verified_artifact(self) -> None:
        from services.orchestrator.project_document_input import markdown
        from services.orchestrator.project_document_tools import include_sources
        from services.orchestrator.presentation_delivery import build_project_document

        summary = markdown("# Synthèse Quartz\nRésumé du projet.", "Quartz")
        source = "Budget initial : 1500 euros.\nLivraison : décembre."
        content = include_sources(summary, [source])
        self.assertEqual(content.sections[0], summary.sections[0])
        self.assertEqual(include_sources(content, [source]), content)
        document = build_project_document(
            content, ".docx", [source], max_bytes=16777216, timeout=30
        )
        self.assertIn(b"Budget initial : 1500 euros.", document.preview)


class ComputedColumnTests(unittest.TestCase):
    """A computed column ("pace calculated automatically") is arithmetic."""

    def workbook(self, formula: str) -> Content:
        value = normalize(
            dict(
                path="log.xlsx",
                formulas=True,
                content="# Training log\n## Runs\n"
                "| Day | Distance | Minutes | Pace |\n| --- | --- | --- | --- |\n"
                f"| Monday | 5 | 27.5 | {formula} |\n| Thursday | 8 | 44 | =C3/B3 |",
            )
        )
        return Content.model_validate(value["content"])

    def cached(self, formula: str) -> object:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.xlsx"
            render(self.workbook(formula), path, image_root=path.parent)
            book = load_workbook(path, data_only=True)
            try:
                self.assertEqual(book["1"]["D3"].value, 5.5)
                return book["1"]["D2"].value
            finally:
                book.close()

    def test_operators_functions_and_lazy_branches_compute_cached_values(
        self,
    ) -> None:
        cases = {
            "=C2/B2": 5.5,
            "=(C2+0.5)/B2": 5.6,
            '=IF(AND(B2>0,C2>0),C2/B2,"")': 5.5,
            # An empty text result reopens as a blank cell.
            '=IF(B2>100,C2/B2,"")': None,
            '=IF(B2=0,"",C2/B2)': 5.5,
            '=IFERROR(C2/0,"n/a")': "n/a",
            "=ROUND(C2/B2*1.01,1)": 5.6,
            "=ROUND(2.5,0)": 3,
            "=-2^2": 4,
            "=50%*B2": 2.5,
            '=B2&" km"': "5 km",
            "=B2>3": True,
            "=NOT(OR(B2>10,C2>100))": True,
            "=AVERAGE(B2:B3)": 6.5,
            "=MAX(B2:C3)-MIN(B2:C3)": 39,
            "=SUM(B2:B3)/COUNT(B2:B3)": 6.5,
            "=ABS(B2-C2)": 22.5,
        }
        for formula, expected in cases.items():
            with self.subTest(formula=formula):
                self.assertEqual(self.cached(formula), expected)

    def test_formula_failures_name_their_reason_for_the_agent(self) -> None:
        from services.orchestrator.presentation_delivery import build_project_document

        for formula, reason in (
            ("=C2/0", "formula_division_by_zero"),
            ("=A2/B2", "formula_requires_numeric_cells"),
            ("=FOO(B2)", "unsupported_formula_function"),
            ("=B2:B3*2", "formula_range_needs_single_cell"),
            ("=ROUND(B2)", "formula_argument_count"),
        ):
            with self.subTest(formula=formula):
                with self.assertRaisesRegex(
                    ValueError, reason + r" \(formulas support"
                ):
                    build_project_document(
                        self.workbook(formula),
                        ".xlsx",
                        [],
                        max_bytes=16777216,
                        timeout=30,
                    )

    def test_only_codes_and_fixed_messages_leave_the_render_process(self) -> None:
        from services.orchestrator.presentation_delivery import render_failure_reason

        self.assertEqual(
            render_failure_reason(
                'Traceback (most recent call last):\n  File "/x/y.py"\n'
                "packages.doc_render.verify.VerificationError: missing_expected_text"
            ),
            "missing_expected_text",
        )
        self.assertEqual(
            render_failure_reason("ValueError: Source coverage verification failed"),
            "Source coverage verification failed",
        )
        for diagnostic in (
            "ValueError: secret content of /tmp/atlas-deliverable-x/artifact.xlsx",
            "KeyError: 'title'",
            "",
        ):
            with self.subTest(diagnostic=diagnostic):
                self.assertEqual(render_failure_reason(diagnostic), "")
