"""A themed PPTX deck: code chooses theme and layout, the content is the model's.

Each section is a slide. Its layout follows from what it holds, never from a
name the model gives: the first slide is a cover, a short last slide a
closing slide, a lone short line a statement, key numbers become callouts, a
chart a native bar chart, two to four developed points cards, and a longer
list a list with accent bullets. Every content slide has the same frame: the
title with an accent rule, the deck title and the slide number in the footer.
The palette is picked from the deck title, so a deck keeps its colours when it
is rebuilt and two decks do not all look alike. Nothing on a slide is text the
content did not hold: the footer repeats the deck title, the slide numbers are
numbers.

Text is fitted with the metrics of the embedded Vera face, wider than the
Arial the file names (Liberation Sans in LibreOffice): what fits here fits in
PowerPoint and LibreOffice, which wrap the text themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any

from lxml import etree
from PIL import Image
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Pt

from .cells import Span, prose, shown
from .layout import font, image_path, width_of
from .schema import Content, Section, Table

W, H = 960.0, 540.0
SIDE = 56.0
TITLE_TOP = 36.0
BODY_TOP = 122.0
FOOTER_TOP = H - 34.0
BODY_BOTTOM = FOOTER_TOP - 14.0
FACE = "Arial"
# Shape names give each text its role (title, list, point, footer...):
# readback.py reads a themed deck by them, not by its geometry.
ROLE = "atlas:"
BOLD = 1.08  # bold Vera is about this much wider than regular


@dataclass(frozen=True)
class Theme:
    name: str
    primary: str  # dark: cover background, titles
    accent: str  # rules, bullets, numbers, bars
    tint: str  # light background of panels and statements
    card: str = "F5F6F8"
    text: str = "1F2328"
    muted: str = "5B6470"
    on_primary: str = "FFFFFF"
    on_primary_muted: str = "C9D1DC"


THEMES = (
    Theme("midnight", "1E2A3A", "2F80ED", "EAF2FD"),
    Theme("teal", "0F3D3E", "12A89D", "E4F5F3"),
    Theme("plum", "2E1F3D", "8E5BD9", "F1EAFB"),
    Theme("ember", "2B2523", "D9692A", "FCEEE3"),
    Theme("forest", "1F3326", "3E9E4C", "E8F4E9"),
)


def theme_for(title: str) -> Theme:
    digest = hashlib.sha256(" ".join(shown(title).split()).encode()).digest()
    return THEMES[digest[0] % len(THEMES)]


def rgb(value: str) -> RGBColor:
    return RGBColor(  # type: ignore[no-untyped-call]
        int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    )


# --- measuring -------------------------------------------------------------


def wrapped(text: str, width: float, size: float, bold: bool = False) -> int:
    """How many lines a text takes in a box of this width (words kept whole)."""
    scale = BOLD if bold else 1.0
    lines, current = 0, ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if width_of(candidate, size) * scale <= width or not current:
            current = candidate
            while width_of(current, size) * scale > width and len(current) > 1:
                # A word longer than the line breaks where the box ends.
                cut = max(
                    1, int(len(current) * width / (width_of(current, size) * scale))
                )
                lines += 1
                current = current[cut:]
            continue
        lines += 1
        current = word
    return lines + (1 if current else 0) or 1


def fit(
    texts: list[tuple[str, bool]],
    width: float,
    height: float,
    largest: float,
    smallest: float,
    *,
    spacing: float = 1.18,
    gap: float = 0.5,
) -> float:
    """The largest size at which the paragraphs fit the box (or the smallest)."""
    size = largest
    while size > smallest:
        total = sum(
            wrapped(text, width, size, bold) * size * spacing + size * gap
            for text, bold in texts
        )
        if total <= height:
            return size
        size -= 1
    return smallest


# --- shapes ----------------------------------------------------------------


def box(
    slide: Any,
    x: float,
    y: float,
    w: float,
    h: float,
    fill: str,
    *,
    shape: Any = None,
    role: str = "",
) -> Any:
    item = slide.shapes.add_shape(
        shape or MSO_SHAPE.RECTANGLE, Pt(x), Pt(y), Pt(w), Pt(h)
    )
    item.fill.solid()
    item.fill.fore_color.rgb = rgb(fill)
    item.line.fill.background()
    item.shadow.inherit = False
    if role:
        item.name = ROLE + role
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        item.adjustments[0] = 0.06
    return item


def frame(
    slide: Any,
    x: float,
    y: float,
    w: float,
    h: float,
    anchor: Any = MSO_ANCHOR.TOP,
    *,
    role: str = "",
) -> Any:
    item = slide.shapes.add_textbox(Pt(x), Pt(y), Pt(w), Pt(h))
    if role:
        # What the text is, for reading the deck back (readback.py).
        item.name = ROLE + role
    text = item.text_frame
    text.word_wrap = True
    text.auto_size = MSO_AUTO_SIZE.NONE
    text.margin_left = text.margin_right = Pt(0)
    text.margin_top = text.margin_bottom = Pt(0)
    text.vertical_anchor = anchor
    return text


def runs(
    paragraph: Any, spans: tuple[Span, ...], size: float, color: str, bold: bool
) -> None:
    for span in spans:
        run = paragraph.add_run()
        run.text = span.text
        run.font.name = FACE
        run.font.size = Pt(size)
        run.font.bold = bold or span.bold or None
        run.font.italic = span.italic or None
        run.font.color.rgb = rgb(color)


def labelled(text: str) -> tuple[str, str] | None:
    """A "Label: text" line ("Budget : 12 400 €"); not a time, not an address."""
    found = re.fullmatch(r"([^:]{2,48}?)\s?:\s+(\S.*)", shown(text))
    if found is None or any(span.bold for span in prose(text)):
        return None
    return found[1].strip(), found[2].strip()


def write(
    text_frame: Any,
    texts: list[str],
    size: float,
    color: str,
    *,
    bold: bool = False,
    bullet: str | None = None,
    align: Any = PP_ALIGN.LEFT,
    spacing: float = 1.1,
    gap: float = 0.5,
    label_color: str | None = None,
) -> None:
    """Paragraphs into a text frame; a bullet colour gives accent bullets."""
    for index, text in enumerate(texts):
        paragraph = (
            text_frame.paragraphs[0] if index == 0 else text_frame.add_paragraph()
        )
        paragraph.alignment = align
        paragraph.line_spacing = spacing
        paragraph.space_after = Pt(size * gap)
        pair = labelled(text) if label_color else None
        if pair is not None and label_color:
            separator = " : " if " :" in shown(text) else ": "
            runs(paragraph, (Span(pair[0] + separator),), size, label_color, True)
            runs(paragraph, (Span(pair[1]),), size, color, bold)
        else:
            runs(paragraph, prose(text), size, color, bold)
        if bullet:
            indent = size * 1.1
            properties = paragraph._p.get_or_add_pPr()
            properties.set("marL", str(Pt(indent)))
            properties.set("indent", str(-Pt(indent)))
            colour = etree.SubElement(properties, qn("a:buClr"))
            etree.SubElement(colour, qn("a:srgbClr")).set("val", bullet)
            etree.SubElement(properties, qn("a:buFont")).set("typeface", FACE)
            etree.SubElement(properties, qn("a:buChar")).set("char", "•")


# --- slide parts -----------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float


def chrome(slide: Any, theme: Theme, heading: str, deck: str, number: int) -> None:
    """The frame of a content slide: title, accent rule, footer."""
    size = fit([(shown(heading), True)], W - 2 * SIDE, 74, 36, 22, gap=0)
    text = frame(
        slide, SIDE, TITLE_TOP, W - 2 * SIDE, 74, MSO_ANCHOR.BOTTOM, role="title"
    )
    write(text, [heading], size, theme.primary, bold=True, gap=0, spacing=1.0)
    rule = TITLE_TOP + 74 + 8
    box(slide, SIDE, rule, 56, 4, theme.accent)
    footer = frame(
        slide,
        SIDE,
        FOOTER_TOP,
        W - 2 * SIDE - 60,
        16,
        MSO_ANCHOR.MIDDLE,
        role="footer",
    )
    write(footer, [deck], 10, theme.muted, gap=0)
    count = frame(
        slide, W - SIDE - 50, FOOTER_TOP, 50, 16, MSO_ANCHOR.MIDDLE, role="number"
    )
    write(count, [str(number)], 10, theme.muted, align=PP_ALIGN.RIGHT, gap=0)


def text_items(section: Section) -> list[tuple[str, bool]]:
    """(text, bulleted) in order: paragraphs, then bullets."""
    return [(text, False) for text in section.paragraphs] + [
        (text, True) for text in section.bullets
    ]


def body_list(
    slide: Any,
    theme: Theme,
    items: list[tuple[str, bool]],
    area: Rect,
    largest: float = 28,
) -> None:
    measured = [(shown(text), False) for text, _ in items]
    indent_width = area.w - largest * 1.1
    size = fit(measured, indent_width, area.h, largest, 11, gap=0.55)
    text = frame(slide, area.x, area.y, area.w, area.h, role="list")
    for index, (value, bulleted) in enumerate(items):
        if index:
            text.add_paragraph()
        holder = _Single(text, index)
        write(
            holder,
            [value],
            size,
            theme.text,
            bullet=theme.accent if bulleted else None,
            gap=0.55,
            label_color=theme.primary,
        )


class _Single:
    """One paragraph of a text frame seen as a frame of one paragraph."""

    def __init__(self, text_frame: Any, index: int) -> None:
        self.paragraphs = [text_frame.paragraphs[index]]

    def add_paragraph(self) -> Any:  # pragma: no cover - one text per call
        raise AssertionError("single paragraph")


def needed(
    texts: list[tuple[str, bool]], width: float, size: float, gap: float
) -> float:
    return sum(
        wrapped(text, width, size, bold) * size * 1.18 + size * gap
        for text, bold in texts
    )


def cards(slide: Any, theme: Theme, texts: list[str], area: Rect) -> None:
    """Two to four developed points as cards, a numbered accent on each.

    The cards are as tall as the fullest one needs (at least half the body)
    and the group is centred in the body: no card is a tall empty box.
    """
    count = len(texts)
    columns = 2 if count == 4 else count
    rows = 2 if count == 4 else 1
    gap = 20.0
    width = (area.w - gap * (columns - 1)) / columns
    room = (area.h - gap * (rows - 1)) / rows
    pairs = [labelled(text) for text in texts]
    inner = width - 2 * 22
    # One size for every card, the size the fullest card allows.
    size = min(
        fit(measured_for(text, pair), inner, room - 76, 24, 12, gap=0.35)
        for text, pair in zip(texts, pairs, strict=True)
    )
    fullest = max(
        needed(measured_for(text, pair), inner, size, 0.35)
        for text, pair in zip(texts, pairs, strict=True)
    )
    height = min(room, max(room * 0.55, fullest + 90))
    top = area.y + (area.h - (height * rows + gap * (rows - 1))) / 2
    for index, (text, pair) in enumerate(zip(texts, pairs, strict=True)):
        column, row = index % columns, index // columns
        x = area.x + column * (width + gap)
        y = top + row * (height + gap)
        box(slide, x, y, width, height, theme.card, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
        box(slide, x, y, 6, height, theme.accent)
        badge = frame(slide, x + 22, y + 20, 80, 34, role="decor")
        write(badge, [f"{index + 1:02d}"], 24, theme.accent, bold=True, gap=0)
        content = frame(slide, x + 22, y + 66, inner, height - 76, role="point")
        if pair:
            # The label keeps its colon: the card says what the line said.
            colon = " :" if " :" in shown(text) else ":"
            write(content, [pair[0] + colon], size, theme.primary, bold=True, gap=0.35)
            more = content.add_paragraph()
            more.line_spacing = 1.1
            runs(more, (Span(pair[1]),), size, theme.text, False)
        else:
            write(content, [text], size, theme.text, gap=0.35)


def pillars(slide: Any, theme: Theme, texts: list[str], area: Rect) -> None:
    """Two to four short points side by side, each under a numbered disc."""
    count = len(texts)
    gap = 28.0
    width = (area.w - gap * (count - 1)) / count
    pairs = [labelled(text) for text in texts]
    disc = 76.0
    size = min(
        fit(measured_for(text, pair), width, area.h - disc - 40, 26, 14, gap=0.3)
        for text, pair in zip(texts, pairs, strict=True)
    )
    fullest = max(
        needed(measured_for(text, pair), width, size, 0.3)
        for text, pair in zip(texts, pairs, strict=True)
    )
    top = area.y + max(0.0, (area.h - (disc + 28 + fullest)) / 2)
    box(slide, area.x + width / 2, top + disc / 2 - 1.5, area.w - width, 3, theme.tint)
    for index, (text, pair) in enumerate(zip(texts, pairs, strict=True)):
        x = area.x + index * (width + gap)
        circle = box(
            slide,
            x + (width - disc) / 2,
            top,
            disc,
            disc,
            theme.accent,
            shape=MSO_SHAPE.OVAL,
            role="decor",
        )
        label = circle.text_frame
        label.margin_left = label.margin_right = Pt(0)
        label.vertical_anchor = MSO_ANCHOR.MIDDLE
        write(
            label,
            [str(index + 1)],
            30,
            theme.on_primary,
            bold=True,
            align=PP_ALIGN.CENTER,
            gap=0,
        )
        content = frame(
            slide, x, top + disc + 28, width, area.h - disc - 28, role="point"
        )
        if pair:
            colon = " :" if " :" in shown(text) else ":"
            write(
                content,
                [pair[0] + colon],
                size,
                theme.primary,
                bold=True,
                align=PP_ALIGN.CENTER,
                gap=0.3,
            )
            more = content.add_paragraph()
            more.alignment = PP_ALIGN.CENTER
            more.line_spacing = 1.1
            runs(more, (Span(pair[1]),), size, theme.text, False)
        else:
            write(content, [text], size, theme.text, align=PP_ALIGN.CENTER, gap=0.3)


def measured_for(text: str, pair: tuple[str, str] | None) -> list[tuple[str, bool]]:
    return [(pair[0], True), (pair[1], False)] if pair else [(shown(text), False)]


def metrics_row(slide: Any, theme: Theme, section: Section, area: Rect) -> None:
    count = len(section.metrics)
    gap = 18.0
    width = (area.w - gap * (count - 1)) / count
    values = [(m.value, True) for m in section.metrics]
    size = min(fit([v], width - 32, 62, 54, 20, gap=0, spacing=1.0) for v in values)
    labels = [(m.label, False) for m in section.metrics]
    label = min(fit([v], width - 32, area.h - 96, 18, 11, gap=0) for v in labels)
    for index, metric in enumerate(section.metrics):
        x = area.x + index * (width + gap)
        box(
            slide,
            x,
            area.y,
            width,
            area.h,
            theme.tint,
            shape=MSO_SHAPE.ROUNDED_RECTANGLE,
        )
        value = frame(
            slide,
            x + 16,
            area.y + 16,
            width - 32,
            62,
            MSO_ANCHOR.BOTTOM,
            role="metric-value",
        )
        write(value, [metric.value], size, theme.accent, bold=True, gap=0, spacing=1.0)
        text = frame(
            slide, x + 16, area.y + 86, width - 32, area.h - 96, role="metric-label"
        )
        write(text, [metric.label], label, theme.muted, gap=0)


def chart(slide: Any, theme: Theme, section: Section, area: Rect) -> None:
    data = section.chart
    assert data is not None
    top = area.y
    if data.title:
        title = frame(slide, area.x, area.y, area.w, 22, role="chart-title")
        write(title, [data.title], 14, theme.muted, bold=True, gap=0)
        top += 28
    values = CategoryChartData()  # type: ignore[no-untyped-call]
    values.categories = list(data.labels)
    values.add_series(  # type: ignore[no-untyped-call]
        data.title or section.heading, list(data.values)
    )
    long_labels = max(len(label) for label in data.labels) > 14
    kind = (
        XL_CHART_TYPE.BAR_CLUSTERED if long_labels else XL_CHART_TYPE.COLUMN_CLUSTERED
    )
    drawn = slide.shapes.add_chart(
        kind, Pt(area.x), Pt(top), Pt(area.w), Pt(area.y + area.h - top), values
    ).chart
    drawn.has_legend = False
    drawn.has_title = False
    drawn.font.name = FACE
    drawn.font.size = Pt(12)
    drawn.font.color.rgb = rgb(theme.muted)
    plot = drawn.plots[0]
    plot.gap_width = 70
    plot.vary_by_categories = False
    series = plot.series[0]
    series.format.fill.solid()
    series.format.fill.fore_color.rgb = rgb(theme.accent)
    series.invert_if_negative = False
    plot.has_data_labels = True
    labels = plot.data_labels
    whole = all(float(value).is_integer() for value in data.values)
    unit = re.sub(r'["\\]', "", data.unit).strip()
    labels.number_format = ("#,##0" if whole else "#,##0.0#") + (
        f' "{unit}"' if unit else ""
    )
    labels.number_format_is_linked = False
    labels.position = XL_LABEL_POSITION.OUTSIDE_END
    labels.font.size = Pt(12)
    labels.font.bold = True
    labels.font.color.rgb = rgb(theme.text)
    axis = drawn.value_axis
    axis.has_major_gridlines = False
    axis.visible = False
    categories = drawn.category_axis
    categories.format.line.color.rgb = rgb("D0D5DC")
    categories.tick_labels.font.size = Pt(12)
    categories.tick_labels.font.color.rgb = rgb(theme.muted)


def table_rows(table: Table, area: Rect) -> list[tuple[int, int, float]]:
    """Row ranges (start, end, font size) of a table: a long one continues."""
    read = table.read()
    cell = area.w / len(table.headers) - 12

    def heights(size: float) -> list[float]:
        return [
            max(wrapped(c.text or " ", cell, size, index == 0) for c in row)
            * size
            * 1.15
            + 12
            for index, row in enumerate(read)
        ]

    for size in (20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10):
        if sum(heights(size)) <= area.h:
            return [(1, len(read), size)]
    size = 10
    measured = heights(size)
    chunks: list[tuple[int, int, float]] = []
    start, used = 1, measured[0]
    for index in range(1, len(read)):
        if used + measured[index] > area.h and index > start:
            chunks.append((start, index, size))
            start, used = index, measured[0]
        used += measured[index]
    chunks.append((start, len(read), size))
    return chunks


def table(
    slide: Any, theme: Theme, data: Table, area: Rect, rows: tuple[int, int, float]
) -> None:
    read = data.read()
    start, end, size = rows
    shown_rows = [read[0], *read[start:end]]
    columns = len(data.headers)
    measured = [
        max(wrapped(c.text or " ", area.w / columns - 12, size, r == 0) for c in row)
        * size
        * 1.15
        + 12
        for r, row in enumerate(shown_rows)
    ]
    # Rows open up (at most by half) when the body has room: a short table
    # is not a thin strip at the top of an empty slide.
    scale = max(1.0, min(1.5, area.h / sum(measured)))
    heights = [value * scale for value in measured]
    grid = slide.shapes.add_table(
        len(shown_rows),
        columns,
        Pt(area.x),
        Pt(area.y),
        Pt(area.w),
        Pt(sum(heights)),
    ).table
    for index, value in enumerate(heights):
        grid.rows[index].height = Pt(value)
    grid.first_row = True
    grid.horz_banding = False
    for r, row in enumerate(shown_rows):
        for c, reading in enumerate(row):
            cell = grid.cell(r, c)
            cell.fill.solid()
            cell.fill.fore_color.rgb = rgb(
                theme.primary if r == 0 else (theme.card if r % 2 == 0 else "FFFFFF")
            )
            cell.margin_left = cell.margin_right = Pt(6)
            cell.margin_top = cell.margin_bottom = Pt(4)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            paragraph = cell.text_frame.paragraphs[0]
            cell.text_frame.word_wrap = True
            runs(
                paragraph,
                reading.spans or (Span(reading.text),),
                size,
                theme.on_primary if r == 0 else theme.text,
                r == 0,
            )
            if reading.number is not None and r:
                paragraph.alignment = PP_ALIGN.RIGHT


def picture(slide: Any, root: Path, value: str, area: Rect) -> None:
    path = image_path(root, value)
    with Image.open(path) as image:
        scale = min(area.w / image.width, area.h / image.height)
        width, height = image.width * scale, image.height * scale
    slide.shapes.add_picture(
        str(path),
        Pt(area.x + (area.w - width) / 2),
        Pt(area.y + (area.h - height) / 2),
        Pt(width),
        Pt(height),
    )


# --- slide kinds -----------------------------------------------------------


def short(texts: tuple[str, ...], most: int, length: int) -> bool:
    return len(texts) <= most and all(len(shown(text)) <= length for text in texts)


def bare(section: Section) -> bool:
    return not (section.tables or section.images or section.metrics or section.chart)


def kind(section: Section, index: int, count: int) -> str:
    texts = section.paragraphs + section.bullets
    if index == 0 and bare(section) and short(texts, 3, 160):
        return "cover"
    if index == count - 1 and count >= 3 and bare(section) and short(texts, 3, 120):
        return "closing"
    if bare(section) and len(texts) == 1 and len(shown(texts[0])) <= 180:
        return "statement"
    if (
        bare(section)
        and not section.paragraphs
        and 2 <= len(section.bullets) <= 4
        and sum(len(shown(text)) for text in section.bullets) <= 600
    ):
        developed = sum(len(shown(text)) for text in section.bullets) / len(
            section.bullets
        )
        if developed >= 45 or all(labelled(text) for text in section.bullets):
            return "cards"
        return "pillars"
    return "content"


def cover(
    slide: Any, theme: Theme, content: Content, section: Section, closing: bool
) -> None:
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = rgb(theme.primary)
    box(slide, 0, 0, 14, H, theme.accent)
    box(
        slide, W - 220, H - 220, 220, 220, theme.accent, shape=MSO_SHAPE.RIGHT_TRIANGLE
    ).rotation = 180
    lines = section.paragraphs + section.bullets
    title = shown(content.title)
    heading = shown(section.heading)
    show_deck = not closing and "".join(title.split()) != "".join(heading.split())
    top = 150.0 if not closing else 170.0
    width = W - 2 * 80 - 120
    if show_deck:
        size = fit([(title, True)], width, 120, 54, 28, gap=0, spacing=1.05)
        text = frame(
            slide, 80, top - 40, width, 120, MSO_ANCHOR.BOTTOM, role="deck-title"
        )
        write(
            text,
            [content.title],
            size,
            theme.on_primary,
            bold=True,
            gap=0,
            spacing=1.05,
        )
        top += 96
        size = fit([(heading, False)], width, 60, 28, 16, gap=0)
        text = frame(slide, 80, top, width, 60, role="title")
        write(text, [section.heading], size, theme.on_primary_muted, gap=0)
        top += 64
    else:
        size = fit(
            [(heading, True)],
            width,
            140,
            54 if not closing else 46,
            26,
            gap=0,
            spacing=1.05,
        )
        text = frame(slide, 80, top - 60, width, 140, MSO_ANCHOR.BOTTOM, role="title")
        write(
            text,
            [section.heading],
            size,
            theme.on_primary,
            bold=True,
            gap=0,
            spacing=1.05,
        )
        top += 96
    box(slide, 80, top, 72, 4, theme.accent)
    top += 22
    if lines:
        size = fit(
            [(shown(line), False) for line in lines],
            width,
            H - top - 60,
            24,
            12,
            gap=0.4,
        )
        text = frame(slide, 80, top, width, H - top - 60, role="lines")
        write(text, list(lines), size, theme.on_primary_muted, gap=0.4)
    if closing:
        footer = frame(
            slide, 80, FOOTER_TOP, W - 400, 16, MSO_ANCHOR.MIDDLE, role="footer"
        )
        write(footer, [content.title], 10, theme.on_primary_muted, gap=0)


def statement(
    slide: Any, theme: Theme, section: Section, deck: str, number: int
) -> None:
    chrome(slide, theme, section.heading, deck, number)
    area = Rect(SIDE, BODY_TOP + 20, W - 2 * SIDE, BODY_BOTTOM - BODY_TOP - 40)
    box(
        slide,
        area.x,
        area.y,
        area.w,
        area.h,
        theme.tint,
        shape=MSO_SHAPE.ROUNDED_RECTANGLE,
    )
    box(slide, area.x, area.y, 6, area.h, theme.accent)
    value = (section.paragraphs + section.bullets)[0]
    size = fit(
        [(shown(value), False)], area.w - 96, area.h - 60, 32, 16, gap=0, spacing=1.15
    )
    text = frame(
        slide,
        area.x + 48,
        area.y + 30,
        area.w - 96,
        area.h - 60,
        MSO_ANCHOR.MIDDLE,
        role="point",
    )
    write(text, [value], size, theme.primary, gap=0, spacing=1.15)


def content_slide(
    slide: Any,
    theme: Theme,
    root: Path,
    section: Section,
    deck: str,
    number: int,
    part: dict[str, Any],
) -> None:
    chrome(slide, theme, section.heading, deck, number)
    area = Rect(SIDE, BODY_TOP, W - 2 * SIDE, BODY_BOTTOM - BODY_TOP)
    items: list[tuple[str, bool]] = part["items"]
    if section.metrics and part["metrics"]:
        alone = not (items or part.get("visual"))
        height = 170.0 if alone else 150.0
        # Numbers alone sit in the middle of the body, not above an empty half.
        top = area.y + (area.h - height) / 2 if alone else area.y
        metrics_row(slide, theme, section, Rect(area.x, top, area.w, height))
        area = Rect(area.x, area.y + height + 24, area.w, area.h - height - 24)
    visual = part.get("visual")
    if visual is None and not items:
        return
    if visual is None:
        body_list(slide, theme, items, area)
        return
    if items and sum(len(shown(text)) for text, _ in items) <= 90:
        # A short lead goes above the visual, which keeps the full width.
        lead = 30.0 * len(items) + 8
        body_list(slide, theme, items, Rect(area.x, area.y, area.w, lead), largest=20)
        area = Rect(area.x, area.y + lead + 10, area.w, area.h - lead - 10)
    elif items:
        left = Rect(area.x, area.y, area.w * 0.4, area.h)
        body_list(slide, theme, items, left, largest=22)
        area = Rect(area.x + area.w * 0.4 + 28, area.y, area.w * 0.6 - 28, area.h)
    what, value = visual
    if what == "chart":
        chart(slide, theme, section, area)
    elif what == "image":
        box(
            slide,
            area.x,
            area.y,
            area.w,
            area.h,
            theme.tint,
            shape=MSO_SHAPE.ROUNDED_RECTANGLE,
        )
        picture(
            slide, root, value, Rect(area.x + 10, area.y + 10, area.w - 20, area.h - 20)
        )
    else:
        data, rows = value
        table(slide, theme, data, area, rows)


def needed_items(items: list[tuple[str, bool]], width: float, size: float) -> float:
    """The height a list takes at this size (as body_list lays it out)."""
    return sum(
        wrapped(shown(text), width - size * 1.1, size) * size * 1.18 + size * 0.55
        for text, _ in items
    )


def chunked(
    items: list[tuple[str, bool]], width: float, first: float, rest: float
) -> list[list[tuple[str, bool]]]:
    """Items split over slides so each slide reads at 16 pt or more.

    A long text continues on the next slide under the same title: nothing is
    shrunk out of sight or cut (a slide held 18 paragraphs at 11 pt).
    """
    chunks: list[list[tuple[str, bool]]] = [[]]
    for item in items:
        room = first if len(chunks) == 1 else rest
        if chunks[-1] and needed_items([*chunks[-1], item], width, 16) > room:
            chunks.append([item])
        else:
            chunks[-1].append(item)
    return chunks


def parts(section: Section) -> list[dict[str, Any]]:
    """The slides of a section: one, or more for long text, tables and pictures.

    Each part says what it shows: its items, whether the key numbers are on
    it, and its visual (chart, table rows or picture).
    """
    area = Rect(SIDE, BODY_TOP, W - 2 * SIDE, BODY_BOTTOM - BODY_TOP)
    items = text_items(section)
    numbers = 174.0 if section.metrics else 0.0
    lead = bool(items) and sum(len(shown(text)) for text, _ in items) <= 90
    has_visual = bool(section.chart or section.tables or section.images)
    beside = bool(items) and not lead and has_visual
    if beside and needed_items(items, area.w * 0.4, 12) > area.h - numbers:
        # Too much text to stand beside a visual: the text, then the visual.
        beside = False
        alone = True
    else:
        alone = not has_visual
    result: list[dict[str, Any]] = []
    if alone and items:
        for index, chunk in enumerate(chunked(items, area.w, area.h - numbers, area.h)):
            result.append(dict(items=chunk, metrics=index == 0))
    visual_area = Rect(
        0,
        0,
        area.w * 0.6 - 28 if beside else area.w,
        area.h
        - (numbers if not result else 0)
        - (30.0 * len(items) + 18 if lead and not result else 0),
    )
    visuals: list[tuple[str, object]] = []
    if section.chart is not None:
        visuals.append(("chart", None))
    for data in section.tables:
        for rows in table_rows(data, visual_area):
            visuals.append(("table", (data, rows)))
    visuals.extend(("image", value) for value in section.images)
    for visual in visuals:
        first = not result
        result.append(
            dict(
                items=items if first and not alone else [],
                metrics=first,
                visual=visual,
            )
        )
    return result or [dict(items=[], metrics=True)]


def build_deck(content: Content, path: Path, root: Path) -> None:
    font()
    theme = theme_for(content.title)
    deck = Presentation()
    deck.slide_width, deck.slide_height = Pt(W), Pt(H)
    deck.core_properties.title = shown(content.title)
    blank = deck.slide_layouts[6]
    number = 0
    count = len(content.sections)
    previous = ""
    for index, section in enumerate(content.sections):
        layout = kind(section, index, count)
        if layout == previous and layout in ("cards", "pillars"):
            # Two slides in a row never share a point layout.
            layout = "cards" if layout == "pillars" else "pillars"
        previous = layout
        pieces = (
            [dict(items=[], metrics=True)] if layout != "content" else parts(section)
        )
        for piece in pieces:
            number += 1
            slide = deck.slides.add_slide(blank)
            if layout in ("cover", "closing"):
                cover(slide, theme, content, section, layout == "closing")
            elif layout == "statement":
                statement(slide, theme, section, content.title, number)
            elif layout in ("cards", "pillars"):
                chrome(slide, theme, section.heading, content.title, number)
                (cards if layout == "cards" else pillars)(
                    slide,
                    theme,
                    list(section.bullets),
                    Rect(SIDE, BODY_TOP + 6, W - 2 * SIDE, BODY_BOTTOM - BODY_TOP - 6),
                )
            else:
                content_slide(slide, theme, root, section, content.title, number, piece)
            if section.notes and piece is pieces[0]:
                slide.notes_slide.notes_text_frame.text = "\n".join(
                    shown(note) for note in section.notes
                )
    deck.save(str(path))
