"""Offline builders; no templates containing user-visible boilerplate."""

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.shared import Inches as DocInches
from docx.shared import Pt as DocPt
from docx.enum.text import WD_BREAK
from openpyxl import Workbook
from openpyxl.drawing.image import Image as SheetImage
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from pptx import Presentation
from pptx.util import Pt
from reportlab.pdfgen.canvas import Canvas

from .cells import PAGE_COLUMNS, SHEET_COLUMNS, Reading, Span
from .layout import FONT, HEIGHT, MARGIN, WIDTH, image_path, pages, table_type
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
    deck = Presentation()
    deck.slide_width, deck.slide_height = Pt(WIDTH), Pt(HEIGHT)
    deck.core_properties.title = content.title
    for page in pages(content, root):
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        for placed in page:
            block, top = placed.block, placed.top
            if block.image:
                slide.shapes.add_picture(
                    str(block.image),
                    Pt(MARGIN),
                    Pt(top),
                    width=Pt(block.width),
                    height=Pt(block.height - 8),
                )
                continue
            if block.table:
                table = slide.shapes.add_table(
                    1,
                    len(block.lines),
                    Pt(MARGIN),
                    Pt(top),
                    Pt(block.width),
                    Pt(block.height),
                ).table
                for col, lines in enumerate(block.lines):
                    cell = table.cell(0, col)
                    cell.text = "\n".join(lines)
                    cell.margin_left = cell.margin_right = Pt(block.pad)
                    cell.margin_top = cell.margin_bottom = Pt(3)
                    for paragraph in cell.text_frame.paragraphs:
                        paragraph.font.name = "Bitstream Vera Sans"
                        paragraph.font.size = Pt(block.size)
                        paragraph.line_spacing = Pt(block.leading)
            else:
                indent = 16 if block.bullet else 0
                box = slide.shapes.add_textbox(
                    Pt(MARGIN + indent),
                    Pt(top),
                    Pt(block.width - indent),
                    Pt(block.height),
                )
                frame = box.text_frame
                frame.margin_left = frame.margin_right = Pt(0)
                frame.margin_top = frame.margin_bottom = Pt(0)
                frame.word_wrap = False
                for index, line in enumerate(block.lines[0]):
                    paragraph = (
                        frame.paragraphs[0] if index == 0 else frame.add_paragraph()
                    )
                    paragraph.text = line
                    paragraph.font.name = "Bitstream Vera Sans"
                    paragraph.font.size = Pt(block.size)
                    paragraph.line_spacing = Pt(block.size * 1.35)
                    paragraph.space_after = Pt(0)
                if block.bullet:
                    from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE

                    slide.shapes.add_shape(
                        MSO_AUTO_SHAPE_TYPE.OVAL,
                        Pt(MARGIN + 3),
                        Pt(top + 5),
                        Pt(4),
                        Pt(4),
                    )
    deck.save(str(path))


def build_pdf(content: Content, path: Path, root: Path) -> None:
    layout = pages(content, root)
    canvas = Canvas(
        str(path), pagesize=(WIDTH, HEIGHT), invariant=1, pageCompression=1, bottomup=1
    )
    canvas.setTitle(content.title)
    canvas.setAuthor("")
    for page in layout:
        for placed in page:
            block, top = placed.block, placed.top
            if block.image:
                canvas.drawImage(
                    str(block.image),
                    MARGIN,
                    HEIGHT - top - block.height + 8,
                    width=block.width,
                    height=block.height - 8,
                    mask="auto",
                )
                continue
            canvas.setFont(FONT, block.size)
            if block.bullet:
                canvas.circle(MARGIN + 5, HEIGHT - top - 8, 2, fill=1, stroke=0)
            for col, lines in enumerate(block.lines):
                cell_width = block.width / len(block.lines)
                x = MARGIN + col * cell_width
                if block.table:
                    canvas.rect(
                        x, HEIGHT - top - block.height, cell_width, block.height
                    )
                x += block.pad if block.table else (16 if block.bullet else 0)
                for line_index, line in enumerate(lines):
                    leading = block.leading if block.table else block.size * 1.35
                    canvas.drawString(
                        x, HEIGHT - top - block.size - 3 - line_index * leading, line
                    )
        canvas.showPage()
    canvas.save()


def build_document(content: Content, path: Path, root: Path) -> None:
    document = Document()
    document.core_properties.title = content.title
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
    document.add_heading(content.title, 0)
    for index, section in enumerate(content.sections):
        if index:
            document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        if index or not content.repeats_title():
            document.add_heading(section.heading, 1)
        for text in section.paragraphs:
            document.add_paragraph(text)
        for text in section.bullets:
            document.add_paragraph(text, style="List Bullet")
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
    workbook.properties.title = content.title
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
            if first.formulas:
                formula_rows.update(range(2, 2 + len(first.rows)))
            rows.extend(first.read())
            rows.append(())
        rows.extend((Reading((Span(text),)),) for text in texts)
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
                if row_index in formula_rows and reading.text.startswith("="):
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

            for coordinate, shown in total_formats(typed, computed).items():
                sheet[coordinate].number_format = shown
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
    if any(table.formulas for section in content.sections for table in section.tables):
        from .formulas import cache_formulas

        cache_formulas(path, extents, areas)


def render(content: Content, path: Path, *, image_root: Path) -> Verification:
    """Build and verify atomically; destination must be chosen by trusted code."""
    content = Content.model_validate(content.model_dump())
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
    pages(content, image_root, tables=path.suffix != ".xlsx")
    builders = {
        ".pptx": build_slides,
        ".pdf": build_pdf,
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
