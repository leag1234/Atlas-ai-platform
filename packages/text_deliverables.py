"""Plain-text project deliverables: Markdown, CSV, text and HTML files.

The bytes are the deliverable. Validation is deterministic and local: strict
UTF-8, non-empty, and for CSV a parse with a consistent column count. HTML
content is not interpreted here; downloads are always attachments with
nosniff, so it never renders on the Atlas origin.
"""

from __future__ import annotations

import csv
import html.parser
import io

TEXT_MIMES = {
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".html": "text/html",
}
# Same bound as the model-facing Markdown content of Office deliverables.
TEXT_CHARACTER_LIMIT = 220000


class TextDeliverableError(ValueError):
    """A named validation failure; the code is safe to show to the model."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_text(content: bytes, suffix: str) -> str:
    """Return the decoded text, or raise a named TextDeliverableError."""
    if suffix not in TEXT_MIMES:
        raise TextDeliverableError("unsupported_text_format")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise TextDeliverableError("text_not_utf8") from None
    if not text.strip():
        raise TextDeliverableError("text_empty")
    if suffix == ".csv":
        csv_rows(text)
    return text


def csv_rows(text: str) -> list[list[str]]:
    try:
        rows = [
            row for row in csv.reader(io.StringIO(text, newline=""), strict=True) if row
        ]
    except csv.Error:
        raise TextDeliverableError("csv_parse_error") from None
    if not rows:
        raise TextDeliverableError("text_empty")
    if any(len(row) != len(rows[0]) for row in rows):
        raise TextDeliverableError("csv_inconsistent_columns")
    return rows


class _Text(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def visible_text(text: str, suffix: str) -> str:
    """Text a reader sees, used only to check selected source coverage."""
    if suffix == ".csv":
        return "\n".join(cell for row in csv_rows(text) for cell in row)
    if suffix == ".html":
        parser = _Text()
        parser.feed(text)
        parser.close()
        return "".join(parser.parts)
    return text
