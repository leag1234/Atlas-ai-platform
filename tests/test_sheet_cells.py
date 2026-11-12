"""REQ-PROJ-05: table cells are read as typed (numbers, emphasis), wide
tables fit a workbook, and a refused document says what to correct."""

import json
import tempfile
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock

from docx import Document
from docx.enum.section import WD_ORIENT
from openpyxl import load_workbook
from packages.doc_render import verify
from packages.doc_render.cells import notation, number, numeric_looking, spans
from packages.doc_render.schema import Content
from packages.doc_render.spreadsheet import total_formats
from services.orchestrator.presentation_delivery import build_project_document
from services.orchestrator.project_document_input import normalize
from services.orchestrator.project_document_tools import execute

MONTHS = (
    "Janvier",
    "Février",
    "Mars",
    "Avril",
    "Mai",
    "Juin",
    "Juillet",
    "Août",
    "Septembre",
    "Octobre",
    "Novembre",
    "Décembre",
)


def content(markdown: str, suffix: str = ".xlsx", *, formulas: bool = False) -> Content:
    fields: dict[str, Any] = dict(path="sheet" + suffix, content=markdown)
    if formulas:
        fields["formulas"] = True
    return Content.model_validate(normalize(fields)["content"])


def table(*rows: str, headers: str = "| Item | Amount |") -> str:
    width = headers.count("|") - 1
    return (
        "# Budget\n## Costs\n"
        + headers
        + "\n|"
        + " --- |" * width
        + "\n"
        + "\n".join(rows)
    )


def delivered(value: Content, suffix: str = ".xlsx") -> bytes:
    return build_project_document(
        value, suffix, [], max_bytes=16777216, timeout=30
    ).content


class NumberReadingTests(unittest.TestCase):
    def test_french_and_english_numbers_are_read_with_their_display(self) -> None:
        cases: dict[str, tuple[int | float, str]] = {
            "12.50": (12.5, "0.00"),
            "12": (12, "General"),
            "-5": (-5, "General"),
            "−5,5": (-5.5, "0.0"),
            "0,5": (0.5, "0.0"),
            "12,50": (12.5, "0.00"),
            "12.00": (12, "0.00"),
            "1 250": (1250, "#,##0"),
            "1 250,00 €": (1250, '#,##0.00 "€"'),
            "1 250,00 €": (1250, '#,##0.00 "€"'),
            "1 250,5 €": (1250.5, '#,##0.0 "€"'),
            "1,250.00": (1250, "#,##0.00"),
            "1,250,000": (1250000, "#,##0"),
            "$40": (40, '"$"0'),
            "$1,250.50": (1250.5, '"$"#,##0.00'),
            "-$5": (-5, '"$"0'),
            "€ 12": (12, '"€" 0'),
            "12 EUR": (12, '0 "EUR"'),
            "45 %": (0.45, "0 %"),
            "45%": (0.45, "0%"),
            "12,5 %": (0.125, "0.0 %"),
            "33.3%": (0.333, "0.0%"),
            "0,250": (0.25, "0.000"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                found = number(text)
                self.assertEqual(found, expected)
                assert found is not None
                self.assertIs(type(found[0]), type(expected[0]))

    def test_what_may_be_something_else_stays_text(self) -> None:
        for text in (
            "007",
            "01,5",
            "2026-11-12",
            "12/11/2026",
            "12.11.2026",
            "06 12 34 56 78",
            "+33 6 12 34 56 78",
            "1/2",
            "1.2.3",
            "12:30",
            "1e5",
            "12.5 kg",
            "(5.00)",
            "+5",
            "1.250,00",
            "1234567890123456",
            "1,250",
            "1.250",
            "$5 %",
            "n/a",
            "",
        ):
            with self.subTest(text=text):
                self.assertIsNone(number(text))

    def test_one_group_of_three_digits_follows_the_notation_of_its_table(self) -> None:
        self.assertEqual(notation(("12.50", "1,250")), "english")
        self.assertEqual(notation(("$1,250", "$2,400")), "english")
        self.assertEqual(notation(("12,50", "1,250")), "french")
        self.assertEqual(notation(("1,250", "2,400")), "")
        self.assertEqual(notation(("12.5", "12,5")), "")
        self.assertEqual(notation(("1.250,00", "12,5")), "")
        self.assertEqual(number("1,250", "english"), (1250, "#,##0"))
        self.assertEqual(number("1,250", "french"), (1.25, "0.000"))
        self.assertEqual(number("1.250", "english"), (1.25, "0.000"))
        self.assertIsNone(number("1.250", "french"))
        self.assertEqual(number("$1,250", "english"), (1250, '"$"#,##0'))

    def test_a_total_is_shown_like_the_cells_it_adds_up(self) -> None:
        euro, plain_euro = '#,##0.00 "€"', '0.00 "€"'
        typed = {
            "B4": (1250, euro),
            "B5": (145.5, plain_euro),
            "C4": (1250, euro),
            "C5": (89.9, plain_euro),
            "E4": (5, "General"),
            "E5": (0.45, "0 %"),
            "F4": (1250.5, "0.00"),
            "F5": (12.5, "0.00"),
        }
        computed = {
            "B6": "=SUM(B4:B5)",
            "C6": "=C4+C5",
            "D4": "=SUM(B4:C4)",
            "D6": "=sum(D4:D5)",
            "D5": '=SUMIF(A4:A5,"x",B4:B5)',
            "E6": "=SUM(E4:E5)",
            "F6": "=AVERAGE(F4:F5)",
            "G6": "=B4/C4",
            "H6": "=COUNT(B4:B5)",
        }
        self.assertEqual(
            total_formats(typed, computed),
            {"B6": euro, "C6": euro, "D4": euro, "D5": euro, "D6": euro, "F6": "0.00"},
        )


class NumberCellTests(unittest.TestCase):
    def reopened(self, value: Content) -> tuple[Any, Any]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sheet.xlsx"
            path.write_bytes(delivered(value))
            formulas = load_workbook(path, data_only=False)
            values = load_workbook(path, data_only=True)
            self.addCleanup(formulas.close)
            self.addCleanup(values.close)
            return formulas["1"], values["1"]

    def test_typed_numbers_are_numbers_in_a_table_without_formulas(self) -> None:
        sheet, _ = self.reopened(
            content(
                table(
                    "| Rent | 1 250,00 € |",
                    "| Power | 12.50 |",
                    "| Share | 45 % |",
                    "| Units | 12 |",
                    "| Code | 007 |",
                    "| Day | 2026-11-12 |",
                    "| Phone | 06 12 34 56 78 |",
                    "| Half | 1/2 |",
                    "| Version | 1.2.3 |",
                )
            )
        )
        expected: dict[str, tuple[str, Any, str]] = {
            "B4": ("n", 1250, '#,##0.00 "€"'),
            "B5": ("n", 12.5, "0.00"),
            "B6": ("n", 0.45, "0 %"),
            "B7": ("n", 12, "General"),
            "B8": ("s", "007", "General"),
            "B9": ("s", "2026-11-12", "General"),
            "B10": ("s", "06 12 34 56 78", "General"),
            "B11": ("s", "1/2", "General"),
            "B12": ("s", "1.2.3", "General"),
            # A header is a name, never a number.
            "B3": ("s", "Amount", "General"),
        }
        for coordinate, (kind, value, shown) in expected.items():
            with self.subTest(coordinate=coordinate):
                cell = sheet[coordinate]
                self.assertEqual(
                    (cell.data_type, cell.value, cell.number_format),
                    (kind, value, shown),
                )

    def test_a_sum_counts_numbers_typed_with_decimals_and_currency(self) -> None:
        sheet, values = self.reopened(
            content(
                table(
                    "| Rent | 1 250,00 € |",
                    "| Power | 145,50 € |",
                    "| Water | 12,50 € |",
                    "| **TOTAL** | **=SUM(B4:B6)** |",
                ),
                formulas=True,
            )
        )
        self.assertEqual(sheet["B7"].data_type, "f")
        self.assertEqual(sheet["B7"].value, "=SUM(B4:B6)")
        self.assertEqual(values["B7"].value, 1408)
        self.assertEqual(sheet["B7"].number_format, '#,##0.00 "€"')
        self.assertEqual(sheet["A7"].value, "TOTAL")
        self.assertTrue(sheet["A7"].font.b)

    def test_numbers_like_text_are_never_skipped_in_silence(self) -> None:
        refused = (
            # Thousands or decimals? Nothing in the table says.
            ("| a | 1,250 |", "=SUM(B4:B5)"),
            ("| a | 1,250 |", "=IFERROR(SUM(B4:B5),0)"),
            ("| a | 1,250 |", "=AVERAGE(B4:B5)"),
            ("| a | 1,250 |", "=COUNT(B4:B5)"),
            ("| a | 1,250 |", '=IF(B4>100,"high","low")'),
            ("| a | 1,250 |", '=IFERROR(B4*2,"")'),
            ("| a | 1,250 |", '=SUMIF(A4:A5,"a",B4:B5)'),
            ("| a | 2 kg |", "=SUM(B4:B5)"),
            ("| a | 2 kg |", "=B4+B5"),
        )
        for row, formula in refused:
            with self.subTest(row=row, formula=formula):
                with self.assertRaisesRegex(
                    ValueError,
                    r"formula_requires_numeric_cells:B4:sheet_1 \(the code ends",
                ):
                    delivered(
                        content(
                            table(row, "| b | 300 |", f"| t | {formula} |"),
                            formulas=True,
                        )
                    )

    def test_a_label_in_a_range_is_skipped_and_the_notation_settles_1_250(
        self,
    ) -> None:
        _, values = self.reopened(
            content(
                table("| a | n/a |", "| b | 300 |", "| t | =SUM(B4:B5) |"),
                formulas=True,
            )
        )
        self.assertEqual(values["B6"].value, 300)
        sheet, values = self.reopened(
            content(
                table("| a | 1,250 |", "| b | 12.50 |", "| t | =SUM(B4:B5) |"),
                formulas=True,
            )
        )
        self.assertEqual(
            (sheet["B4"].value, sheet["B4"].number_format), (1250, "#,##0")
        )
        self.assertEqual(values["B6"].value, 1262.5)
        sheet, values = self.reopened(
            content(
                table("| a | 1,250 |", "| b | 12,50 |", "| t | =SUM(B4:B5) |"),
                formulas=True,
            )
        )
        self.assertEqual(
            (sheet["B4"].value, sheet["B4"].number_format), (1.25, "0.000")
        )
        self.assertEqual(values["B6"].value, 13.75)

    def test_a_criterion_and_a_source_excerpt_meet_a_number_as_typed(self) -> None:
        value = content(
            table(
                "| 12.50 | 1 250,00 € |",
                "| 12.50 | 300 |",
                "| 7 | 40 |",
                '| t | =SUMIF(A4:A6,"12.50",B4:B6) |',
                "| u | =SUMIF(A4:A6,7,B4:B6) |",
                headers="| Rate | Amount |",
            ),
            formulas=True,
        )
        _, values = self.reopened(value)
        self.assertEqual((values["B7"].value, values["B8"].value), (1550, 40))
        # The excerpt of a source is found in the cell as typed.
        document = build_project_document(
            value, ".xlsx", ["1 250,00 €", "12.50"], max_bytes=16777216, timeout=30
        )
        self.assertIn(b"1250", document.preview)
        with self.assertRaisesRegex(ValueError, "Source coverage verification failed"):
            build_project_document(
                value, ".xlsx", ["1 350,00 €"], max_bytes=16777216, timeout=30
            )

    def test_other_formats_keep_what_was_typed(self) -> None:
        value = content(table("| Rent | 1 250,00 € |", "| Share | 45 % |"), ".docx")
        for suffix in (".docx", ".pdf"):
            with (
                self.subTest(suffix=suffix),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / ("sheet" + suffix)
                path.write_bytes(delivered(value, suffix))
                text = "".join("".join(verify(path).text).split())
                self.assertIn("1250,00€", text)
                self.assertIn("45%", text)


class WideTableTests(unittest.TestCase):
    def budget(self, suffix: str, *, formulas: bool) -> Content:
        headers = "| Poste | " + " | ".join(MONTHS) + " | Total |"
        rows = []
        for index, (label, amount) in enumerate(
            (("Loyer", "1 250,00 €"), ("Énergie", "145,50 €"), ("Eau", "12,50 €")), 4
        ):
            total = f"=SUM(B{index}:M{index})" if formulas else "17 825,00 €"
            rows.append(f"| {label} | " + " | ".join([amount] * 12) + f" | {total} |")
        if formulas:
            columns = "BCDEFGHIJKLMN"
            rows.append(
                "| **TOTAL** | "
                + " | ".join(f"=SUM({column}4:{column}6)" for column in columns)
                + " |"
            )
        return content(table(*rows, headers=headers), suffix, formulas=formulas)

    def test_twelve_months_in_columns_compute_in_a_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budget.xlsx"
            path.write_bytes(delivered(self.budget(".xlsx", formulas=True)))
            formulas = load_workbook(path, data_only=False)
            values = load_workbook(path, data_only=True)
            try:
                sheet, cached = formulas["1"], values["1"]
                self.assertEqual(sheet.max_column, 14)
                self.assertEqual(
                    [cell.value for cell in sheet[3]], ["Poste", *MONTHS, "Total"]
                )
                self.assertEqual(sheet["M4"].data_type, "n")
                self.assertEqual(sheet["M4"].value, 1250)
                self.assertEqual(sheet["M5"].number_format, '0.00 "€"')
                self.assertEqual(cached["N4"].value, 15000)
                self.assertEqual(cached["N5"].value, 1746)
                self.assertEqual(cached["M7"].value, 1408)
                self.assertEqual(cached["N7"].value, 16896)
                for coordinate in ("N4", "N5", "M7", "N7"):
                    self.assertEqual(sheet[coordinate].data_type, "f")
                    self.assertEqual(sheet[coordinate].number_format, '#,##0.00 "€"')
                # Fourteen columns stay in view: no column keeps the wide default.
                self.assertTrue(
                    all(
                        sheet.column_dimensions[letter].width < 32
                        for letter in "ABCDEFGHIJKLMN"
                    )
                )
            finally:
                formulas.close()
                values.close()

    def test_a_workbook_takes_sixty_columns_and_no_more(self) -> None:
        def wide(count: int) -> str:
            headers = "| " + " | ".join(f"W{i}" for i in range(1, count + 1)) + " |"
            return table(
                "| " + " | ".join(str(i) for i in range(1, count + 1)) + " |",
                headers=headers,
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wide.xlsx"
            path.write_bytes(delivered(content(wide(60))))
            book = load_workbook(path)
            try:
                self.assertEqual(book["1"].max_column, 60)
                self.assertEqual(book["1"]["BH4"].value, 60)
            finally:
                book.close()
        with self.assertRaisesRegex(
            ValueError, r"tables\[0\]\.headers: at most 60 items here, 61 given"
        ):
            content(wide(61))

    def test_pages_and_slides_show_fourteen_columns_and_refuse_more_by_name(
        self,
    ) -> None:
        value = self.budget(".pdf", formulas=False)
        for suffix in (".pdf", ".docx", ".pptx"):
            with (
                self.subTest(suffix=suffix),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / ("budget" + suffix)
                path.write_bytes(delivered(value, suffix))
                text = verify(path, expected=value).text
                # A month and an amount each stay on one line of their cell.
                self.assertIn("Septembre", "\n".join(text))
                self.assertIn("1 250,00 €", "\n".join(text))
                if suffix == ".docx":
                    page = Document(str(path)).sections[0]
                    self.assertEqual(page.orientation, WD_ORIENT.LANDSCAPE)
                    assert page.page_width and page.page_height
                    self.assertGreater(page.page_width, page.page_height)
        headers = "| " + " | ".join(f"W{i}" for i in range(1, 16)) + " |"
        fifteen = content(
            table("| " + " | ".join("1" for _ in range(15)) + " |", headers=headers),
            ".pdf",
        )
        for suffix in (".pdf", ".docx", ".pptx"):
            with (
                self.subTest(suffix=suffix),
                self.assertRaisesRegex(
                    ValueError,
                    r"table_columns_limit:14 \(the number is the most columns",
                ),
            ):
                delivered(fifteen, suffix)
        self.assertGreater(len(delivered(fifteen, ".xlsx")), 100)

    def test_a_table_of_eight_columns_keeps_the_upright_page(self) -> None:
        headers = "| " + " | ".join(f"W{i}" for i in range(1, 9)) + " |"
        value = content(
            table("| " + " | ".join("1" for _ in range(8)) + " |", headers=headers),
            ".docx",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upright.docx"
            path.write_bytes(delivered(value, ".docx"))
            page = Document(str(path)).sections[0]
            assert page.page_width and page.page_height
            self.assertLess(page.page_width, page.page_height)


class EmphasisTests(unittest.TestCase):
    def test_markers_are_read_at_word_boundaries_only(self) -> None:
        cases: dict[str, list[tuple[str, bool, bool]]] = {
            "**TOTAL**": [("TOTAL", True, False)],
            "*note*": [("note", False, True)],
            "***both***": [("both", True, True)],
            "**Total** HT": [("Total", True, False), (" HT", False, False)],
            "`code`": [("code", False, False)],
            "[Report](https://example.org/a_(b))": [("Report", False, False)],
            "\\*kept\\*": [("*kept*", False, False)],
            # Not emphasis: products, arguments, patterns, identifiers.
            "2*3*4": [("2*3*4", False, False)],
            "5 * 3 * 2": [("5 * 3 * 2", False, False)],
            "*args, **kwargs": [("*args, **kwargs", False, False)],
            "*.txt *.md": [("*.txt *.md", False, False)],
            "__init__": [("__init__", False, False)],
            "items[i](x)": [("items[i](x)", False, False)],
            "****": [("****", False, False)],
            "=A1*B1*C1": [("=A1*B1*C1", False, False)],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(
                    [(span.text, span.bold, span.italic) for span in spans(text)],
                    expected,
                )
        self.assertTrue(numeric_looking("1,250"))
        self.assertFalse(numeric_looking("n/a"))

    def rows(self) -> tuple[str, ...]:
        return (
            "| *Rent* | 1 250,00 € | `fixed` |",
            "| Power | 2*3*4 | [Contract](https://example.org/contract) |",
            "| **TOTAL** | **1 262,50 €** | see **notes** below |",
        )

    def test_a_workbook_styles_the_cell_and_never_shows_the_markers(self) -> None:
        for strict in (False, True):
            value = content(table(*self.rows(), headers="| **Item** | Amount | Note |"))
            if strict:
                # The same cells sent as a strict JSON object.
                value = Content.model_validate(
                    normalize(
                        dict(path="sheet.xlsx", content=json.dumps(value.model_dump()))
                    )["content"]
                )
            with (
                self.subTest(strict=strict),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "sheet.xlsx"
                path.write_bytes(delivered(value))
                book = load_workbook(path)
                try:
                    sheet = book["1"]
                    shown = [
                        [cell.value for cell in row] for row in sheet.iter_rows(3, 6)
                    ]
                    self.assertEqual(
                        shown,
                        [
                            ["Item", "Amount", "Note"],
                            ["Rent", 1250, "fixed"],
                            ["Power", "2*3*4", "Contract"],
                            ["TOTAL", 1262.5, "see notes below"],
                        ],
                    )
                    self.assertTrue(sheet["A3"].font.b)
                    self.assertTrue(sheet["A4"].font.i)
                    self.assertFalse(sheet["A4"].font.b)
                    self.assertTrue(sheet["A6"].font.b)
                    self.assertTrue(sheet["B6"].font.b)
                    self.assertEqual(sheet["B6"].number_format, '#,##0.00 "€"')
                    self.assertFalse(sheet["C6"].font.b)
                    self.assertFalse(sheet["A5"].font.b)
                    self.assertEqual(
                        sheet["C5"].hyperlink.target, "https://example.org/contract"
                    )
                finally:
                    book.close()

    def test_a_document_writes_bold_runs_and_pages_drop_the_markers(self) -> None:
        value = content(
            table(*self.rows(), headers="| **Item** | Amount | Note |"), ".docx"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sheet.docx"
            path.write_bytes(delivered(value, ".docx"))
            grid = Document(str(path)).tables[0]
            self.assertEqual(grid.cell(3, 0).text, "TOTAL")
            self.assertTrue(grid.cell(3, 0).paragraphs[0].runs[0].bold)
            self.assertTrue(grid.cell(1, 0).paragraphs[0].runs[0].italic)
            runs = grid.cell(3, 2).paragraphs[0].runs
            self.assertEqual(
                [(run.text, bool(run.bold)) for run in runs],
                [("see ", False), ("notes", True), (" below", False)],
            )
            self.assertEqual(grid.cell(2, 1).text, "2*3*4")
            self.assertEqual(grid.cell(2, 2).text, "Contract")
            for suffix in (".pdf", ".docx"):
                other = Path(directory) / ("pages" + suffix)
                other.write_bytes(delivered(value, suffix))
                text = "\n".join(verify(other, expected=value).text)
                self.assertIn("TOTAL", text)
                self.assertIn("see notes below", text)
                self.assertNotIn("**", text)
                self.assertNotIn("`", text)
                self.assertNotIn("](", text)
        slide = dict(
            title="Costs",
            bullets=["One table"],
            table=dict(headers=["Item", "Amount"], rows=[["**TOTAL**", "`12.50`"]]),
        )
        deck = Content.model_validate(
            normalize(dict(path="deck.pptx", title="Budget", slides=[slide]))["content"]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deck.pptx"
            path.write_bytes(delivered(deck, ".pptx"))
            text = "\n".join(verify(path, expected=deck).text)
            self.assertIn("TOTAL", text)
            self.assertNotIn("*", text)
            self.assertNotIn("`", text)


class RefusalMessageTests(unittest.IsolatedAsyncioTestCase):
    def refusal(self, **fields: Any) -> str:
        with self.assertRaises(ValueError) as caught:
            normalize(dict(sources=[], **fields))
        message = str(caught.exception)
        # Nothing internal: no report header, class name or documentation link.
        for internal in ("validation error", "pydantic", "https://", "Content\n"):
            self.assertNotIn(internal, message)
        self.assertNotIn("\n", message)
        self.assertLess(len(message), 700)
        return message

    def strict(self, section: dict[str, Any]) -> str:
        return self.refusal(
            path="costs.xlsx",
            content=json.dumps(dict(title="Costs", sections=[section])),
        )

    def test_a_refused_object_names_the_field_and_its_rule(self) -> None:
        grid = dict(headers=["Item", "Amount"], rows=[["Rent", "1250"]])
        self.assertEqual(
            self.strict(dict(heading="Year", table=grid)),
            "Invalid document arguments. content.sections[0].table: unknown field "
            '(did you mean "tables"?); fields allowed here: heading, paragraphs, '
            "bullets, tables, images. Correct these fields and call the tool again.",
        )
        cases = (
            (dict(tables=[grid]), "content.sections[0].heading: this required field"),
            (
                dict(heading="Year", tables=[dict(grid, style="plain")]),
                "content.sections[0].tables[0].style: unknown field; fields allowed "
                "here: formulas, headers, rows",
            ),
            (
                dict(heading="Year", tables=[dict(grid, rows=[["Rent"]])]),
                "content.sections[0].tables[0]: every row needs exactly as many "
                "cells as the table has headers",
            ),
            (
                dict(heading="Year", tables=[dict(grid, rows=[["Rent", 1250]])]),
                "content.sections[0].tables[0].rows[0][1]: Input should be a valid "
                "string",
            ),
            (
                dict(
                    heading="Year",
                    tables=[dict(grid, headers=["", " "], rows=[["", ""]])],
                ),
                "content.sections[0].tables[0]: a table needs at least one column name",
            ),
            (dict(heading="Year"), "content.sections[0]: a section needs at least one"),
        )
        for section, expected in cases:
            with self.subTest(expected=expected):
                self.assertIn(expected, self.strict(section))
        self.assertIn(
            "content.sections[0]: an object is expected here with the fields: heading",
            self.refusal(
                path="costs.xlsx",
                content=json.dumps(dict(title="Costs", sections=["Year"])),
            ),
        )

    def test_broken_json_markdown_and_slides_are_explained_too(self) -> None:
        self.assertIn(
            "content starts like JSON but is not valid JSON (line 1, column",
            self.refusal(path="costs.xlsx", content='{"title": "Costs", "sections": ['),
        )
        self.assertIn(
            "content.sections[0].tables[0]: every row needs exactly as many cells",
            self.refusal(
                path="costs.docx", content="| A | B |\n| --- | --- |\n| one |"
            ),
        )
        self.assertIn(
            'slides[0].bullet: unknown field (did you mean "bullets"?)',
            self.refusal(
                path="deck.pptx",
                title="Costs",
                slides=[dict(title="Year", bullet=["One"])],
            ),
        )

    async def test_the_tool_result_carries_the_short_refusal(self) -> None:
        tools = Mock()
        tools.execution.check = AsyncMock()
        calls: tuple[tuple[str, dict[str, Any], str], ...] = (
            (
                "publish_document",
                dict(path="costs.docx", content="Rent", sources=[], strategy="unsafe"),
                "strategy: Input should be 'created', 'regenerated' or "
                "'edited_in_place'",
            ),
            (
                "publish_document",
                dict(path="costs.md", sources=[]),
                "content: this required field is missing",
            ),
            (
                "read_document",
                dict(version="one"),
                "file_id: this required field is missing",
            ),
        )
        for name, arguments, expected in calls:
            with self.subTest(name=name, expected=expected):
                with self.assertRaises(ValueError) as caught:
                    await execute(tools, name, json.dumps(arguments), 30)
                message = str(caught.exception)
                self.assertTrue(message.startswith("Invalid document arguments. "))
                self.assertIn(expected, message)
                self.assertNotIn("validation error", message)
                self.assertNotIn("\n", message)


if __name__ == "__main__":
    unittest.main()
