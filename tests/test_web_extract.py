"""Downloaded PDF evidence uses the existing PDF parser, not HTML extraction."""

from io import BytesIO
import unittest

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from packages.web_extract import extract


class WebExtractionTests(unittest.TestCase):
    def test_pdf_preserves_text_after_first_page(self) -> None:
        writer = PdfWriter()
        writer.add_blank_page(width=600, height=800)
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 12 Tf 20 700 Td (Late technical evidence) Tj ET")
        page[NameObject("/Contents")] = content
        buffer = BytesIO()
        writer.write(buffer)
        self.assertIn(
            "Late technical evidence",
            extract(buffer.getvalue(), "https://example.org/manual.pdf"),
        )
        writer.encrypt("test-only")
        encrypted = BytesIO()
        writer.write(encrypted)
        with self.assertRaisesRegex(ValueError, "encrypted_pdf"):
            extract(encrypted.getvalue(), "https://example.org/manual.pdf")
        with self.assertRaisesRegex(ValueError, "invalid_pdf"):
            extract(b"%PDF-broken", "https://example.org/manual.pdf")
