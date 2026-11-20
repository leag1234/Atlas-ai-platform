"""Offline builders; no templates containing user-visible boilerplate."""

from collections.abc import Callable
from datetime import datetime, timezone
from io import BytesIO
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.shared import Inches as DocInches
from docx.shared import Pt as DocPt
from docx.enum.text import WD_BREAK
from docx.text.paragraph import Paragraph
from openpyxl import Workbook
from openpyxl.drawing.image import Image as SheetImage
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen.canvas import Canvas

from . import emoji
from .cells import (
    PAGE_COLUMNS,
    SHEET_COLUMNS,
    Reading,
    Span,
    prose,
    shown,
    whole_link,
)
from .layout import (
    Geometry,
    report_geometry,
    FONT,
    SLIDE,
    image_path,
    pages,
    table_type,
)
from .schema import Content
from .verify import Verification, verify

EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def normalize_zip(path: Path) -> None:
    output = BytesIO()
    with ZipFile(path) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for name in sorted(source.namelist()):
            data = source.read(name)
            if name == "docProps/core.xml":
                # Normalize library-generated timestamps and author/template metadata.
                from lxml import etree

                root = etree.fromstring(data)
                for element in root:
                    local = etree.QName(element).localname
                    if local in {"created", "modified"}:
                        element.text = "2000-01-01T00:00:00Z"
                    elif local not in {"title"}:
                        element.text = ""
                data = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
            info = ZipInfo(name, (2000, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            target.writestr(info, data)
    path.write_bytes(output.getvalue())


def build_slides(content: Content, path: Path, root: Path) -> None:
    """A themed deck (deck.py): layouts follow from what each slide holds."""
    from .deck import build_deck

    build_deck(content, path, root)


def draw_line(canvas: Canvas, x: float, y: float, line: str, size: float) -> None:
    """A line at its baseline: text in the face, each emoji as its picture.

    The picture has the emoji font's own ascent and descent around the
    baseline, at the size of the text (emoji.py).
    """
    run = ""
    for token in [*emoji.tokens(line), ""]:
        drawn = emoji.picture(token) if token and emoji.is_emoji(token) else None
        if drawn is None and token:
            run += token
            continue
        if run:
            canvas.drawString(x, y, run)
            x += pdfmetrics.stringWidth(run, FONT, size)
            run = ""
        if drawn is not None:
            canvas.drawImage(
                ImageReader(BytesIO(drawn.png)),
                x,
                y - (drawn.height - drawn.ascent) * size,
                width=drawn.advance * size,
                height=drawn.height * size,
                mask="auto",
            )
            x += drawn.advance * size


def build_pdf(
    content: Content, path: Path, root: Path, geometry: Geometry | None = None
) -> None:
    """A report on A4 pages; a presentation passes the slide geometry."""
    g = geometry or report_geometry(content)
    layout = pages(content, root, geometry=g)
    canvas = Canvas(
        str(path),
        pagesize=(g.width, g.height),
        invariant=1,
        pageCompression=1,
        bottomup=1,
    )
    canvas.setTitle(shown(content.title))
    canvas.setAuthor("")
    for page in layout:
        for placed in page:
            block, top = placed.block, placed.top
            if block.image:
                canvas.drawImage(
                    str(block.image),
                    g.margin,
                    g.height - top - block.height + 8,
                    width=block.width,
                    height=block.height - 8,
                    mask="auto",
                )
                continue
            canvas.setFont(FONT, block.size)
            if block.bullet:
                canvas.circle(g.margin + 5, g.height - top - 8, 2, fill=1, stroke=0)
            for col, lines in enumerate(block.lines):
                cell_width = block.width / len(block.lines)
                x = g.margin + col * cell_width
                if block.table:
                    canvas.rect(
                        x, g.height - top - block.height, cell_width, block.height
                    )
                x += block.pad if block.table else (16 if block.bullet else 0)
                for line_index, line in enumerate(lines):
                    leading = block.leading if block.table else block.size * 1.35
                    draw_line(
                        canvas,
                        x,
                        g.height - top - block.size - 3 - line_index * leading,
                        line,
                        block.size,
                    )
        canvas.showPage()
    canvas.save()


def write_runs(paragraph: Paragraph, text: str, *, weight: bool = True) -> None:
    """A text as runs: its emphasis is read, its markers are never written.

    A heading keeps the weight of its style ("## **Summary**" is a habit of
    writing, not a request for another weight) and takes italics only.
    """
    for span in prose(text):
        run = paragraph.add_run(span.text)
        if span.bold and weight:
            run.bold = True
        if span.italic:
            run.italic = True


def build_document(content: Content, path: Path, root: Path) -> None:
    document = Document()
    document.core_properties.title = shown(content.title)
    wide = any(
        len(table.headers) > 8
        for section in content.sections
        for table in section.tables
    )
    if wide:
        # A table of more than eight columns gets the long side of the page
        # and narrow side margins.
        page = document.sections[0]
        page.orientation = WD_ORIENT.LANDSCAPE
        page.page_width, page.page_height = page.page_height, page.page_width
        page.left_margin = page.right_margin = DocInches(0.5)
    write_runs(document.add_heading("", 0), content.title, weight=False)
    for index, section in enumerate(content.sections):
        if index:
            document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        if index or not content.repeats_title():
            write_runs(document.add_heading("", 1), section.heading, weight=False)
        for text in section.paragraphs:
            paragraph = document.add_paragraph()
            write_runs(paragraph, text)
            if emoji.pictures_only(shown(text)):
                for run in paragraph.runs:
                    run.font.size = DocPt(emoji.PICTURE_SIZE)
        for text in section.bullets:
            write_runs(document.add_paragraph(style="List Bullet"), text)
        for table in section.tables:
            grid = document.add_table(rows=1, cols=len(table.headers))
            grid.style = "Table Grid"
            size = table_type(len(table.headers))[0]
            small = size < 11
            for position, row in enumerate(table.read()):
                cells = grid.rows[0].cells if position == 0 else grid.add_row().cells
                for cell, reading in zip(cells, row, strict=True):
                    if not small and reading.spans in ((), (Span(reading.text),)):
                        cell.text = reading.text
                        continue
                    for span in reading.spans:
                        run = cell.paragraphs[0].add_run(span.text)
                        run.bold = span.bold or None
                        run.italic = span.italic or None
                        if small:
                            run.font.size = DocPt(size)
        for value in section.images:
            document.add_picture(str(image_path(root, value)), width=DocInches(4))
    document.save(str(path))


def build_workbook(content: Content, path: Path, root: Path) -> None:
    from .spreadsheet import table_areas

    # Where each sheet's table is, checked before anything is written.
    areas = table_areas(content)
    workbook = Workbook()
    workbook.remove(workbook.worksheets[0])
    workbook.properties.title = shown(content.title)
    workbook.properties.created = workbook.properties.modified = EPOCH
    # The written area of each sheet, empty table cells included: a formula may
    # reference an empty cell of the table, never a cell outside what was written.
    extents: dict[str, tuple[int, int]] = {}
    for index, section in enumerate(content.sections):
        # Numeric tabs are structural identifiers, never invented document content.
        sheet = workbook.create_sheet(str(index + 1))
        texts = [section.heading, *section.paragraphs, *section.bullets]
        if index == 0 and not content.repeats_title():
            texts.insert(0, content.title)
        # The first table starts at cell A1: header in row 1, first data row
        # in row 2. A reference then means the same cell to the author of the
        # table, in the file and to the person who opens it. Texts written
        # above it shifted every formula (a total summed the heading, the
        # header and half of the rows, staging 2026-11-12). The title, the
        # heading and the texts follow the table after one empty row.
        rows: list[tuple[Reading, ...]] = []
        formula_rows: set[int] = set()
        if section.tables:
            first = section.tables[0]
            # A cell of this table that starts with "=" is a formula, whether
            # or not the table was flagged (see Table.holds_formulas).
            formula_rows.update(range(2, 2 + len(first.rows)))
            rows.extend(first.read())
            rows.append(())
        # A text is read as a cell is: its markers are never shown, and the
        # cell is bold or italic when the whole text is. It is never a number
        # nor a formula.
        rows.extend((Reading(prose(text), link=whole_link(text)),) for text in texts)
        for table in section.tables[1:]:
            # No formula here: table_areas refused it.
            rows.extend(table.read())
        extents[sheet.title] = (len(rows), max(map(len, rows)))
        typed: dict[str, tuple[float, str]] = {}
        computed: dict[str, str] = {}
        # Up to eight columns keep one comfortable width; a wider sheet fits
        # each column to what it holds, so twelve months stay in view.
        widths: dict[int, int] = {}
        fitted = extents[sheet.title][1] > 8
        for row_index, row in enumerate(rows, 1):
            for column, reading in enumerate(row, 1):
                fit = min(32, max(10, len(reading.text) + 4)) if fitted else 32
                widths[column] = max(widths.get(column, 0), fit)
                if not reading.text:
                    # An empty table cell is a truly empty cell: no value, no
                    # empty string. Formulas read it as Excel does.
                    continue
                cell = sheet.cell(row_index, column, reading.text)
                cell.data_type = "s"
                if reading.quoted:
                    # Typed "'=A1": the text "=A1", marked as Excel marks a
                    # cell entered with an apostrophe.
                    cell.quotePrefix = True
                elif row_index in formula_rows and reading.text.startswith("="):
                    cell.data_type = "f"
                    computed[cell.coordinate] = reading.text
                elif reading.number is not None:
                    # A cell typed as a number is one in every table, shown as
                    # typed: a sum never skips "12.50" or "1 250,00 €".
                    cell.value = reading.number
                    cell.number_format = reading.number_format
                    typed[cell.coordinate] = (reading.number, reading.number_format)
                elif reading.link:
                    cell.hyperlink = reading.link
                cell.font = Font(
                    name="Bitstream Vera Sans",
                    size=11,
                    bold=reading.bold or None,
                    italic=reading.italic or None,
                )
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        for column, width in widths.items():
            sheet.column_dimensions[get_column_letter(column)].width = width
        if computed:
            from .spreadsheet import total_formats

            for coordinate, display in total_formats(typed, computed).items():
                sheet[coordinate].number_format = display
        image_row = len(rows) + 2
        for value in section.images:
            image = SheetImage(str(image_path(root, value)))
            scale = min(400 / image.width, 180 / image.height)
            image.width = int(image.width * scale)
            image.height = int(image.height * scale)
            sheet.add_image(image, f"A{image_row}")
            image_row += 15
    workbook.save(path)
    workbook.close()
    if any(
        table.holds_formulas()
        for section in content.sections
        for table in section.tables
    ):
        from .formulas import cache_formulas

        cache_formulas(path, extents, areas)


def render(
    content: Content, path: Path, *, image_root: Path, slides: bool = False
) -> Verification:
    """Build and verify atomically; destination must be chosen by trusted code.

    With slides, a PDF has one slide-size page per slide, as the PPTX deck.
    """
    content = Content.model_validate(content.model_dump())
    if path.suffix != ".pptx":
        # Speaker notes, key numbers and charts are text and tables on pages.
        content = content.flattened()
    if path.suffix == ".xlsx":
        from .spreadsheet import bind_summaries

        content = bind_summaries(content)
    limit = SHEET_COLUMNS if path.suffix == ".xlsx" else PAGE_COLUMNS
    if any(
        len(table.headers) > limit
        for section in content.sections
        for table in section.tables
    ):
        raise ValueError(f"table_columns_limit:{limit}")
    # Apply identical font and image bounds for every output format; table rows
    # are laid out on pages for the formats that have pages.
    pages(
        content,
        image_root,
        tables=path.suffix != ".xlsx",
        geometry=report_geometry(content)
        if path.suffix == ".pdf" and not slides
        else SLIDE,
    )
    builders: dict[str, Callable[[Content, Path, Path], None]] = {
        ".pptx": build_slides,
        ".pdf": partial(build_pdf, geometry=SLIDE if slides else None),
        ".docx": build_document,
        ".xlsx": build_workbook,
    }
    if path.suffix not in builders:
        raise ValueError("unsupported_output_format")
    with TemporaryDirectory(prefix="doc-render-", dir=path.parent) as temporary:
        staged = Path(temporary) / path.name
        builders[path.suffix](content, staged, image_root)
        if path.suffix != ".pdf":
            normalize_zip(staged)
        result = verify(staged, expected=content)
        staged.replace(path)
    return result
