"""REQ-PROJ-05: explicit formulas, computed caches and rejected external formulas."""

import tempfile
from pathlib import Path
import unittest

from openpyxl import load_workbook
from packages.doc_render import render
from packages.doc_render.schema import Content
from services.orchestrator.project_document_input import normalize


class FormulaDeliveryTests(unittest.TestCase):
    def content(self, formula: str = '=SUMIF(A4:A7,"Formation",B4:B7)') -> Content:
        value = normalize(
            dict(
                path="totals.xlsx",
                formulas=True,
                content="# Costs\n## Expenses\n| Category | Amount |\n| --- | --- |\n| Formation | 200 |\n| Formation | 300 |\n| Equipement | 600 |\n| Equipement | 400 |\n| Formation total | "
                + formula
                + ' |\n| Equipement total | =SUMIF(A4:A7,"Equipement",B4:B7) |\n| Grand total | =SUM(B8:B9) |',
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
                self.assertEqual(formulas["1"]["B8"].data_type, "f")
                self.assertEqual(formulas["1"]["B4"].data_type, "n")
                self.assertEqual(
                    [values["1"][f"B{i}"].value for i in (8, 9, 10)], [500, 1000, 1500]
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
                self.content("=UNKNOWN(B4:B7)"),
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
                self.assertAlmostEqual(book["1"]["B8"].value, 0.3)
            finally:
                book.close()

    def test_untrusted_functions_external_references_and_cycles_fail_closed(
        self,
    ) -> None:
        for formula in (
            '=WEBSERVICE("https://example.org")',
            "='[other.xlsx]1'!A1",
            "=SUM(B8)",
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

    def test_generic_workbooks_keep_formula_shaped_text_literal(self) -> None:
        value = self.content().model_dump()
        value["sections"][0]["tables"][0]["formulas"] = False
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "literal.xlsx"
            render(Content.model_validate(value), path, image_root=path.parent)
            book = load_workbook(path)
            try:
                self.assertEqual(book["1"]["B8"].data_type, "s")
            finally:
                book.close()

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
    "| Rent | 800 | | =B4+C4 |\n"
    "| Groceries | 300.5 |  | =B5+C5 |\n"
    "| Total | =SUM(B4:B5) | =SUM(C4:C5) | =SUM(D4:D5) |\n"
    "{extra}"
)


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
                for cell in ("C4", "C5"):
                    self.assertIsNone(formulas["1"][cell].value)
                    self.assertIsNone(sheet[cell].value)
                self.assertEqual(formulas["1"]["C6"].value, "=SUM(C4:C5)")
                self.assertEqual(
                    [sheet[cell].value for cell in ("D4", "D5", "B6", "C6", "D6")],
                    [800, 300.5, 1100.5, 0, 1100.5],
                )
            finally:
                formulas.close()
                values.close()
            with ZipFile(path) as archive:
                xml = archive.read("xl/worksheets/sheet1.xml").decode()
        # Truly empty: the cell is absent from the file, not an empty string.
        self.assertIn('r="B4"', xml)
        self.assertNotIn('r="C4"', xml)
        self.assertNotIn('r="C5"', xml)

    def test_the_publication_path_delivers_the_sheet_with_an_empty_month(self) -> None:
        from services.orchestrator.presentation_delivery import build_project_document

        document = build_project_document(
            self.content(), ".xlsx", [], max_bytes=16777216, timeout=30
        )
        preview = document.preview.decode()
        self.assertIn("=SUM(C4:C5)", preview)
        self.assertNotIn("—", preview)

    def test_formulas_over_empty_cells_compute_what_excel_shows(self) -> None:
        cases = {
            # Arithmetic reads an empty cell as 0.
            "=C4+C5": 0,
            "=B4-C4": 800,
            "=C4*B4": 0,
            "=-C4": 0,
            "=C4*-1": 0,
            "=C4%": 0,
            "=ABS(C4)": 0,
            "=ROUND(C4,2)": 0,
            # Aggregates skip it; over no number they give 0.
            "=SUM(C4:C5)": 0,
            "=SUM(B4:C5)": 1100.5,
            "=COUNT(C4:C5)": 0,
            "=COUNT(B4:C5)": 2,
            "=MIN(C4:C5)": 0,
            "=MAX(C4:C5)": 0,
            "=MIN(B4:C5)": 300.5,
            "=AVERAGE(B4:C5)": 550.25,
            '=SUMIF(A4:A5,"Rent",C4:C5)': 0,
            # Comparisons and conditions: 0, "" and FALSE.
            "=C4=0": True,
            '=C4=""': True,
            "=C4=FALSE": True,
            "=C4>0": False,
            "=IF(C4,1,2)": 2,
            "=NOT(C4)": True,
            '=IF(C4="","to fill in",C4)': "to fill in",
            # Concatenation reads it as "".
            '=C4&"x"': "x",
            # A reference to an empty cell shows 0.
            "=C4": 0,
            # The error of an average or a division over cells still empty is
            # caught by IFERROR, as in Excel.
            '=IFERROR(AVERAGE(C4:C5),"")': None,
            "=IFERROR(B4/C4,0)": 0,
        }
        for formula, expected in cases.items():
            with self.subTest(formula=formula):
                extra = f"| Check | {formula} | | |\n"
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "budget.xlsx"
                    render(self.content(extra), path, image_root=path.parent)
                    book = load_workbook(path, data_only=True)
                    try:
                        found = book["1"]["B7"].value
                    finally:
                        book.close()
                self.assertEqual(found, expected)
                # TRUE is not 1 and "" is not 0 in the stored result.
                self.assertEqual(isinstance(found, bool), isinstance(expected, bool))

    def test_what_excel_shows_as_an_error_is_still_refused_by_name(self) -> None:
        from services.orchestrator.presentation_delivery import build_project_document

        for extra, reason in (
            ("| Check | =AVERAGE(C4:C5) | | |\n", "formula_division_by_zero"),
            ("| Check | =B4/C4 | | |\n", "formula_division_by_zero"),
            # Text is not an empty cell: a written dash still stops arithmetic.
            ("| Check | — | =B7+1 | |\n", "formula_requires_numeric_cells"),
            ("| Check | =A4*2 | | |\n", "formula_requires_numeric_cells"),
            # Outside the written table.
            ("| Check | =SUM(B4:B40) | | |\n", "formula_range_limit"),
            ("| Check | =F4 | | |\n", "formula_range_limit"),
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

    def test_blank_rows_to_fill_in_below_a_total_are_inside_the_table(self) -> None:
        value = normalize(
            dict(
                path="log.xlsx",
                formulas=True,
                content="# Log\n## Expenses\n| Date | Amount |\n| --- | --- |\n"
                "| Total | =SUM(B5:B7) |\n| | |\n| | |\n| | |",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.xlsx"
            render(
                Content.model_validate(value["content"]), path, image_root=path.parent
            )
            book = load_workbook(path, data_only=True)
            try:
                self.assertEqual(book["1"]["B4"].value, 0)
                self.assertEqual(book["1"].max_row, 4)
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
                self.assertIn("=SUM(C4:C5)", "".join(text.split()))
                if suffix == "docx":
                    grid = Document(str(path)).tables[0]
                    self.assertEqual(
                        [cell.text for cell in grid.rows[1].cells],
                        ["Rent", "800", "", "=B4+C4"],
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
                f"| Monday | 5 | 27.5 | {formula} |\n| Thursday | 8 | 44 | =C5/B5 |",
            )
        )
        return Content.model_validate(value["content"])

    def cached(self, formula: str) -> object:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.xlsx"
            render(self.workbook(formula), path, image_root=path.parent)
            book = load_workbook(path, data_only=True)
            try:
                self.assertEqual(book["1"]["D5"].value, 5.5)
                return book["1"]["D4"].value
            finally:
                book.close()

    def test_operators_functions_and_lazy_branches_compute_cached_values(
        self,
    ) -> None:
        cases = {
            "=C4/B4": 5.5,
            "=(C4+0.5)/B4": 5.6,
            '=IF(AND(B4>0,C4>0),C4/B4,"")': 5.5,
            # An empty text result reopens as a blank cell.
            '=IF(B4>100,C4/B4,"")': None,
            '=IF(B4=0,"",C4/B4)': 5.5,
            '=IFERROR(C4/0,"n/a")': "n/a",
            "=ROUND(C4/B4*1.01,1)": 5.6,
            "=ROUND(2.5,0)": 3,
            "=-2^2": 4,
            "=50%*B4": 2.5,
            '=B4&" km"': "5 km",
            "=B4>3": True,
            "=NOT(OR(B4>10,C4>100))": True,
            "=AVERAGE(B4:B5)": 6.5,
            "=MAX(B4:C5)-MIN(B4:C5)": 39,
            "=SUM(B4:B5)/COUNT(B4:B5)": 6.5,
            "=ABS(B4-C4)": 22.5,
        }
        for formula, expected in cases.items():
            with self.subTest(formula=formula):
                self.assertEqual(self.cached(formula), expected)

    def test_formula_failures_name_their_reason_for_the_agent(self) -> None:
        from services.orchestrator.presentation_delivery import build_project_document

        for formula, reason in (
            ("=C4/0", "formula_division_by_zero"),
            ("=A4/B4", "formula_requires_numeric_cells"),
            ("=FOO(B4)", "unsupported_formula_function"),
            ("=B4:B5*2", "formula_range_needs_single_cell"),
            ("=ROUND(B4)", "formula_argument_count"),
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
