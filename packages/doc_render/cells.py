"""How a text is read: inline emphasis, and numbers typed as text in a cell.

A cell is written with Markdown and typing habits: "**TOTAL**", "12.50",
"1 250,00 €", "45 %". The markers are read, never shown, and a text that is
clearly a number is a number in a workbook, shown as it was typed (decimals,
thousands separators, currency sign, percent). French and English notations
are both read. A text that may be a number or something else stays text: a
date, a phone number, an identifier with leading zeros, "1/2", a version
number, and "1,250" when nothing in its table says which notation is used.

A title, a heading, a paragraph and a bullet are read with the same rules
(prose): "**Date**: 12 March" never shows its asterisks. There a link keeps
its address after its text, since no cell holds it.

In a workbook a cell that starts with "=" is a formula. A cell typed with an
apostrophe before it ("'=A1") is the text "=A1" in every format.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
import re

# Columns a workbook table may have, and what a page or a slide still shows
# legibly (a label, twelve months and a total).
SHEET_COLUMNS = 60
PAGE_COLUMNS = 14


@dataclass(frozen=True)
class Span:
    text: str
    bold: bool = False
    italic: bool = False


@dataclass(frozen=True)
class Reading:
    """A cell as displayed, and the number a workbook stores for it."""

    spans: tuple[Span, ...] = ()
    number: int | float | None = None
    number_format: str = "General"
    link: str = ""
    # Typed with an apostrophe before "=": a text, never a formula.
    quoted: bool = False

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)

    @property
    def bold(self) -> bool:
        return bool(self.spans) and all(span.bold for span in self.spans)

    @property
    def italic(self) -> bool:
        return bool(self.spans) and all(span.italic for span in self.spans)

    @property
    def stored(self) -> str:
        """The text a reopened workbook gives for this cell."""
        return self.text if self.number is None else str(self.number)


# Emphasis opens and closes at a word boundary: "2*3*4", "5 * 3", "*args" and
# "a_b_c" are not emphasis. Underscores are never emphasis: identifiers such as
# "__init__" keep theirs. A link keeps its text.
URL = r"(?:https?://|mailto:)[^\s()]*(?:\([^\s()]*\)[^\s()]*)*"
INLINE = re.compile(
    r"\\(?P<escaped>[\\`*\[\]])"
    r"|`(?P<code>[^`]*[^`\s][^`]*)`"
    r"|(?<![\w\]])!?\[(?P<label>[^\[\]]*[^\[\]\s][^\[\]]*)\]\((?P<url>" + URL + r")\)"
    r"|(?<![\w*])(?P<stars>\*{1,3})(?![\s*])(?P<inner>.{1,400}?)(?<![\s*])"
    r"(?P=stars)(?![\w*])"
)


@lru_cache(maxsize=4096)
def spans(text: str, addresses: bool = False) -> tuple[Span, ...]:
    """The text without its inline Markdown markers, with its emphasis.

    With addresses, a link keeps its address after its text.
    """
    if text.startswith("="):
        # A formula: "*" is a product.
        return (Span(text),)
    found: list[Span] = []

    def add(part: str, bold: bool, italic: bool) -> None:
        if not part:
            return
        if found and (found[-1].bold, found[-1].italic) == (bold, italic):
            found[-1] = Span(found[-1].text + part, bold, italic)
        else:
            found.append(Span(part, bold, italic))

    def walk(part: str, bold: bool, italic: bool) -> None:
        position = 0
        for match in INLINE.finditer(part):
            add(part[position : match.start()], bold, italic)
            position = match.end()
            if match["escaped"]:
                add(match["escaped"], bold, italic)
            elif match["code"]:
                add(match["code"].strip(), bold, italic)
            elif match["label"]:
                walk(match["label"], bold, italic)
                address = match["url"].removeprefix("mailto:")
                if addresses and address not in match["label"]:
                    add(f" ({address})", bold, italic)
            else:
                marks = len(match["stars"])
                walk(match["inner"], bold or marks >= 2, italic or marks != 2)
        add(part[position:], bold, italic)

    walk(text, False, False)
    if not "".join(span.text for span in found).strip():
        # Markers around nothing are text.
        return (Span(text),) if text else ()
    return tuple(found)


def plain(text: str) -> str:
    return "".join(span.text for span in spans(text))


def prose(text: str) -> tuple[Span, ...]:
    """A title, a heading, a paragraph or a bullet, read as a cell is.

    "[the report](https://example.org/r)" reads "the report
    (https://example.org/r)": outside a workbook cell nothing else would keep
    the address.
    """
    return spans(text, True)


def shown(text: str) -> str:
    """A title, a heading, a paragraph or a bullet as displayed."""
    return "".join(span.text for span in prose(text))


def whole_link(text: str) -> str:
    """The address when the cell is one link and nothing else."""
    match = INLINE.fullmatch(text)
    return match["url"] if match and match["url"] else ""


GAP = "[    ]"
MONEY = r"[€$£]|EUR|USD|GBP|CHF"
SHAPE = re.compile(
    r"(?P<minus>[-−])?"
    rf"(?:(?P<before>{MONEY})(?P<before_gap>{GAP}?))?"
    r"(?P<inner_minus>[-−])?"
    rf"(?P<body>\d(?:(?:\d|[.,]|{GAP})*\d)?)"
    rf"(?:(?P<after_gap>{GAP}?)(?P<after>{MONEY}|%))?"
)
SPACED = re.compile(rf"([1-9]\d{{0,2}}(?:{GAP}\d{{3}})+)(?:([.,])(\d+))?")
COMMA_GROUPS = re.compile(r"([1-9]\d{0,2}(?:,\d{3})+)(?:\.(\d+))?")
SIMPLE = re.compile(r"(\d+)(?:([.,])(\d+))?")
# One group of exactly three digits after one separator reads both ways:
# "1,250" is 1250 in English and 1.25 in French.
TWO_WAYS = re.compile(r"[1-9]\d{0,2}([.,])\d{3}")
DOT_GROUPS = re.compile(r"\d{1,3}(?:\.\d{3})+,\d+|\d{1,3}(?:\.\d{3}){2,}")
# Text that begins like a number. A formula never skips such a cell silently.
NUMBER_START = re.compile(rf"[(+\-−]?\s*(?:(?:{MONEY})\s*)?[+\-−]?\d")


def notation(texts: tuple[str, ...]) -> str:
    """The notation the numbers of a table agree on: "english", "french" or ""."""
    english = french = False
    for text in texts:
        match = SHAPE.fullmatch(text.strip())
        if not match:
            continue
        body = match["body"]
        if DOT_GROUPS.fullmatch(body):
            return ""
        spaced = SPACED.fullmatch(body)
        if spaced:
            english = english or spaced[2] == "."
            french = french or spaced[2] == ","
        elif COMMA_GROUPS.fullmatch(body):
            english = english or "." in body or body.count(",") > 1
        elif SIMPLE.fullmatch(body) and not TWO_WAYS.fullmatch(body):
            english = english or "." in body
            french = french or "," in body
        if match["before"] in ("$", "£") and "," in body:
            english = True
    if english == french:
        return ""
    return "english" if english else "french"


def number(text: str, known: str = "") -> tuple[int | float, str] | None:
    """The number a text clearly writes and the format that shows it as typed."""
    match = SHAPE.fullmatch(text.strip())
    if not match or (match["minus"] and match["inner_minus"]):
        return None
    if match["before"] and match["after"]:
        return None
    body = match["body"]
    grouped = False
    if found := SPACED.fullmatch(body):
        whole, decimals, grouped = re.sub(GAP, "", found[1]), found[3] or "", True
    elif TWO_WAYS.fullmatch(body):
        if not known or (known == "french" and "." in body):
            return None
        whole, decimals = body.split("," if "," in body else ".")
        if known == "english" and "," in body:
            whole, decimals, grouped = whole + decimals, "", True
    elif found := COMMA_GROUPS.fullmatch(body):
        whole, decimals, grouped = found[1].replace(",", ""), found[2] or "", True
    elif found := SIMPLE.fullmatch(body):
        whole, decimals = found[1], found[3] or ""
    else:
        return None
    if (len(whole) > 1 and whole[0] == "0") or len((whole + decimals).lstrip("0")) > 15:
        # An identifier with leading zeros; more digits than a workbook keeps.
        return None
    value = Decimal(whole + "." + (decimals or "0"))
    if match["minus"] or match["inner_minus"]:
        value = -value
    shown = ("#,##0" if grouped else "0") + (
        "." + "0" * len(decimals) if decimals else ""
    )
    before, after = match["before"], match["after"]
    if after == "%":
        value /= 100
        shown += (" " if match["after_gap"] else "") + "%"
    elif before:
        shown = f'"{before}"' + (" " if match["before_gap"] else "") + shown
    elif after:
        shown += (" " if match["after_gap"] else "") + f'"{after}"'
    elif not grouped and not decimals:
        shown = "General"
    if value == value.to_integral_value():
        return int(value), shown
    return float(value), shown


def numeric_looking(text: str) -> bool:
    return NUMBER_START.match(text.strip()) is not None


def read(text: str, known: str = "", *, data: bool = True) -> Reading:
    """One cell. Headers and formulas are never numbers."""
    quoted = text.startswith("'=")
    # After the apostrophe the text is kept as typed: "*" is not emphasis.
    parts = (Span(text[1:]),) if quoted else spans(text)
    display = "".join(span.text for span in parts)
    found = number(display, known) if data and not display.startswith("=") else None
    return Reading(
        spans=parts,
        number=found[0] if found else None,
        number_format=found[1] if found else "General",
        link=whole_link(text),
        quoted=quoted,
    )


@lru_cache(maxsize=64)
def read_table(
    headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]
) -> tuple[tuple[Reading, ...], ...]:
    """The header row, then the rows; the rows share one number notation."""
    known = notation(tuple(plain(cell) for row in rows for cell in row))
    return (
        tuple(read(cell, data=False) for cell in headers),
        *(tuple(read(cell, known) for cell in row) for row in rows),
    )
