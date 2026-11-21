"""A delivered file read back from its bytes: what a revision starts from.

A change of a file delivered earlier ("change the title", then "add a
table") is written by the model from the file's current content, as an app
follow-up is written from the stored app (artifact_tools.current_artifact_message,
app_turn.CURRENT_SOURCE). That content is read here from the stored bytes, never
from the conversation: the conversation holds what was asked, the file holds
what was delivered, an earlier change included.

read() gives the document as the model writes one (a title, then sections of
headings, paragraphs, bullets and tables; slides for a deck; sheets for a
workbook; the text of a text file), editable() gives it in the input format of
publish_document, and changes() compares two versions word by word for the
check before a revision is claimed (deliverable_check.py).

The files are those this repository's renderer writes (renderer.py, layout.py):
title, heading and body sizes, bullet indents and one table shape per row are
read back from there. A file built otherwise is still read, with fewer roles
told apart (every text a paragraph). Pictures are counted, never described.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import io
import re
import unicodedata
import zipfile
from typing import Any

# The bytes a stored deliverable may have (terminal_client publishes at most 16 MiB).
MAX_BYTES = 16 * 1024 * 1024
# A workbook is read up to this many cells, a PDF up to this many pages.
MAX_CELLS = 200_000
MAX_PAGES = 300
# The comparison works on words; beyond this many, on lines (cheaper, coarser).
MAX_WORDS = 12_000
TEXT_SUFFIXES = frozenset({".md", ".csv", ".txt", ".html"})
SUFFIXES = frozenset({".docx", ".pptx", ".xlsx", ".pdf", *TEXT_SUFFIXES})
PICTURE = "[picture]"
CHART = "[chart]"


@dataclass
class Section:
    heading: str = ""
    # Slide, sheet or page number where the section starts (1-based); 0 when
    # the format has none (a Word document, a text file).
    unit: int = 0
    paragraphs: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)
    pictures: int = 0
    charts: int = 0
    # Reading order of the blocks, for the comparison: (kind, index).
    order: list[tuple[str, int]] = field(default_factory=list)

    def add(self, kind: str, value: Any) -> None:
        if kind == "paragraph":
            self.order.append((kind, len(self.paragraphs)))
            self.paragraphs.append(value)
        elif kind == "bullet":
            self.order.append((kind, len(self.bullets)))
            self.bullets.append(value)
        elif kind == "table":
            self.order.append((kind, len(self.tables)))
            self.tables.append(value)
        elif kind == "picture":
            self.order.append((kind, self.pictures))
            self.pictures += 1
        elif kind == "chart":
            self.order.append((kind, self.charts))
            self.charts += 1

    def empty(self) -> bool:
        return not (
            self.paragraphs
            or self.bullets
            or self.tables
            or self.pictures
            or self.charts
        )

    def blocks(self) -> list[str]:
        """The section's texts in reading order, a table row per line."""
        lines = [self.heading] if self.heading else []
        for kind, index in self.order:
            if kind == "paragraph":
                lines.append(self.paragraphs[index])
            elif kind == "bullet":
                lines.append(self.bullets[index])
            elif kind == "table":
                lines.extend(" | ".join(row) for row in self.tables[index])
            else:
                lines.append(PICTURE if kind == "picture" else CHART)
        return lines


@dataclass
class Readback:
    suffix: str
    title: str = ""
    sections: list[Section] = field(default_factory=list)
    # A text file is kept whole.
    text: str | None = None

    def blocks(self) -> list[str]:
        if self.text is not None:
            return self.text.splitlines()
        return [self.title, *(line for s in self.sections for line in s.blocks())]


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _package(raw: bytes) -> None:
    """The package checks of the document reader, before any parser runs."""
    from packages.attachment_reader import validate_package

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        validate_package(archive)


def read(raw: bytes, suffix: str) -> Readback:
    """The document a stored file holds. Raises ValueError when it cannot be read."""
    if not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("readback_size")
    if suffix in TEXT_SUFFIXES:
        return Readback(suffix, text=raw.decode("utf-8-sig"))
    if suffix == ".docx":
        _package(raw)
        return _docx(raw)
    if suffix == ".pptx":
        _package(raw)
        return _pptx(raw)
    if suffix == ".xlsx":
        _package(raw)
        return _xlsx(raw)
    if suffix == ".pdf":
        return _pdf(raw)
    raise ValueError("readback_format")


# Word -----------------------------------------------------------------------


def _runs(paragraph: Any, weight: bool = True) -> str:
    """A paragraph's text with its emphasis written as the input writes it."""
    pieces: list[tuple[str, bool, bool]] = []
    for run in paragraph.runs:
        text = run.text.replace("\n", " ").replace("\t", " ")
        if not text:
            continue
        style = (bool(run.bold) and weight, bool(run.italic))
        if pieces and pieces[-1][1:] == style:
            pieces[-1] = (pieces[-1][0] + text, *style)
        else:
            pieces.append((text, *style))
    written = []
    for text, bold, italic in pieces:
        marker = ("**" if bold else "") + ("*" if italic else "")
        core = text.strip()
        if not marker or not core:
            written.append(text)
            continue
        lead = text[: len(text) - len(text.lstrip())]
        tail = text[len(text.rstrip()) :]
        written.append(f"{lead}{marker}{core}{marker[::-1]}{tail}")
    return _one_line("".join(written))


def _docx(raw: bytes) -> Readback:
    from docx import Document
    from docx.table import Table

    document = Document(io.BytesIO(raw))
    result = Readback(".docx", title=_one_line(document.core_properties.title or ""))
    section = Section()
    titled = False
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            rows = [
                [
                    " ".join(_runs(p) for p in cell.paragraphs).strip()
                    for cell in row.cells
                ]
                for row in block.rows
            ]
            if rows:
                section.add("table", rows)
            continue
        style = (block.style.name if block.style is not None else "") or ""
        pictures = len(block._p.xpath(".//w:drawing | .//w:pict"))
        if style == "Title" and not titled:
            titled = True
            result.title = _runs(block, weight=False) or result.title
            continue
        if style.startswith("Heading"):
            text = _runs(block, weight=False)
            if text:
                if not section.empty() or section.heading:
                    result.sections.append(section)
                section = Section(heading=text)
            continue
        text = _runs(block)
        if text:
            section.add(
                "bullet"
                if style.startswith(("List Bullet", "List Number"))
                else "paragraph",
                text,
            )
        for _ in range(pictures):
            section.add("picture", None)
    if not section.empty() or section.heading:
        result.sections.append(section)
    return result


# PowerPoint -----------------------------------------------------------------

# renderer.build_slides: the deck title at 23 pt, a slide's heading at 19 pt,
# its texts at 12 pt; a bullet is indented by 16 pt.
TITLE_SIZE = 21.0
HEADING_SIZE = 17.0
BULLET_INDENT = 8.0
EMU_PER_POINT = 12700


def _size(frame: Any) -> float | None:
    sizes = []
    for paragraph in frame.paragraphs:
        size = paragraph.font.size
        for run in paragraph.runs:
            size = run.font.size or size
        if size is not None:
            sizes.append(size.pt)
    return max(sizes) if sizes else None


def _bulleted(frame: Any) -> bool:
    return any(
        paragraph._p.xpath("./a:pPr/a:buChar | ./a:pPr/a:buAutoNum")
        for paragraph in frame.paragraphs
    )


# A themed deck (deck.py) names each text shape by its role.
ROLE = "atlas:"
# Shapes that are not the content: the footer repeats the deck title, the
# numbers are slide numbers and ordinals, the chart title is the chart's.
FRAME_ROLES = frozenset({"footer", "number", "decor", "chart-title"})


def _themed(shapes: list[Any], section: Section, result: Readback) -> None:
    """Read a themed slide by its shape names: one point is one bullet."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    values: list[str] = []
    table: list[list[str]] | None = None
    for shape in shapes:
        role = shape.name[len(ROLE) :] if shape.name.startswith(ROLE) else ""
        if getattr(shape, "has_table", False) and shape.has_table:
            rows = [
                [_one_line(cell.text) for cell in row.cells] for row in shape.table.rows
            ]
            if table is None:
                table = rows
                section.add("table", table)
            else:
                table.extend(rows)
            continue
        if getattr(shape, "has_chart", False) and shape.has_chart:
            section.add("chart", None)
            table = None
            continue
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            section.add("picture", None)
            table = None
            continue
        if role in FRAME_ROLES or not (
            shape.has_text_frame and shape.text_frame.text.strip()
        ):
            continue
        table = None
        frame = shape.text_frame
        lines = [_one_line(p.text) for p in frame.paragraphs if p.text.strip()]
        text = " ".join(lines)
        if role == "deck-title":
            result.title = result.title or text
        elif role == "title":
            section.heading = text
        elif role == "metric-value":
            values.append(text)
        elif role == "metric-label":
            # Values and labels of a row of key numbers pair up in order.
            value = values.pop(0) if values else ""
            section.add("bullet", f"{text}: {value}" if value else text)
        elif role == "lines":
            for line in lines:
                section.add("bullet", line)
        elif role == "list":
            for paragraph in frame.paragraphs:
                if paragraph.text.strip():
                    bulleted = bool(paragraph._p.xpath("./a:pPr/a:buChar"))
                    section.add(
                        "bullet" if bulleted else "paragraph",
                        _one_line(paragraph.text),
                    )
        else:
            # A card, a column, a step or a statement: its label and text are
            # one point ("Budget :" then "12 400 €" reads "Budget : 12 400 €").
            section.add("bullet", text)


def _pptx(raw: bytes) -> Readback:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    deck = Presentation(io.BytesIO(raw))
    result = Readback(".pptx", title=_one_line(deck.core_properties.title or ""))
    current: Section | None = None
    for number, slide in enumerate(deck.slides, 1):
        shapes = sorted(slide.shapes, key=lambda s: (s.top or 0, s.left or 0))
        texts = [s for s in shapes if s.has_text_frame and s.text_frame.text.strip()]
        left = min(((s.left or 0) for s in texts), default=0)
        section = Section(unit=number)
        headed = False
        table: list[list[str]] | None = None
        themed = any(shape.name.startswith(ROLE) for shape in shapes)
        if themed:
            _themed(shapes, section, result)
        for shape in shapes if not themed else ():
            if getattr(shape, "has_table", False) and shape.has_table:
                rows = [
                    [_one_line(cell.text) for cell in row.cells]
                    for row in shape.table.rows
                ]
                if table is None:
                    table = rows
                    section.add("table", table)
                else:
                    # renderer.build_slides draws a table row per shape.
                    table.extend(rows)
                continue
            if getattr(shape, "has_chart", False) and shape.has_chart:
                section.add("chart", None)
                table = None
                continue
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                section.add("picture", None)
                table = None
                continue
            if not (shape.has_text_frame and shape.text_frame.text.strip()):
                continue
            table = None
            frame = shape.text_frame
            size = _size(frame)
            if _bulleted(frame) and len(frame.paragraphs) > 1:
                items = [_one_line(p.text) for p in frame.paragraphs if p.text.strip()]
            else:
                # A text box holds one text, one paragraph per line laid out.
                items = [_one_line(" ".join(p.text for p in frame.paragraphs))]
            if number == 1 and not result.title and size and size >= TITLE_SIZE:
                result.title = items[0]
                continue
            if (
                number == 1
                and items[0] == result.title
                and not section.order
                and not headed
            ):
                if size is None or size >= TITLE_SIZE:
                    continue
            if (
                not headed
                and not section.order
                and (size is None or size >= HEADING_SIZE)
            ):
                headed = True
                section.heading = items[0]
                continue
            bullet = (
                _bulleted(frame)
                or (shape.left or 0) - left > BULLET_INDENT * EMU_PER_POINT
            )
            for item in items:
                section.add("bullet" if bullet else "paragraph", item)
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = _one_line(slide.notes_slide.notes_text_frame.text)
        if notes:
            section.add("paragraph", notes)
        if number == 1 and not section.heading:
            # Under the title, a heading that repeats it is not drawn again.
            section.heading = result.title
        if current is not None and (
            not section.heading or section.heading == current.heading
        ):
            # A section too long for one slide continues on the next under
            # its heading again (layout.pages); a table repeats its header.
            _continue(current, section)
            continue
        if not section.empty() or section.heading:
            result.sections.append(section)
            current = section
    if not result.title and result.sections:
        result.title = result.sections[0].heading
    return result


def _continue(current: Section, section: Section) -> None:
    """Add a continued slide to its section. A text too long for the slide
    goes on at the top of the next one, without its bullet (layout.pages)."""
    for position, (kind, index) in enumerate(section.order):
        if (
            position == 0
            and kind == "paragraph"
            and current.order
            and current.order[-1][0] in ("paragraph", "bullet")
        ):
            previous_kind, previous = current.order[-1]
            texts = (
                current.paragraphs if previous_kind == "paragraph" else current.bullets
            )
            texts[previous] += " " + section.paragraphs[index]
            continue
        if kind == "table":
            rows = section.tables[index]
            last = next(
                (current.tables[i] for k, i in reversed(current.order) if k == "table"),
                None,
            )
            if last is not None and current.order[-1][0] == "table" and rows:
                last.extend(rows[1:] if rows[0] == last[0] else rows)
                continue
            current.add("table", rows)
        elif kind == "paragraph":
            current.add(kind, section.paragraphs[index])
        elif kind == "bullet":
            current.add(kind, section.bullets[index])
        else:
            current.add(kind, None)


# Excel ----------------------------------------------------------------------


def number_text(value: float | int, shown: str) -> str:
    """A stored number written back as the formats cells.number gives show it."""
    if shown == "General" or not shown:
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    percent = shown.rstrip().endswith("%")
    if percent:
        value = value * 100
    money = re.search(r'"([^"]+)"', shown)
    pattern = re.sub(r'"[^"]*"|%', "", shown).strip()
    decimals = len(pattern.split(".", 1)[1]) if "." in pattern else 0
    grouped = "," in pattern
    body = f"{abs(value):,.{decimals}f}" if grouped else f"{abs(value):.{decimals}f}"
    sign = "-" if value < 0 else ""
    if percent:
        gap = " " if re.search(r"\s%", shown) else ""
        return f"{sign}{body}{gap}%"
    if money:
        before = shown.lstrip().startswith('"')
        gap = " " if re.search(r'"\s|\s"', shown) else ""
        symbol = money[1]
        return f"{sign}{symbol}{gap}{body}" if before else f"{sign}{body}{gap}{symbol}"
    return sign + body


def _cell(cell: Any) -> str:
    value = cell.value
    if value is None:
        return ""
    if cell.data_type == "f" or (isinstance(value, str) and value.startswith("=")):
        return str(value)
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        text = number_text(value, cell.number_format)
    elif hasattr(value, "isoformat"):
        text = value.isoformat()
    else:
        text = _one_line(str(value))
        if getattr(cell, "quotePrefix", False) and text.startswith("="):
            return "'" + text
    if not text:
        return ""
    link = cell.hyperlink.target if cell.hyperlink is not None else None
    if link:
        return f"[{text}]({link})"
    font = cell.font
    marker = ("**" if font is not None and font.b else "") + (
        "*" if font is not None and font.i else ""
    )
    return f"{marker}{text}{marker[::-1]}" if marker else text


def _xlsx(raw: bytes) -> Readback:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(raw), data_only=False)
    try:
        result = Readback(".xlsx", title=_one_line(workbook.properties.title or ""))
        cells = 0
        for number, sheet in enumerate(workbook.worksheets, 1):
            cells += (sheet.max_row or 0) * (sheet.max_column or 0)
            if cells > MAX_CELLS:
                raise ValueError("readback_cells")
            rows: list[list[str]] = []
            for row in sheet.iter_rows():
                values = [_cell(cell) for cell in row]
                while values and not values[-1]:
                    values.pop()
                rows.append(values)
            result.sections.append(
                _sheet(rows, number, result, len(getattr(sheet, "_images", ())))
            )
        return result
    finally:
        workbook.close()


def _sheet(
    rows: list[list[str]], number: int, result: Readback, pictures: int
) -> Section:
    """renderer.build_workbook: the first table at A1 and an empty row, then the
    texts one per row in column A (the title first on the first sheet), then
    the other tables."""
    section = Section(unit=number)
    index = 0
    if rows and len(rows[0]) > 1:
        table: list[list[str]] = []
        while index < len(rows) and rows[index]:
            table.append(rows[index])
            index += 1
        section.add("table", _rectangle(table))
    texts: list[str] = []
    while index < len(rows) and len(rows[index]) <= 1:
        if rows[index]:
            texts.append(rows[index][0])
        index += 1
    rest = [row for row in rows[index:] if row]
    if number == 1 and texts and result.title and texts[0].strip("*") == result.title:
        # The title opens the first sheet, then the first heading unless it
        # repeats the title: either way the title heads a section whose texts
        # are written in the same cells.
        pass
    elif not result.title and number == 1 and texts:
        result.title = texts[0]
    if texts:
        section.heading = texts[0]
        for text in texts[1:]:
            section.add("paragraph", text)
    if section.order and section.order[0][0] == "table":
        # The input writes a section's texts before its tables.
        section.order = [o for o in section.order if o[0] != "table"] + [
            o for o in section.order if o[0] == "table"
        ]
    if rest:
        section.add("table", _rectangle(rest))
    for _ in range(pictures):
        section.add("picture", None)
    if not section.heading:
        section.heading = str(number)
    return section


def _rectangle(rows: list[list[str]]) -> list[list[str]]:
    width = max(len(row) for row in rows)
    return [row + [""] * (width - len(row)) for row in rows]


# PDF ------------------------------------------------------------------------

# renderer.build_pdf: title 23 pt, headings 19 pt, texts 12 pt (bullets
# indented by 16 pt), tables 11, 9 or 7 pt (layout.table_type) with these
# line spacings; texts are spaced at 1.35 times their size.
TABLE_LEADING = {11.0: 15.0, 9.0: 12.0, 7.0: 10.0}


@dataclass
class _Line:
    page: int
    x: float
    y: float
    size: float
    text: str


def _pdf_lines(raw: bytes) -> tuple[str, list[_Line]]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw), strict=False)
    if reader.is_encrypted:
        raise ValueError("readback_encrypted")
    if len(reader.pages) > MAX_PAGES:
        raise ValueError("readback_pages")
    title = ""
    if reader.metadata is not None and reader.metadata.title:
        title = _one_line(str(reader.metadata.title))
    lines: list[_Line] = []
    for number, page in enumerate(reader.pages, 1):
        pieces: list[_Line] = []

        def visit(text: str, cm: Any, tm: Any, font: Any, size: float) -> None:
            if text.strip():
                x = float(tm[4]) * float(cm[0]) + float(cm[4])
                y = float(tm[5]) * float(cm[3]) + float(cm[5])
                pieces.append(
                    _Line(number, x, y, round(float(size) * float(cm[0]), 1), text)
                )

        page.extract_text(visitor_text=visit)
        # One drawn line may be several pieces (an emoji drawn between two runs).
        merged: list[_Line] = []
        for piece in pieces:
            last = merged[-1] if merged else None
            if (
                last is not None
                and abs(last.y - piece.y) < 0.5
                and last.size == piece.size
                and piece.x > last.x
                and not _table_size(piece.size)
            ):
                last.text += piece.text
            else:
                merged.append(piece)
        for line in merged:
            line.text = _one_line(line.text)
        lines.extend(line for line in merged if line.text)
    return title, lines


def _table_size(size: float) -> bool:
    return size < 11.5


def _pdf(raw: bytes) -> Readback:
    title, lines = _pdf_lines(raw)
    result = Readback(".pdf", title=title)
    margin = min((line.x for line in lines), default=0.0)
    section = Section(unit=1)
    page_title: list[str] = []
    index = 0

    def follows(previous: _Line, line: _Line, leading: float) -> bool:
        """The next line of the same block: just below, or at the top of the
        next page when the block was split there (layout.pages)."""
        if line.size != previous.size or abs(line.x - previous.x) >= 0.5:
            return False
        if line.page == previous.page:
            return 0 < previous.y - line.y <= leading * 1.25
        return line.page == previous.page + 1 and first[id(line)] and last[id(previous)]

    first = {
        id(line): i == 0 or lines[i - 1].page != line.page
        for i, line in enumerate(lines)
    }
    last = {
        id(line): i == len(lines) - 1 or lines[i + 1].page != line.page
        for i, line in enumerate(lines)
    }
    while index < len(lines):
        line = lines[index]
        if (
            line.size >= TITLE_SIZE
            and line.page == 1
            and not section.order
            and not section.heading
        ):
            page_title.append(line.text)
            index += 1
            continue
        if line.size >= HEADING_SIZE:
            words = [line.text]
            index += 1
            while index < len(lines) and follows(
                lines[index - 1], lines[index], line.size * 1.35
            ):
                words.append(lines[index].text)
                index += 1
            if not section.empty() or section.heading:
                result.sections.append(section)
            section = Section(heading=" ".join(words), unit=line.page)
            continue
        if _table_size(line.size):
            rows, end = _pdf_table(lines, index)
            table = (
                section.tables[section.order[-1][1]]
                if section.order and section.order[-1][0] == "table"
                else None
            )
            if table is not None and first[id(line)] and len(rows[0]) == len(table[0]):
                # A table continued on the next page (its rows only).
                table.extend(rows)
            else:
                section.add("table", rows)
            index = end
            continue
        kind = "bullet" if line.x - margin > BULLET_INDENT else "paragraph"
        words = [line.text]
        index += 1
        while index < len(lines) and follows(
            lines[index - 1], lines[index], line.size * 1.35
        ):
            words.append(lines[index].text)
            index += 1
        section.add(kind, " ".join(words))
    if not section.empty() or section.heading:
        result.sections.append(section)
    if page_title:
        result.title = " ".join(page_title)
    return result


def _pdf_table(lines: list[_Line], index: int) -> tuple[list[list[str]], int]:
    """The rows of a table: cells by column position, a new row after the
    spacing of a row (layout.pages: a row is its line spacing plus a padding).

    A row is drawn column by column, so a cell of several lines goes down and
    the next column starts up again: only a step down larger than a line
    starts a row.
    """
    start = index
    while (
        index < len(lines)
        and _table_size(lines[index].size)
        and lines[index].page == lines[start].page
    ):
        index += 1
    region = lines[start:index]
    columns = sorted({round(line.x, 1) for line in region})
    leading = TABLE_LEADING.get(region[0].size, region[0].size * 1.36)
    rows: list[list[list[str]]] = []
    baseline: float | None = None
    for line in region:
        if baseline is None or baseline - line.y > leading * 1.4:
            rows.append([[] for _ in columns])
        baseline = line.y
        rows[-1][columns.index(round(line.x, 1))].append(line.text)
    return [[" ".join(cell) for cell in row] for row in rows], index


# The input format of publish_document -----------------------------------------


def _markdown_table(rows: list[list[str]]) -> list[str]:
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    return [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join("---" for _ in rows[0]) + " |",
        *("| " + " | ".join(row) + " |" for row in rows[1:]),
    ]


def section_markdown(section: Section, title: str = "") -> str:
    """A section in the Markdown of publish_document; a first section headed
    by the title is written under it, without its heading again."""
    lines: list[str] = []
    if section.heading and section.heading != title:
        lines.extend(["## " + section.heading, ""])
    for kind, index in section.order:
        if kind == "paragraph":
            lines.extend([section.paragraphs[index], ""])
        elif kind == "bullet":
            lines.append("- " + section.bullets[index])
            following = section.order[section.order.index((kind, index)) + 1 :]
            if not following or following[0][0] != "bullet":
                lines.append("")
        elif kind == "table":
            if len(section.tables[index]) > 1:
                lines.extend([*_markdown_table(section.tables[index]), ""])
            else:
                lines.extend([" | ".join(section.tables[index][0]), ""])
    return "\n".join(lines).strip()


def slide(section: Section) -> dict[str, Any]:
    """A slide as publish_document takes one (schema.PresentationSlide)."""
    value: dict[str, Any] = {"title": section.heading or "-"}
    bullets = list(section.bullets) or list(section.paragraphs)
    value["bullets"] = bullets or ["-"]
    if section.bullets and section.paragraphs:
        value["notes"] = " ".join(section.paragraphs)
    if section.tables and len(section.tables[0]) > 1:
        value["table"] = {
            "headers": section.tables[0][0],
            "rows": section.tables[0][1:],
        }
    return value


def editable(document: Readback) -> dict[str, Any]:
    """The document in the arguments publish_document takes for its format."""
    if document.text is not None:
        return {"content": document.text}
    if document.suffix == ".pptx":
        return {
            "title": document.title,
            "slides": [slide(s) for s in document.sections],
        }
    return {
        "title": document.title,
        "content": "\n\n".join(
            section_markdown(section, document.title if index == 0 else "")
            for index, section in enumerate(document.sections)
        ),
    }


def outline(document: Readback) -> list[dict[str, Any]]:
    """Every part of the document, named and counted, without its text."""
    if document.text is not None:
        lines = document.text.splitlines()
        return [{"lines": len(lines), "first_line": lines[0][:120] if lines else ""}]
    parts = []
    for number, section in enumerate(document.sections, 1):
        entry: dict[str, Any] = {"part": number, "heading": section.heading[:160]}
        if section.unit:
            entry[
                {".pptx": "slide", ".xlsx": "sheet", ".pdf": "page"}.get(
                    document.suffix, "unit"
                )
            ] = section.unit
        for name, count in (
            ("paragraphs", len(section.paragraphs)),
            ("bullets", len(section.bullets)),
            ("tables", len(section.tables)),
            ("pictures", section.pictures),
            ("charts", section.charts),
        ):
            if count:
                entry[name] = count
        if section.tables:
            entry["table_rows"] = [len(table) for table in section.tables]
        first = next(iter(section.paragraphs or section.bullets), "")
        if first:
            entry["starts"] = first[:120]
        parts.append(entry)
    return parts


# The comparison of two versions ----------------------------------------------

# Characters that differ between versions without changing what a person reads:
# typographic quotes and dashes, and the spaces the font adapts (glyphs.adapt).
_SAME = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‛": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "«": '"',
        "»": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
        " ": " ",
        " ": " ",
    }
)
_MARKERS = re.compile(r"\*{1,3}|_{2}")


def normal(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_SAME)
    return " ".join(_MARKERS.sub("", text).split())


def _tokens(document: Readback) -> list[str]:
    words = [word for block in document.blocks() for word in normal(block).split()]
    if len(words) <= MAX_WORDS:
        return words
    return [normal(block) for block in document.blocks() if normal(block)]


CONTEXT_WORDS = 6
ENTRY_CHARACTERS = 300
MAX_ENTRIES = 10


def _join(words: list[str]) -> str:
    text = " ".join(words)
    return text if len(text) <= ENTRY_CHARACTERS else text[: ENTRY_CHARACTERS - 1] + "…"


def changes(before: Readback, after: Readback) -> dict[str, Any]:
    """What changed from one version to the next, measured on their texts.

    Tolerated (not changes): spaces and line breaks (a page or a slide lays the
    same text out again on other lines), typographic quotes, dashes and narrow
    spaces (the font adapts them), emphasis markers, and text that moved
    without changing (reported as moved). Everything else is reported: a word
    removed, replaced or added, a table cell, a picture.
    """
    old, new = _tokens(before), _tokens(after)
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    removed: list[dict[str, str]] = []
    replaced: list[dict[str, str]] = []
    added: list[dict[str, str]] = []
    kept = 0
    for kind, a, b, c, d in matcher.get_opcodes():
        if kind == "equal":
            kept += b - a
            continue
        context = _join(old[max(0, a - CONTEXT_WORDS) : a])
        if kind == "delete":
            removed.append({"text": _join(old[a:b]), "after": context})
        elif kind == "insert":
            added.append({"text": _join(new[c:d]), "after": context})
        else:
            replaced.append(
                {
                    "before": _join(old[a:b]),
                    "after": _join(new[c:d]),
                    "following": context,
                }
            )
    # A passage removed in one place and added in another moved.
    inserted = {entry["text"]: entry for entry in added}
    moved = [entry for entry in removed if entry["text"] in inserted]
    for entry in moved:
        removed.remove(entry)
        added.remove(inserted[entry["text"]])
    count = len(removed) + len(replaced) + len(added)
    result: dict[str, Any] = {
        "identical": count == 0 and not moved,
        "kept_share": round(kept / len(old), 3) if old else 1.0,
        "removed": removed[:MAX_ENTRIES],
        "replaced": replaced[:MAX_ENTRIES],
        "added": added[:MAX_ENTRIES],
    }
    if moved:
        result["moved"] = [entry["text"] for entry in moved[:MAX_ENTRIES]]
    shown = len(result["removed"]) + len(result["replaced"]) + len(result["added"])
    if count > shown:
        result["more_changes"] = count - shown
    return result
