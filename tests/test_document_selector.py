"""A document selector that cannot apply to the document type must not stall a Work turn."""

import io
import unittest
from typing import Any

from docx import Document
from openpyxl import Workbook

from packages.attachment_reader import select_units, structured_units


def units(raw: bytes, extension: str) -> list[dict[str, Any]]:
    found, _ = structured_units(raw, extension)
    return found


def word(paragraphs: list[str]) -> bytes:
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class InapplicableSelectorTests(unittest.TestCase):
    def test_page_and_cell_selectors_are_ignored_for_plain_text(self) -> None:
        text = units(b"Alice Martin\nBruno Petit\nBudget 4 000 euros\n", ".txt")
        self.assertEqual(len(select_units(text, {})), 3)
        for selector in (
            {"page_start": 1, "page_end": 1},
            {"page_start": 5},
            {"paragraph_start": 9, "paragraph_end": 12},
            {"range": "A1:C5"},
            {"sheet": "Feuille1", "range": "A1:C5"},
            {"page_start": 1, "page_end": 2, "paragraph_start": 1},
        ):
            with self.subTest(selector=selector):
                self.assertEqual(len(select_units(text, selector)), 3)

    def test_cell_selector_is_ignored_for_a_word_document(self) -> None:
        paragraphs = units(word(["Alice", "Bruno", "Charlie"]), ".docx")
        self.assertEqual(len(select_units(paragraphs, {"range": "A1:C5"})), 3)

    def test_applicable_selectors_stay_strict(self) -> None:
        paragraphs = units(word(["Alice", "Bruno", "Charlie"]), ".docx")
        self.assertTrue(all("paragraph" in unit["locator"] for unit in paragraphs))
        self.assertEqual(
            len(select_units(paragraphs, {"paragraph_start": 1, "paragraph_end": 1})),
            1,
        )
        with self.assertRaisesRegex(ValueError, "matched no stored units"):
            select_units(paragraphs, {"paragraph_start": 99, "paragraph_end": 100})
        with self.assertRaisesRegex(ValueError, "Unknown document selector"):
            select_units(paragraphs, {"unknown": 1})
        with self.assertRaisesRegex(ValueError, "Invalid document range"):
            select_units(paragraphs, {"paragraph_start": 3, "paragraph_end": 1})

    def test_spreadsheet_range_still_needs_a_sheet(self) -> None:
        book = Workbook()
        sheet: Any = book.active
        sheet.title = "Feuille1"
        sheet["A1"], sheet["B1"] = "a", 2
        buffer = io.BytesIO()
        book.save(buffer)
        cells = units(buffer.getvalue(), ".xlsx")
        self.assertTrue(any("cell" in unit["locator"] for unit in cells))
        with self.assertRaisesRegex(ValueError, "Cell range requires a sheet"):
            select_units(cells, {"range": "A1:B1"})
        self.assertTrue(select_units(cells, {"sheet": "Feuille1", "range": "A1:B1"}))
