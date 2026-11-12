"""Measured layout shared by the PDF writer and slide builder."""

from dataclasses import dataclass
from pathlib import Path

import reportlab
from PIL import Image
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from .cells import shown
from .schema import Content

FONT = "AtlasContentVera"
WIDTH, HEIGHT, MARGIN = 720.0, 405.0, 30.0
BODY_WIDTH = WIDTH - 2 * MARGIN


@dataclass(frozen=True)
class Geometry:
    """The page a format lays content on.

    A slide starts each section on a new slide and repeats its heading when it
    continues. A PDF report is an A4 portrait page where sections follow each
    other: on 16:9 slide-size pages, one section per page, a short report took
    16 pages filled to a quarter (staging 2026-11-12).
    """

    width: float
    height: float
    margin: float = MARGIN
    flow: bool = False
    image_height: float = 180.0

    @property
    def body(self) -> float:
        return self.width - 2 * self.margin


SLIDE = Geometry(WIDTH, HEIGHT)
A4 = Geometry(595.28, 841.89, 50.0, flow=True, image_height=320.0)
# A table of more than eight columns (twelve months and a total) needs the width.
A4_LANDSCAPE = Geometry(841.89, 595.28, 40.0, flow=True, image_height=260.0)


def report_geometry(content: Content) -> Geometry:
    """The page of a PDF report: A4 portrait, landscape for a wide table."""
    wide = any(
        len(table.headers) > 8
        for section in content.sections
        for table in section.tables
    )
    return A4_LANDSCAPE if wide else A4


@dataclass(frozen=True)
class Block:
    lines: tuple[tuple[str, ...], ...] = ()
    size: float = 12
    image: Path | None = None
    width: float = BODY_WIDTH
    height: float = 0
    table: bool = False
    bullet: bool = False
    # Table rows only: line spacing and the horizontal padding of a cell.
    leading: float = 15
    pad: float = 6


@dataclass(frozen=True)
class Placed:
    block: Block
    top: float


def font() -> TTFont:
    path = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
    registered = TTFont(FONT, str(path))
    pdfmetrics.registerFont(registered)
    return registered


def wrap(text: str, width: float, size: float) -> tuple[str, ...]:
    # Character wrapping preserves supplied whitespace and long unbroken values.
    lines: list[str] = []
    current = ""
    for character in text:
        if pdfmetrics.stringWidth(current + character, FONT, size) > width:
            if not current:
                raise ValueError("glyph_exceeds_line_width")
            lines.append(current)
            current = ""
        current += character
    if current:
        lines.append(current)
    return tuple(lines)


def table_type(columns: int) -> tuple[float, float, float, float]:
    """Font size, line spacing, side padding and row padding of a table.

    More than eight columns are set smaller and tighter: a label, twelve
    months and a total hold "1 250,00 €" on one line each.
    """
    if columns <= 8:
        return 11.0, 15.0, 6.0, 12.0
    if columns <= 11:
        return 9.0, 12.0, 3.0, 8.0
    return 7.0, 10.0, 2.0, 8.0


def text_block(
    text: str, size: float = 12, *, bullet: bool = False, body: float = BODY_WIDTH
) -> Block:
    lines = wrap(text, body - (16 if bullet else 0), size)
    return Block(
        lines=(lines,),
        size=size,
        height=len(lines) * size * 1.35 + 8,
        bullet=bullet,
        width=body,
    )


def image_path(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError("image_path_must_be_relative")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("image_outside_root")
    if path.stat().st_size > 10_000_000:
        raise ValueError("image_size_limit")
    with Image.open(path) as image:
        if (
            image.format not in {"PNG", "JPEG"}
            or image.width * image.height > 16_000_000
        ):
            raise ValueError("unsupported_image")
        image.verify()
    return path


def pages(
    content: Content,
    image_root: Path,
    *,
    tables: bool = True,
    geometry: Geometry = SLIDE,
) -> tuple[tuple[Placed, ...], ...]:
    """Lay the content out on pages; a workbook has none for its tables."""
    g = geometry
    registered = font()
    for text in content.texts():
        if any(ord(c) not in registered.face.charToGlyph for c in text):
            raise ValueError("unsupported_font_character")
    result: list[tuple[Placed, ...]] = []
    current: list[Placed] = []
    top = g.margin
    for index, section in enumerate(content.sections):
        heading = text_block(shown(section.heading), 19, body=g.body)
        if heading.height > 95:
            raise ValueError("heading_exceeds_layout")
        if index == 0:
            current, top = [], g.margin
            title = text_block(shown(content.title), 23, body=g.body)
            if title.height > 100:
                raise ValueError("title_exceeds_layout")
            current.append(Placed(title, top))
            top += title.height
        elif not g.flow or g.height - g.margin - top < heading.height + 2 * 16 + 8:
            # A slide per section; on a report page, a heading never ends a page.
            result.append(tuple(current))
            current, top = [], g.margin
        else:
            top += 10
        if index or not content.repeats_title():
            # Under the title, a heading that repeats it is not written again.
            current.append(Placed(heading, top))
            top += heading.height
        body: list[Block] = []
        # Inline markers are read, never laid out: a page and a slide show
        # the text in one face.
        for text in section.paragraphs:
            body.append(text_block(shown(text), body=g.body))
        for text in section.bullets:
            body.append(text_block(shown(text), bullet=True, body=g.body))
        for table in section.tables if tables else ():
            size, leading, pad, row_pad = table_type(len(table.headers))
            for row in table.read():
                lines = tuple(
                    wrap(cell.text, g.body / len(row) - 2 * pad, size) for cell in row
                )
                body.append(
                    Block(
                        lines=lines,
                        size=size,
                        table=True,
                        leading=leading,
                        pad=pad,
                        width=g.body,
                        # A row of empty cells keeps the height of one line.
                        height=max(1, *map(len, lines)) * leading + row_pad,
                    )
                )
        for value in section.images:
            path = image_path(image_root, value)
            with Image.open(path) as image:
                scale = min(g.body / image.width, g.image_height / image.height)
                body.append(
                    Block(
                        image=path,
                        width=image.width * scale,
                        height=image.height * scale + 8,
                    )
                )
        # A continued slide repeats its heading; a continued report page does not.
        repeat = 0.0 if g.flow else heading.height
        # Split text across pages. Table rows/images remain atomic and fail if oversized.
        for block in body:
            pending = block
            while True:
                room = g.height - g.margin - top
                if pending.height <= room:
                    current.append(Placed(pending, top))
                    top += pending.height
                    break
                if not pending.table and pending.image is None:
                    count = int((room - 8) / (pending.size * 1.35))
                    if count > 0:
                        prefix = pending.lines[0][:count]
                        current.append(
                            Placed(
                                Block(
                                    lines=(prefix,),
                                    size=pending.size,
                                    height=count * pending.size * 1.35 + 8,
                                    bullet=pending.bullet,
                                    width=pending.width,
                                ),
                                top,
                            )
                        )
                        rest = pending.lines[0][count:]
                        pending = Block(
                            lines=(rest,),
                            size=pending.size,
                            height=len(rest) * pending.size * 1.35 + 8,
                            width=pending.width,
                        )
                elif pending.height > g.height - 2 * g.margin - repeat:
                    raise ValueError("table_row_or_image_exceeds_page")
                result.append(tuple(current))
                if g.flow:
                    current, top = [], g.margin
                else:
                    current = [Placed(heading, g.margin)]
                    top = g.margin + heading.height
    result.append(tuple(current))
    return tuple(result)
