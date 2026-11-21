"""A figure keeps the unit and currency of its source, in every deliverable.

A deck asked with "8k MRR, raising 600k" showed "8 000 €" and "600 000 €": the
writer added a currency the person never gave (owner review 2026-11-21). Code
never formats, converts or adds a unit; this check finds a figure of the file
whose value is in the request or a document read, but with another unit or
currency than there. Only those figures are judged: a value the sources do not
hold is not a unit question.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

# Currencies by their written forms, in the languages Atlas answers in.
CURRENCIES = {
    "EUR": ("€", "eur", "euro", "euros"),
    "USD": ("$", "us$", "usd", "dollar", "dollars", "dólares", "dollari"),
    "GBP": ("£", "gbp", "pound", "pounds", "livre", "livres", "sterling"),
    "CHF": ("chf", "franc suisse", "francs suisses", "franken"),
    "JPY": ("¥", "jpy", "yen"),
}
# Units of measure, by symbol (case matters for some: m, M).
UNITS = (
    "km/h",
    "km",
    "m²",
    "m2",
    "m³",
    "cm",
    "mm",
    "m",
    "mi",
    "miles",
    "mile",
    "kg",
    "g",
    "t",
    "lb",
    "lbs",
    "l",
    "L",
    "ml",
    "kWh",
    "MWh",
    "kW",
    "MW",
    "W",
    "h",
    "min",
    "s",
    "°C",
    "°F",
    "%",
    "GB",
    "Go",
    "MB",
    "Mo",
    "TB",
    "To",
)
# Magnitude words and suffixes: "8k", "1,2 M€", "3 millions".
MAGNITUDES = {
    "k": 1e3,
    "K": 1e3,
    "m": 1e6,
    "M": 1e6,
    "Mio": 1e6,
    "Md": 1e9,
    "Mrd": 1e9,
    "bn": 1e9,
    "B": 1e9,
    "million": 1e6,
    "millions": 1e6,
    "Million": 1e6,
    "Millionen": 1e6,
    "milliard": 1e9,
    "milliards": 1e9,
    "Milliarden": 1e9,
    "billion": 1e9,
    "billions": 1e9,
    "mille": 1e3,
}

_CURRENCY_FORMS = sorted(
    ((form, code) for code, forms in CURRENCIES.items() for form in forms),
    key=lambda pair: -len(pair[0]),
)
_NUMBER = r"\d{1,3}(?:[   .,’']\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_MAGNITUDE = "|".join(sorted(map(re.escape, MAGNITUDES), key=len, reverse=True))
_CURRENCY = "|".join(re.escape(form) for form, _ in _CURRENCY_FORMS)
_UNIT = "|".join(
    [_CURRENCY] + [re.escape(unit) for unit in sorted(UNITS, key=len, reverse=True)]
)
# A currency may come first ("$8k", "EUR 600"); a unit of measure never does.
FIGURE = re.compile(
    rf"(?:(?<![\w])(?P<before>{_CURRENCY})\s?)?(?<![\w.,])(?P<number>{_NUMBER})"
    rf"(?:\s?(?P<magnitude>{_MAGNITUDE})(?![\w]))?"
    rf"(?:\s?(?:d['’]|de\s|of\s)?(?P<after>{_UNIT})(?![\w]))?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Figure:
    text: str
    values: frozenset[float]
    unit: str  # a currency code, a unit symbol, or "" for a bare number


def _unit(form: str | None) -> str:
    if not form:
        return ""
    lowered = form.lower()
    for written, code in _CURRENCY_FORMS:
        if lowered == written:
            return code
    for unit in UNITS:
        if form == unit or (lowered == unit.lower() and unit not in ("m", "M", "L")):
            return {
                "m2": "m²",
                "m3": "m³",
                "Go": "GB",
                "Mo": "MB",
                "To": "TB",
                "miles": "mi",
                "mile": "mi",
                "lbs": "lb",
                "L": "l",
            }.get(unit, unit)
    return ""


def _values(number: str, magnitude: str | None) -> frozenset[float]:
    """Every value the digits may mean: "8.000" is 8000 in German, 8 in English."""
    digits = re.sub(r"[   ’']", "", number)
    readings: set[float] = set()
    for thousands, decimal in ((",", "."), (".", ","), ("", ".")):
        candidate = digits
        if thousands:
            candidate = candidate.replace(thousands, "")
        candidate = candidate.replace(decimal, ".")
        try:
            readings.add(float(candidate))
        except ValueError:
            continue
    scale = MAGNITUDES.get(magnitude or "", 1.0)
    if magnitude and magnitude not in MAGNITUDES:
        scale = MAGNITUDES.get(
            magnitude.lower(), MAGNITUDES.get(magnitude.upper(), 1.0)
        )
    return frozenset(round(value * scale, 6) for value in readings)


def figures(text: str) -> list[Figure]:
    """The figures of a text with their unit or currency (before or after)."""
    text = unicodedata.normalize("NFC", text)
    found = []
    for match in FIGURE.finditer(text):
        if not match.group("number"):
            continue
        before, after = _unit(match.group("before")), _unit(match.group("after"))
        magnitude = match.group("magnitude")
        # "5 m" is five metres; "5 M" and "5 M€" are millions.
        if magnitude == "m" and not after:
            after, magnitude = _unit(magnitude), None
        found.append(
            Figure(
                match.group(0).strip(),
                _values(match.group("number"), magnitude),
                before or after,
            )
        )
    return found


def mismatches(file_texts: list[str], source_texts: list[str]) -> list[str]:
    """Figures of the file whose value the sources hold with another unit.

    Each entry quotes the file's figure and the source's: "8 000 €" against
    "8k". A currency added to a bare source value, a currency changed, and a
    unit changed for the same value are all mismatches.
    """
    sources = [figure for text in source_texts for figure in figures(text)]
    result: list[str] = []
    for text in file_texts:
        for figure in figures(text):
            if not figure.unit or figure.unit in ("%",):
                continue
            same = [s for s in sources if s.values & figure.values]
            if not same or any(s.unit == figure.unit for s in same):
                continue
            entry = f"“{figure.text}” (source: “{same[0].text}”)"
            if entry not in result:
                result.append(entry)
    return result
