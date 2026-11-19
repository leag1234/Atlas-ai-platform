"""Number reading shared by fact verification and the document checks."""

from __future__ import annotations

import re

def candidates(token: str) -> set[float]:
    """All plausible values of a written number (locale-ambiguous forms give two)."""
    token = token.strip()
    out: set[float] = set()
    spaced = re.sub(r"[   ]", "", token)
    if spaced != token:
        token = spaced
        if re.fullmatch(r"\d+(?:[.,]\d+)?", token):
            out.add(float(token.replace(",", ".")))
            return out
    if re.fullmatch(r"\d+", token):
        out.add(float(token))
    elif re.fullmatch(r"\d+[.,]\d+", token):
        whole, frac = re.split(r"[.,]", token)
        out.add(float(whole + "." + frac))
        if len(frac) == 3:
            out.add(float(whole + frac))
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", token):
        out.add(float(token.replace(",", "")))
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", token):
        out.add(float(token.replace(".", "").replace(",", ".")))
    return out


def numbers_in(text: str) -> list[tuple[str, set[float], int]]:
    """Every number token in the text with its candidate values and position."""
    found = []
    for match in re.finditer(
        r"(?<![\w.,\-/])(\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)*)", text
    ):
        token = match.group(1)
        values = candidates(token)
        if values:
            found.append((token, values, match.start()))
    return found


def same(a: float, b: float, rel: float = 0.0) -> bool:
    if a == b:
        return True
    if rel and b:
        return abs(a - b) / abs(b) <= rel
    return False


def text_contains_value(value: float, text: str) -> bool:
    return any(any(same(v, value) for v in values) for _, values, _ in numbers_in(text))


def rounds_to(shown: float, exact: float) -> bool:
    """A displayed number is a fair rounding of an exact value."""
    if exact == 0:
        return shown == 0
    if abs(shown - exact) / abs(exact) <= 0.02:
        return True
    # Rounded to the displayed number of significant digits.
    digits = len(re.sub(r"[^0-9]", "", f"{shown:g}").lstrip("0")) or 1
    from math import floor, log10

    magnitude = floor(log10(abs(exact)))
    rounded = round(exact, -int(magnitude) + digits - 1)
    return abs(rounded - shown) <= 1e-9 * max(1, abs(shown))
