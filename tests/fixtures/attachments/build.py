"""Deterministic public attachment fixtures for chat attachment tests."""

import io
from pathlib import Path

from docx import Document
from pypdf import PdfWriter


def build(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    pdf = root / "Rapport annuel été (v2).pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO()
    writer.write(stream)
    pdf.write_bytes(stream.getvalue())

    docx = root / "Compte rendu – réunion du 3 mars.docx"
    document = Document()
    document.add_paragraph("Synthetic public meeting note.")
    document.save(str(docx))

    long_name = root / ("Rapport public " + "très long " * 12 + "(annexe).txt")
    long_name.write_text("Synthetic public attachment.")
    return {"scanned_pdf": pdf, "docx": docx, "long_name": long_name}
