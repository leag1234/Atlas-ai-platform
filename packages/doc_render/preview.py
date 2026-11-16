"""The first page of a laid-out document as a picture, for checking it.

The deliverable check (services/orchestrator/deliverable_check.py) shows the
page to a vision model next to the request. No PDF rasteriser is installed on
the servers; the page is drawn here from the same layout the PDF writer and
the slide builder use (layout.pages): the same lines, the same breaks, the same
emoji pictures, the same table cells. What the layout dropped is absent here
as it is in the file.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from . import emoji
from .layout import A4, SLIDE, Geometry, pages, report_geometry
from .glyphs import FONT_FILE
from .schema import Content

# Pixels per point: an A4 page is 595 x 842 points, read well at this size.
SCALE = 1.0


def geometry(content: Content, suffix: str) -> Geometry | None:
    """The page a format lays out; None for a workbook (no page)."""
    if suffix == ".pdf":
        return report_geometry(content)
    if suffix == ".pptx":
        return SLIDE
    if suffix == ".docx":
        return A4  # Word flows the text itself: an approximation of its page
    return None


def _line(
    draw: ImageDraw.ImageDraw,
    page: Image.Image,
    x: float,
    base: float,
    line: str,
    size: float,
) -> None:
    face = ImageFont.truetype(str(FONT_FILE), max(1, round(size * SCALE)))
    run = ""
    for token in [*emoji.tokens(line), ""]:
        drawn = emoji.picture(token) if token and emoji.is_emoji(token) else None
        if drawn is None and token:
            run += token
            continue
        if run:
            draw.text(
                (x * SCALE, base * SCALE), run, font=face, fill="black", anchor="ls"
            )
            x += face.getlength(run) / SCALE
            run = ""
        if drawn is not None:
            with Image.open(io.BytesIO(drawn.png)) as source:
                width = max(1, round(drawn.advance * size * SCALE))
                height = max(1, round(drawn.height * size * SCALE))
                picture = source.convert("RGBA").resize((width, height))
            top = base - drawn.ascent * size
            page.alpha_composite(picture, (round(x * SCALE), round(top * SCALE)))
            x += drawn.advance * size


def first_page(content: Content, suffix: str) -> bytes | None:
    """The first page as PNG bytes, or None for a format without pages."""
    g = geometry(content, suffix)
    if g is None:
        return None
    laid = pages(content, FONT_FILE.parent, geometry=g)
    page = Image.new("RGBA", (round(g.width * SCALE), round(g.height * SCALE)), "white")
    draw = ImageDraw.Draw(page)
    for placed in laid[0] if laid else ():
        block, top = placed.block, placed.top
        if block.image is not None:
            continue  # publication refuses images (presentation_delivery)
        if block.bullet:
            draw.ellipse(
                [
                    (g.margin + 3) * SCALE,
                    (top + 6) * SCALE,
                    (g.margin + 7) * SCALE,
                    (top + 10) * SCALE,
                ],
                fill="black",
            )
        cell_width = block.width / max(1, len(block.lines))
        for column, lines in enumerate(block.lines):
            x = g.margin + column * cell_width
            if block.table:
                draw.rectangle(
                    [
                        x * SCALE,
                        top * SCALE,
                        (x + cell_width) * SCALE,
                        (top + block.height) * SCALE,
                    ],
                    outline="black",
                )
            x += block.pad if block.table else (16 if block.bullet else 0)
            leading = block.leading if block.table else block.size * 1.35
            for index, line in enumerate(lines):
                _line(
                    draw,
                    page,
                    x,
                    top + block.size + 3 + index * leading,
                    line,
                    block.size,
                )
    output = io.BytesIO()
    page.convert("RGB").save(output, "PNG", optimize=True)
    return output.getvalue()
