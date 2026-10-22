"""Offline builders; no templates containing user-visible boilerplate."""

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from docx import Document
from docx.shared import Inches as DocInches
from docx.enum.text import WD_BREAK
from openpyxl import Workbook
from openpyxl.drawing.image import Image as SheetImage
from openpyxl.styles import Alignment, Font
from pptx import Presentation
from pptx.util import Pt
from reportlab.pdfgen.canvas import Canvas

from .layout import FONT, HEIGHT, MARGIN, WIDTH, image_path, pages
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
                    cell.margin_left = cell.margin_right = Pt(6)
                    cell.margin_top = cell.margin_bottom = Pt(3)
                    for paragraph in cell.text_frame.paragraphs:
                        paragraph.font.name = "Bitstream Vera Sans"
                        paragraph.font.size = Pt(block.size)
                        paragraph.line_spacing = Pt(15)
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
                x += 6 if block.table else (16 if block.bullet else 0)
                for line_index, line in enumerate(lines):
                    leading = 15 if block.table else block.size * 1.35
                    canvas.drawString(
                        x, HEIGHT - top - block.size - 3 - line_index * leading, line
                    )
        canvas.showPage()
    canvas.save()


def build_document(content: Content, path: Path, root: Path) -> None:
    document = Document()
    document.core_properties.title = content.title
    document.add_heading(content.title, 0)
    for index, section in enumerate(content.sections):
        if index:
            document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        document.add_heading(section.heading, 1)
        for text in section.paragraphs:
            document.add_paragraph(text)
        for text in section.bullets:
            document.add_paragraph(text, style="List Bullet")
        for table in section.tables:
            grid = document.add_table(rows=1, cols=len(table.headers))
            grid.style = "Table Grid"
            for cell, text in zip(grid.rows[0].cells, table.headers, strict=True):
                cell.text = text
            for row in table.rows:
                for cell, text in zip(grid.add_row().cells, row, strict=True):
                    cell.text = text
        for value in section.images:
            document.add_picture(str(image_path(root, value)), width=DocInches(4))
    document.save(str(path))


def build_workbook(content: Content, path: Path, root: Path) -> None:
    workbook = Workbook()
    workbook.remove(workbook.worksheets[0])
    workbook.properties.title = content.title
    workbook.properties.created = workbook.properties.modified = EPOCH
    for index, section in enumerate(content.sections):
        # Numeric tabs are structural identifiers, never invented document content.
        sheet = workbook.create_sheet(str(index + 1))
        rows: list[tuple[str, ...]] = []
        if index == 0:
            rows.append((content.title,))
        rows.extend(
            [
                (section.heading,),
                *((text,) for text in section.paragraphs),
                *((text,) for text in section.bullets),
            ]
        )
        for table in section.tables:
            rows.extend((table.headers, *table.rows))
        for row_index, row in enumerate(rows, 1):
            for column, value in enumerate(row, 1):
                cell = sheet.cell(row_index, column, value)
                cell.data_type = "s"
                cell.font = Font(name="Bitstream Vera Sans", size=11)
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                sheet.column_dimensions[cell.column_letter].width = 32
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


def render(content: Content, path: Path, *, image_root: Path) -> Verification:
    """Build and verify atomically; destination must be chosen by trusted code."""
    content = Content.model_validate(content.model_dump())
    # Apply identical font, image and layout bounds for every output format.
    pages(content, image_root)
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
