"""Measured layout shared by the PDF writer and slide builder."""

from dataclasses import dataclass
from pathlib import Path

import reportlab
from PIL import Image
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from .schema import Content

FONT = "AtlasContentVera"
WIDTH, HEIGHT, MARGIN = 720.0, 405.0, 30.0
BODY_WIDTH = WIDTH - 2 * MARGIN


@dataclass(frozen=True)
class Block:
    lines: tuple[tuple[str, ...], ...] = ()
    size: float = 12
    image: Path | None = None
    width: float = BODY_WIDTH
    height: float = 0
    table: bool = False
    bullet: bool = False


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


def text_block(text: str, size: float = 12, *, bullet: bool = False) -> Block:
    lines = wrap(text, BODY_WIDTH - (16 if bullet else 0), size)
    return Block(
        lines=(lines,), size=size, height=len(lines) * size * 1.35 + 8, bullet=bullet
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


def pages(content: Content, image_root: Path) -> tuple[tuple[Placed, ...], ...]:
    registered = font()
    for text in content.texts():
        if any(ord(c) not in registered.face.charToGlyph for c in text):
            raise ValueError("unsupported_font_character")
    result: list[tuple[Placed, ...]] = []
    for index, section in enumerate(content.sections):
        heading = text_block(section.heading, 19)
        if heading.height > 95:
            raise ValueError("heading_exceeds_layout")
        current: list[Placed] = []
        top = MARGIN
        if index == 0:
            title = text_block(content.title, 23)
            if title.height > 100:
                raise ValueError("title_exceeds_layout")
            current.append(Placed(title, top))
            top += title.height
        current.append(Placed(heading, top))
        top += heading.height
        body: list[Block] = []
        for text in section.paragraphs:
            body.append(text_block(text))
        for text in section.bullets:
            body.append(text_block(text, bullet=True))
        for table in section.tables:
            for row in (table.headers, *table.rows):
                lines = tuple(
                    wrap(cell, BODY_WIDTH / len(row) - 12, 11) for cell in row
                )
                body.append(
                    Block(
                        lines=lines,
                        size=11,
                        table=True,
                        height=max(map(len, lines)) * 15 + 12,
                    )
                )
        for value in section.images:
            path = image_path(image_root, value)
            with Image.open(path) as image:
                scale = min(BODY_WIDTH / image.width, 180 / image.height)
                body.append(
                    Block(
                        image=path,
                        width=image.width * scale,
                        height=image.height * scale + 8,
                    )
                )
        # Split text across pages. Table rows/images remain atomic and fail if oversized.
        for block in body:
            pending = block
            while True:
                room = HEIGHT - MARGIN - top
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
                                ),
                                top,
                            )
                        )
                        rest = pending.lines[0][count:]
                        pending = Block(
                            lines=(rest,),
                            size=pending.size,
                            height=len(rest) * pending.size * 1.35 + 8,
                        )
                elif pending.height > HEIGHT - 2 * MARGIN - heading.height:
                    raise ValueError("table_row_or_image_exceeds_page")
                result.append(tuple(current))
                current = [Placed(heading, MARGIN)]
                top = MARGIN + heading.height
        result.append(tuple(current))
    return tuple(result)
