"""Travel times, distances and timetable times come from a source or from code.

Owner test 2026-11-16 (staging): asked for morning trains to reach a Paris
station by 12:30, Atlas wrote that the person's street was "about 10 minutes'
walk" from the station, and listed connections with clock times no source gave.
Atlas has no routing engine: a walking, driving or transit duration, a distance
between two places, or a departure or arrival time written in a travel answer
is a fact, and like any fact it must come from a page read in this turn, from
the person's own words, or from code (the journey plan's time arithmetic, the
straight-line distance between the device position and a geocoded place).

On a travel turn (a position was used, the routing decision needs the position,
or the journey plan tool was offered) the answer is held, then a duration, a
distance or a clock time found in no source, in no computation and not in the
person's own words (figures or words: "midi trente", "by 2pm", "half past
twelve") is left out with the parenthesis or the whole sentence that states it;
a table cell becomes a dash and a row or table left without values goes; one
short note at the end, in the answer's language, lists what was left out
(staging -94: markers after every value broke sentences).

Units and duration forms are parsed by code for the five interface languages
(km, m, mi, min, h, hours, heures, Stunden, horas, ore...), never by a list of
sentences. Durations in an answer are read strictly (``1 h 20`` without a
minute unit may be a clock time, so it is not read as a duration); sources are
read generously (``2h05`` there counts as both a time and a duration), so a
sourced value is not mistaken for an invented one.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import urlsplit

from services.orchestrator import redirects, route_facts
from services.orchestrator.grounding import Grounding, normalized
from services.orchestrator.location import Position

# "price" is a fare or fee; "route" is a sentence, a parenthesis or a table
# cell stating an unsourced route fact (route_facts.py): both are left out
# like a value.
# "part": a part of a sentence removed as it is (route_facts._cuts).
Kind = Literal["duration", "distance", "clock", "price", "route", "part"]

_SPACE = r"[   ]?"
_NUMBER = r"(?<![\w.,:/])(\d{1,5}(?:[.,]\d{1,2})?)"
_HOURS = r"(?:h|hrs?|hours?|heures?|std|stunden?|horas?|ore|ora)"
_MINUTES = r"(?:minut(?:es?|en|os?|i)|mins?|mn)"
_KILOMETRES = r"(?:km|kilom[eè]t(?:re|er|ro|ri)s?|kilometr(?:es|os?|i))"
_METRES = r"(?:m[eè]t(?:re|er|ro|ri)s?|metros?|metri)"
_MILES = r"(?:mi|miles?|millas?|miglia)"
_JOIN = r"(?:[   ]?(?:and|et|und|y|e)[   ]?|[   ]?)"
_END = r"(?![\w²³/])"

# "1 h 20 min", "2 hours and 5 minutes", "1h20min".
_HOURS_MINUTES = re.compile(
    rf"{_NUMBER}{_SPACE}{_HOURS}{_JOIN}(\d{{1,2}}){_SPACE}{_MINUTES}\.?{_END}",
    re.I,
)
# "2h 45m", "1 h 5 m": a journey time, never the clock time "2h45" (staging
# -115: a train's "2h 45m" between two sourced times was read as 02:45 and
# left out of its table row).
_HOURS_M = re.compile(
    rf"{_NUMBER}{_SPACE}h{_SPACE}(\d{{1,2}}){_SPACE}m\.?{_END}",
    re.I,
)
# "12h30", "12 h 30", "12:30", "2:00 pm"; "9.45" is not read (a decimal), nor
# "Platform 4: 08:15" (a colon with a space after it ends a label).
_CLOCK = re.compile(
    r"(?<![\w.,:/])(\d{1,2})(?:[ \u00a0\u202f]?h[ \u00a0\u202f]?|:)(\d{2})(?!\d)"
    r"(?:[ \u00a0\u202f]?([ap])\.?m\b\.?)?",
    re.I,
)
# "vers 12 h 2x" (French for "around 12:2x"), and a time cut short (an
# "arrival ~12h..." in French, staging -104).
_PLACEHOLDER = re.compile(
    r"(?<![\w.,:/])\d{1,2}[ \u00a0\u202f]?(?:h|:)[ \u00a0\u202f]?"
    r"(?:\d?[xX?]{1,2}(?![\w?])|\d?(?:\.{2,3}|…))"
)
# "2 pm", "11am", "14 Uhr": hours of the day without minutes.
_CLOCK_HOUR = re.compile(r"(?<![\w.,:/])(\d{1,2})[   ]?(am|pm|uhr)(?!\w)", re.I)
# "4+ hours" (staging -104) is a duration claim like "4 hours".
_MINUTES_ONLY = re.compile(rf"{_NUMBER}\+?{_SPACE}{_MINUTES}\.?{_END}", re.I)
_HOURS_ONLY = re.compile(
    rf"{_NUMBER}\+?{_SPACE}{_HOURS}\.?(?![\w²³/:])(?![   ]?\d)", re.I
)
_KM = re.compile(rf"{_NUMBER}{_SPACE}{_KILOMETRES}{_END}", re.I)
_M_WORD = re.compile(rf"{_NUMBER}{_SPACE}{_METRES}{_END}", re.I)
# A bare lower-case "m" only: "12 M" is a million.
_M_SYMBOL = re.compile(rf"{_NUMBER}{_SPACE}m{_END}")
_MI = re.compile(rf"{_NUMBER}{_SPACE}{_MILES}{_END}", re.I)
# "14,170", "1 250", "19,90", "3.50": thousands groups, then two decimals.
_PRICE_NUMBER = (
    r"(?P<number>\d{1,3}(?:[,.   ]\d{3})++(?:[.,]\d{1,2})?|\d{1,6}(?:[.,]\d{1,2})?)"
)
_PRICE_CURRENCY = (
    r"(?P<currency>€|euros?|eur|s\$|r\$|\$|usd|dollars?|£|gbp|pounds?|aud|cad|"
    r"chf|¥|yen|jpy|yuan|cny|rmb|₩|krw|₹|inr|rupees?|sgd|nzd|hkd|thb|baht|"
    r"sek|nok|dkk|kr|pln|zł|czk|kč|huf|brl|mxn)"
)
_PRICE_BEFORE = re.compile(rf"{_PRICE_CURRENCY}{_SPACE}{_PRICE_NUMBER}(?![\w])", re.I)
_PRICE_AFTER = re.compile(rf"{_PRICE_NUMBER}{_SPACE}{_PRICE_CURRENCY}(?![\w])", re.I)
# Words that make "10 h" a time of day ("at 10 h", "um 10 Uhr", the French "a 10 h").
_CLOCK_BEFORE = re.compile(
    r"(?:\b(?:at|by|before|after|until|till|from|around|à|a|vers|avant|apres|après|"
    r"jusqu.?à|jusqu.?a|dès|des|um|gegen|vor|nach|bis|ab|alle|las|antes|despues|"
    r"después|hasta|desde|prima|dopo|fino|entre|between|zwischen|tra|fra)"
    r"[   ]*|[-–→>])$",
    re.I,
)
# The lower bound of a range written before a value ("20-", "1,5 to ").
_RANGE_START = re.compile(
    r"(?<![\w.,:/])\d{1,5}(?:[.,]\d{1,2})?[ \u00a0\u202f]?"
    r"(?:[-\u2013\u2014]|\b(?:to|a|à|bis|al)\b)[ \u00a0\u202f]?$",
    re.I,
)
# A margin the person is advised to keep is advice, not a fact to source. Read
# in the value's own clause only: "17 minutes d'avance" is a computed claim, and
# a margin word in the next sentence once hid a walking time (staging -94).
_MARGIN = re.compile(
    r"\b(?:marge|margin|buffer|puffer|margen|margine|reserve|réserve|spare)\b",
    re.I,
)
# Where a clause ends inside a sentence.
_CLAUSE_END = re.compile(r"[,;:()]|\s[\u2013\u2014]\s|\s-\s")
# Advice: what the person is told to aim for, plan or do, five languages
# (folded). A time in advice is a target the person may set, not a fact
# about a service: unsourced, it still goes, but without a removal note
# (staging -136: French advice to "rather aim for 8:30 at the airport", under a plan
# where nothing arrives by 09:00, was quoted in a note as an unverified fact).
_ADVICE = re.compile(
    r"\b(?:"
    # English
    r"aim(?:ing)? (?:for|to|at)|plan (?:to|on|for)|allow (?:for|yourself)|"
    r"make sure|be sure to|try to|you (?:should|could|might|may want to)|"
    r"i(?:'d| would)? (?:recommend|suggest|advise)|recommended|advisable|"
    r"consider|(?:it'?s |it is )?(?:best|better|wiser|safer) to|"
    r"give yourself|leave yourself|target|"
    # French
    r"prevoy(?:ez|ons)|prevois|prevoir|vis(?:ez|ons|er)|essay(?:ez|e)|"
    r"tach(?:ez|e)|compt(?:ez|e) (?:sur|arriver|etre)|"
    r"je (?:vous|te) (?:conseille|recommande|suggere)|"
    r"(?:conseille|recommande|preferable|prudent) d|il vaut mieux|mieux vaut|"
    r"(?:vous|tu) (?:devriez|devrais|pourriez|pourrais)|pensez a|"
    r"assurez.vous|assure.toi|veillez a|"
    # German
    r"planen sie|rechnen sie|ich (?:empfehle|rate)|empfehlenswert|empfohlen|"
    r"sollten sie|sie sollten|solltest|am besten|versuchen sie|"
    r"achten sie|stellen sie sicher|peilen sie|ratsam|"
    # Spanish
    r"procur(?:e|a)|intent(?:e|a)|planee|planea|preve(?:a)?|"
    r"(?:le|te|les) recomiendo|recomendamos|se recomienda|recomendable|"
    r"conviene|es mejor|deberia(?:s)?|asegurese|asegurate|apunt(?:e|a) a|"
    r"cuent(?:e|a) con|"
    # Italian
    r"cerc(?:hi|a) di|preved(?:a|i)|pianific(?:hi|a)|"
    r"(?:le|ti|vi) consiglio|consigliamo|si consiglia|consigliabile|"
    r"e meglio|dovrebbe|dovresti|si assicuri|assicurati|punt(?:i|a) a|"
    r"mett(?:i|a) in conto"
    r")\b"
)
# A service named next to an advised time makes it a fact about that service
# ("take the 08:30 train", "prenez le TER de 8h30"): it keeps its note.
_SERVICE = re.compile(
    r"\b(?:trains?|tren(?:es|o|i)?|zug(?:e|s)?|bus|buses|autobus|coach(?:es)?|"
    r"autocars?|tram(?:way)?s?|metro|u-bahn|s-bahn|rer|ter|tgv|ice|ave|"
    r"intercity|regional|flixbus|ferry|ferries|fahre|bateau|boat|navette|"
    r"shuttle|lines?|lignes?|linie|linea|connections?|correspondances?|"
    r"anschluss|conexion|coincidenza|transbordo|departures?|departs?|"
    r"abfahrt|salida|partenza)\b"
)
_LINE_CODE = re.compile(r"\b[A-Z]{1,4}[ ]?\d{1,5}\b")
# What joins the two ends of a range ("08:30-09:30", "8 h 30 to 9 h 30").
_RANGE_JOIN = re.compile(
    r"^[ \u00a0\u202f]*+"
    r"(?:(?:[-\u2013\u2014]|to|à|a|bis|al|y|e|et|and|und)[ \u00a0\u202f]*+)?$",
    re.I,
)


@dataclass(frozen=True)
class Quantity:
    start: int
    end: int
    kind: Kind
    value: float  # minutes, metres, or minutes of the day
    text: str


@dataclass(frozen=True)
class _Price(Quantity):
    currency: str = ""


def _amount(value: str) -> float:
    """A price as written: "14,170" and "1 250" are thousands, "19,90" and
    "3.50" have cents."""
    groups = re.split(r"[,.   ]", value)
    if len(groups) > 1 and len(groups[-1]) <= 2:
        return float("".join(groups[:-1]) + "." + groups[-1])
    return float("".join(groups))


def _number(value: str) -> float:
    return float(value.replace(",", "."))


def _masked(text: str) -> str:
    """Code, link targets and URLs keep their offsets but are never read."""

    def blank(match: re.Match[str]) -> str:
        return " " * len(match[0])

    text = re.sub(r"```[\s\S]*?(?:```|$)|`[^`\n]*`", blank, text)
    text = re.sub(r"\]\([^)\s]*\)", lambda m: "]" + " " * (len(m[0]) - 1), text)
    return re.sub(r"https?://\S+", blank, text)


def quantities(text: str, *, generous: bool = False) -> list[Quantity]:
    """Durations, distances and clock times written in ``text``.

    ``generous`` is for sources: "2h05" or "1:55" counts as a time and as a
    duration, and "10 h" after "at" counts as both too.
    """
    plain = _masked(text)
    found: list[Quantity] = []
    taken: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return all(end <= a or start >= b for a, b in taken)

    def add(
        match: re.Match[str],
        kind: Kind,
        value: float,
        *,
        claim: bool = True,
        currency: str = "",
    ) -> None:
        taken.append((match.start(), match.end()))
        if claim:
            found.append(
                _Price(match.start(), match.end(), kind, value, match[0], currency)
                if kind == "price"
                else Quantity(match.start(), match.end(), kind, value, match[0])
            )

    for match in _HOURS_MINUTES.finditer(plain):
        add(match, "duration", _number(match[1]) * 60 + int(match[2]))
    for match in _HOURS_M.finditer(plain):
        if free(match.start(), match.end()) and int(match[2]) < 60:
            add(match, "duration", _number(match[1]) * 60 + int(match[2]))
    if not generous:
        # "vers 12 h 2x": a placeholder written for a time no source gave.
        for match in _PLACEHOLDER.finditer(plain):
            add(match, "clock", math.nan)
    for match in _CLOCK.finditer(plain):
        if not free(match.start(), match.end()):
            continue
        hours, minutes = int(match[1]), int(match[2])
        if minutes >= 60:
            continue
        if match[3] and hours <= 12:
            hours = hours % 12 + (12 if match[3].lower() == "p" else 0)
        if hours <= 24:
            add(match, "clock", (hours % 24) * 60 + minutes)
        if generous:
            found.append(
                Quantity(
                    match.start(),
                    match.end(),
                    "duration",
                    hours * 60 + minutes,
                    match[0],
                )
            )
        elif hours > 24:
            taken.append((match.start(), match.end()))
    for match in _CLOCK_HOUR.finditer(plain):
        if not free(match.start(), match.end()):
            continue
        hours = int(match[1])
        suffix = match[2].lower()
        if suffix == "pm" and hours < 12:
            hours += 12
        if suffix == "am" and hours == 12:
            hours = 0
        if hours <= 24:
            add(match, "clock", (hours % 24) * 60)
    for match in _MINUTES_ONLY.finditer(plain):
        if free(match.start(), match.end()):
            add(match, "duration", _number(match[1]))
    for match in _HOURS_ONLY.finditer(plain):
        if not free(match.start(), match.end()):
            continue
        amount = _number(match[1])
        before = plain[max(0, match.start() - 24) : match.start()]
        if amount <= 24 and amount == int(amount) and _CLOCK_BEFORE.search(before):
            # "at 10 h": a time of the day, not a duration.
            add(match, "clock", (int(amount) % 24) * 60)
            if generous:
                found.append(
                    Quantity(
                        match.start(), match.end(), "duration", amount * 60, match[0]
                    )
                )
            continue
        add(match, "duration", amount * 60)
    for pattern, factor in (
        (_KM, 1000.0),
        (_MI, 1609.344),
        (_M_WORD, 1.0),
        (_M_SYMBOL, 1.0),
    ):
        for match in pattern.finditer(plain):
            if free(match.start(), match.end()):
                add(match, "distance", _number(match[1]) * factor)
    # "$17 AUD": the amount with its code first, the symbol alone after.
    for match in (*_PRICE_AFTER.finditer(plain), *_PRICE_BEFORE.finditer(plain)):
        if free(match.start(), match.end()):
            add(
                match,
                "price",
                _amount(match["number"]),
                currency=match["currency"].upper(),
            )
    # "20-25 min", "1,5 to 2 km": the lower bound of a range goes with the
    # value, and is checked too (staging -98: "20–25 min" was kept because a
    # source said "25 min" about something else, its "20" was never read).
    widened: list[Quantity] = []
    for quantity in found:
        lower = _RANGE_START.search(plain, 0, quantity.start)
        if (
            lower is not None
            and lower.end() == quantity.start
            and quantity.kind != "clock"
        ):
            numbers = re.findall(r"\d+(?:[.,]\d+)?", quantity.text)
            upper = _number(numbers[0]) if len(numbers) == 1 else 0.0
            start_number = re.match(r"\d+(?:[.,]\d+)?", lower[0])
            low = (
                _number(start_number[0]) * quantity.value / upper
                if upper and start_number
                else None
            )
            quantity = _Range(
                lower.start(),
                quantity.end,
                quantity.kind,
                quantity.value,
                text[lower.start() : quantity.end],
                low=quantity.value if low is None else low,
            )
        widened.append(quantity)
    # "4 hours." ends a sentence: its full stop is not part of the value.
    trimmed = [
        replace(q, end=q.end - 1, text=q.text[:-1])
        if q.text.endswith(".") and plain[q.end : q.end + 1] in {"", " ", "\n"}
        else q
        for q in widened
    ]
    return sorted(trimmed, key=lambda q: (q.start, q.kind))


# --- The person's own times and dates, in words or figures. -----------------
# Staging -94: "midi trente" and "by 2pm" came back marked unsourced as "12 h 30"
# and "2:00 (not sourced) pm". Times the person gave count as theirs whatever
# the form; read on folded text (lower case, no accents), five languages.

_NUMBER_WORDS = {
    word: value
    for value, words in enumerate(
        (
            "zero zero null cero zero",
            "one un une eins uno una",
            "two deux zwei dos due",
            "three trois drei tres tre",
            "four quatre vier cuatro quattro",
            "five cinq funf cinco cinque",
            "six six sechs seis sei",
            "seven sept sieben siete sette",
            "eight huit acht ocho otto",
            "nine neuf neun nueve nove",
            "ten dix zehn diez dieci",
            "eleven onze elf once undici",
            "twelve douze zwolf doce dodici",
            "thirteen treize dreizehn trece tredici",
            "fourteen quatorze vierzehn catorce quattordici",
            "fifteen quinze funfzehn quince quindici",
            "sixteen seize sechzehn dieciseis sedici",
            "seventeen dix-sept siebzehn diecisiete diciassette",
            "eighteen dix-huit achtzehn dieciocho diciotto",
            "nineteen dix-neuf neunzehn diecinueve diciannove",
            "twenty vingt zwanzig veinte venti",
            "twenty-one vingt-et-un einundzwanzig veintiuno ventuno",
            "twenty-two vingt-deux zweiundzwanzig veintidos ventidue",
            "twenty-three vingt-trois dreiundzwanzig veintitres ventitre",
        )
    )
    for word in words.split()
}
_NOON = r"(?:midi|noon|midday|mittag|mediodia|mezzogiorno)"
_MIDNIGHT = r"(?:minuit|midnight|mitternacht|medianoche|mezzanotte)"
_HOUR_WORD = (
    r"(\d{1,2}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"
)
_HALF = r"(?:et demie?|trente|thirty|half|und halb|y media|e mezz[ao])"
_QUARTER = r"(?:et quart|quinze|fifteen|y cuarto|e un quarto|e quarto)"
_LESS_QUARTER = (
    r"(?:moins le quart|moins quart|menos cuarto|meno un quarto|meno quarto)"
)
_SPOKEN: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], int]], ...] = (
    (re.compile(rf"\b{_NOON}(?:\s+{_HALF})"), lambda m: 12 * 60 + 30),
    (re.compile(rf"\b{_NOON}(?:\s+{_QUARTER})"), lambda m: 12 * 60 + 15),
    (re.compile(rf"\b{_NOON}(?:\s+{_LESS_QUARTER})"), lambda m: 11 * 60 + 45),
    (
        re.compile(rf"\b{_NOON}\s+(?:et\s+)?(\d{{1,2}})\b(?!\s*h)"),
        lambda m: 12 * 60 + int(m[1]),
    ),
    (re.compile(rf"\b{_NOON}\b"), lambda m: 12 * 60),
    (re.compile(rf"\b{_MIDNIGHT}(?:\s+{_HALF})"), lambda m: 30),
    (re.compile(rf"\b{_MIDNIGHT}\b"), lambda m: 0),
    (re.compile(rf"\bhalf past {_HOUR_WORD}\b"), lambda m: _hour(m[1]) * 60 + 30),
    (re.compile(rf"\bquarter past {_HOUR_WORD}\b"), lambda m: _hour(m[1]) * 60 + 15),
    (
        re.compile(rf"\bquarter to {_HOUR_WORD}\b"),
        lambda m: (_hour(m[1]) - 1) * 60 + 45,
    ),
    (re.compile(rf"\bhalb {_HOUR_WORD}\b"), lambda m: (_hour(m[1]) - 1) * 60 + 30),
    (re.compile(rf"\bviertel nach {_HOUR_WORD}\b"), lambda m: _hour(m[1]) * 60 + 15),
    (
        re.compile(rf"\bviertel vor {_HOUR_WORD}\b"),
        lambda m: (_hour(m[1]) - 1) * 60 + 45,
    ),
    (
        re.compile(rf"\b{_HOUR_WORD}\s+(?:heures?|uhr|o.?clock)\s+{_HALF}"),
        lambda m: _hour(m[1]) * 60 + 30,
    ),
    (
        re.compile(rf"\b{_HOUR_WORD}\s+(?:heures?|uhr)\s+{_QUARTER}"),
        lambda m: _hour(m[1]) * 60 + 15,
    ),
    (
        re.compile(rf"\b{_HOUR_WORD}\s+(?:heures?)\s+{_LESS_QUARTER}"),
        lambda m: (_hour(m[1]) - 1) * 60 + 45,
    ),
    (
        re.compile(rf"\b{_HOUR_WORD}\s+(?:heures?|uhr)\s+(\d{{1,2}})\b"),
        lambda m: _hour(m[1]) * 60 + int(m[2]),
    ),
    (
        re.compile(rf"\b{_HOUR_WORD}\s+(?:heures?|uhr|o.?clock)\b"),
        lambda m: _hour(m[1]) * 60,
    ),
    (
        re.compile(rf"\b(?:las|la|le|alle|ore)\s+{_HOUR_WORD}\s+{_HALF}"),
        lambda m: _hour(m[1]) * 60 + 30,
    ),
    (
        re.compile(rf"\b(?:las|la|le|alle|ore)\s+{_HOUR_WORD}\s+{_QUARTER}"),
        lambda m: _hour(m[1]) * 60 + 15,
    ),
    (
        re.compile(rf"\b(?:las|la|le|alle|ore)\s+{_HOUR_WORD}\s+{_LESS_QUARTER}"),
        lambda m: (_hour(m[1]) - 1) * 60 + 45,
    ),
    (re.compile(rf"\b{_HOUR_WORD}\s+thirty\b"), lambda m: _hour(m[1]) * 60 + 30),
)


def _hour(word: str) -> int:
    return int(word) if word.isdigit() else _NUMBER_WORDS[word]


def _folded(text: str) -> str:
    import unicodedata

    value = unicodedata.normalize("NFKD", text.lower().replace("’", "'"))
    return " ".join("".join(c for c in value if not unicodedata.combining(c)).split())


def spoken_times(text: str) -> list[float]:
    """Times of day the text gives in words ("midi trente", "half past twelve")."""
    folded = _folded(text)
    found: list[float] = []
    taken: list[tuple[int, int]] = []
    # Longer forms are listed first: "midi trente" is not also "midi".
    for pattern, value in _SPOKEN:
        for match in pattern.finditer(folded):
            if any(match.start() < b and a < match.end() for a, b in taken):
                continue
            try:
                minutes = value(match)
            except (KeyError, ValueError):
                continue
            if 0 <= minutes < 24 * 60:
                taken.append((match.start(), match.end()))
                found.append(float(minutes))
    return found


_RELATIVE_DAYS = (
    (
        re.compile(
            r"\b(?:apres.demain|day after tomorrow|ubermorgen|pasado manana|dopodomani)\b"
        ),
        2,
    ),
    (
        re.compile(
            # Frequent spellings of tomorrow in chat ("tmrw", "tomorow").
            r"\b(?:demain|tomorrow|morgen|manana|domani|tmrw|tmrrw|tmr|tmw|"
            r"tom+or+ow|2mor+ow|2moro|tomoz|demin|dmain|domai)\b"
        ),
        1,
    ),
    (
        re.compile(
            r"\b(?:aujourd.hui|today|tonight|ce soir|ce matin|heute|hoy|oggi|stasera)\b"
        ),
        0,
    ),
)
_WEEKDAYS = (
    "lundi monday montag lunes lunedi",
    "mardi tuesday dienstag martes martedi",
    "mercredi wednesday mittwoch miercoles mercoledi",
    "jeudi thursday donnerstag jueves giovedi",
    "vendredi friday freitag viernes venerdi",
    "samedi saturday samstag sabado sabato",
    "dimanche sunday sonntag domingo domenica",
)


def user_dates(texts: list[str], zone: str = "UTC", now: Any = None) -> list[str]:
    """ISO dates the person's words name relative to today, in their zone.

    ``now`` (an aware datetime) is the caller's clock: the journey planner
    passes its own, so "tomorrow" and the planned day come from one clock.
    Without one, the orchestrator's clock (datecalc.utc_now)."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from services.orchestrator import datecalc

    instant = now or datecalc.utc_now()
    try:
        today = instant.astimezone(ZoneInfo(zone)).date()
    except (ZoneInfoNotFoundError, ValueError):
        today = instant.astimezone(ZoneInfo("UTC")).date()
    days: set[int] = set()
    named: set[str] = set()
    for text in texts:
        folded = _folded(text)
        for pattern, offset in _RELATIVE_DAYS:
            if pattern.search(folded):
                days.add(offset)
        for weekday, words in enumerate(_WEEKDAYS):
            if re.search(r"\b(?:" + "|".join(words.split()) + r")\b", folded):
                ahead = (weekday - today.weekday()) % 7
                days.update({ahead, ahead + 7} if ahead == 0 else {ahead})
        named |= named_dates(text, today.isoformat())
    relative = {(today + timedelta(days=offset)).isoformat() for offset in days}
    return sorted(relative | named)


# --- Dates written in a text ("23.11.26", "18 novembre", "Nov 18, 2026"). ---
# Staging -102: a timetable page gave the times of 23.11.26 for a trip asked
# for tomorrow (18.11.26), and the answer listed them as if they were
# tomorrow's. A time read next to another date is a fact about that date.

_MONTHS = (
    "janvier january januar enero gennaio jan janv ene",
    "fevrier february februar febrero febbraio feb fev fevr",
    "mars march marz marzo",
    "avril april abril aprile apr avr",
    "mai may mayo maggio",
    "juin june juni junio giugno jun giu",
    "juillet july juli julio luglio jul juil lug",
    "aout august agosto aug",
    "septembre september septiembre settembre sep sept",
    "octobre october oktober octubre ottobre oct okt ott",
    "novembre november noviembre nov",
    "decembre december dezember diciembre dicembre dec dez dic",
)
_MONTH_NUMBER = {
    word: number for number, words in enumerate(_MONTHS, 1) for word in words.split()
}
_MONTH = r"(" + "|".join(sorted(_MONTH_NUMBER, key=len, reverse=True)) + r")"


def page_key(url: str) -> str:
    """One form of a page address: no scheme, "www." or closing slash."""
    parts = urlsplit(url.strip().rstrip(".,;"))
    return parts.netloc.lower().removeprefix("www.") + parts.path.rstrip("/")


def _text_key(text: str) -> str:
    return " ".join(text.split())[:200]


# --- Values of another leg. ----------------------------------------------------
# Staging -118: "The Acela ... takes 7 hours ([amtrak.com](...))" for New York -
# Boston, a value the page gives for another stretch of the line. A duration or
# a distance a source sentence states for two places, one of which neither the
# person, nor their starting point, nor the answer names, is the value of
# another leg: it supports nothing in this trip's answer.
_NAME = r"[A-ZÀ-ÖØ-Þ][\w'’.]*+(?:[ -][A-ZÀ-ÖØ-Þ][\w'’.]*+){0,3}+"
_ARTICLE = r"(?:(?i:la|le|les|l'|the|der|die|dem|den|el|los|il|lo)\s*+)?+"
_LEG = re.compile(
    r"(?<![\w-])(?i:from|de|du|des|depuis|von|vom|ab|desde|da|dal|dalla|"
    r"between|entre|zwischen|tra|fra|connects?|connecting|relie|reliant|"
    r"verbindet|conecta|collega)\s++"
    + _ARTICLE
    + rf"({_NAME})(?:,\s*+{_NAME})?+\s*+(?i:to|and|with|à|a|au|aux|et|avec|nach|bis|"
    r"und|mit|zum|zur|hasta|y|con|e|al|jusqu'à)\s++"
    + _ARTICLE
    + rf"({_NAME})"
    + rf"|({_NAME})(?:\s++[–—-]\s++|\s*+(?:→|->)\s*+)({_NAME})"
)


def _name_words(name: str) -> set[str]:
    return {
        word
        for word in normalized(name).split()
        if (len(word) >= 3 or any(ord(character) > 0x2F80 for character in word))
        and word not in route_facts._GENERIC
        and word not in route_facts.STATION_WORDS
        and word not in route_facts.PLACE_WORDS
        and word not in _MONTH_NUMBER
    }


def another_leg(sentence: str, places: frozenset[str]) -> bool:
    """Whether a source sentence names a leg with an end outside ``places``."""
    if not places:
        return False
    for match in _LEG.finditer(sentence):
        for end in match.groups():
            words = _name_words(end) if end else set()
            if words and not words & places:
                return True
    return False


# "from Sydney Airport to City of Sydney": a leg ending at a city named after
# its kind ("City of", "Stadt", "Ciudad de").
_CITY_LEG = re.compile(
    rf"(?i:from|de|von|da|desde)\s++({_NAME})\s++(?i:to|à|a|nach|para|al|zu)\s++"
    rf"(?i:the\s++)?+(?i:city|cité|ville|città|ciudad|stadt)\s++(?i:of|de|di|del)?+\s*+({_NAME})"
)


_UNORDERED = re.compile(
    r"(?i:between|entre|zwischen|tra|fra|connects?|connecting|relie|reliant|"
    r"verbindet|conecta|collega)\s"
)


def reverse_leg(
    sentence: str, starting_point: str, place_names: list[str] | None = None
) -> bool:
    """Whether a sentence gives a leg towards the starting point from elsewhere:
    the opposite direction of the person's trip.

    The leg ends at the starting point (or a station of it) and starts at a
    place apart from it (staging -120: "the T8 from Sydney Airport to City of
    Sydney" for a trip from a Sydney street to the airport; "de Barcelona a
    Madrid" for a trip from Madrid). A leg out of the starting point is never
    one, even when both ends share the city's name ("from a Sydney street to
    Sydney Airport"). A time, frequency, platform or departure of that leg is
    no fact of the trip.
    """
    parts = [part.strip() for part in starting_point.split(",")]
    origin = _name_words(" ".join(parts[:-1] if len(parts) >= 3 else parts))
    origin |= _name_words(" ".join(place_names or []))
    if not origin:
        return False
    for match in (*_LEG.finditer(sentence), *_CITY_LEG.finditer(sentence)):
        first, second = match.group(1), match.group(2)
        # "between X and Y", "connects X with Y": no direction.
        if not first or not second or _UNORDERED.match(match[0]):
            continue
        start, end = _name_words(first), _name_words(second)
        if not end & origin or not start - origin:
            continue
        # Towards the starting point: from a place apart from it, or to the
        # starting point itself when both ends share the city's name.
        if not start & origin or end <= origin:
            return True
    return False


# Sentences of a source for the leg check: "Washington, D.C. and Boston" is
# one (an abbreviation's points end no sentence).
_LEG_SENTENCE = re.compile(r"(?<=\w\w[.!?;])\s++|\n++")
# The pages an answer sentence cites (markdown links, bare links).
_CITATION = re.compile(r"\((https?://[^)\s]++)\)|<(https?://[^>\s]++)>")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_NUMERIC_DATE = re.compile(
    r"(?<![\d.:/])(\d{1,2})([./-])(\d{1,2})\2(\d{4}|\d{2})(?![\d:])"
)
_DAY_MONTH = re.compile(
    rf"(?<![\w:])(\d{{1,2}})(?:er|st|nd|rd|th|\.)?\s+(?:de\s+)?{_MONTH}\.?"
    r"(?:\s+(?:de\s+)?(\d{4}))?(?!\w)"
)
_MONTH_DAY = re.compile(
    rf"(?<!\w){_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?(?![\w:])"
)


def named_dates(text: str, today: str) -> set[str]:
    """ISO dates the text writes as a day and a month (any order, five languages).

    A numeric date that reads both ways ("05/11/2026") gives both; a date
    without a year is the one nearest ``today``.
    """
    from datetime import date

    base = date.fromisoformat(today)
    found: set[str] = set()

    def add(year: int | None, month: int, day: int) -> None:
        years = (
            [year] if year is not None else [base.year - 1, base.year, base.year + 1]
        )
        candidates: list[date] = []
        for value in years:
            try:
                candidates.append(date(value, month, day))
            except ValueError:
                continue
        if candidates:
            nearest = min(candidates, key=lambda d: abs((d - base).days))
            found.add(nearest.isoformat())

    def full(year: str | None) -> int | None:
        if not year:
            return None
        number = int(year)
        return number + 2000 if number < 100 else number

    folded = _folded(text)
    for match in _ISO_DATE.finditer(folded):
        add(int(match[1]), int(match[2]), int(match[3]))
    for match in _NUMERIC_DATE.finditer(folded):
        first, second, year = int(match[1]), int(match[3]), full(match[4])
        if 1 <= second <= 12:
            add(year, second, first)  # day first
        if 1 <= first <= 12 and first != second:
            add(year, first, second)  # month first
    for match in _DAY_MONTH.finditer(folded):
        add(full(match[3]), _MONTH_NUMBER[match[2]], int(match[1]))
    for match in _MONTH_DAY.finditer(folded):
        add(full(match[3]), _MONTH_NUMBER[match[1]], int(match[2]))
    return found


_EVIDENCE_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")
# Days a timetable row may stand for instead of the trip's: a date further
# away is a validity date ("valid from 14.12.2025"), not a row's day.
NEAR_DAYS = 45


def other_dates(sentence: str, asked: set[str]) -> set[str]:
    """The days near the trip a sentence gives, when none is a trip day."""
    from datetime import date

    if not asked:
        return set()
    trip = [date.fromisoformat(day) for day in asked]
    dates = {
        day
        for day in named_dates(sentence, min(asked))
        if min(abs((date.fromisoformat(day) - t).days) for t in trip) <= NEAR_DAYS
    }
    return set() if dates & asked else dates


def _rounded(claimed: float, known: float) -> bool:
    """A trip duration rounded to the half hour ("about 4 h" for 4 h 02 min)."""
    return claimed >= 60 and claimed % 30 == 0 and abs(claimed - known) <= 15


def _same(kind: Kind, claimed: float, known: float) -> bool:
    if kind == "clock":
        # "2:00 pm" and "14:00", or a "2 pm" written as "2:00": the same time.
        gap = abs(claimed - known) % 720
        return min(gap, 720 - gap) < 0.5
    if kind == "duration":
        return abs(claimed - known) <= max(0.5, 0.05 * known)
    return abs(claimed - known) <= max(25.0, 0.03 * known)


_CURRENCY = re.compile(_PRICE_CURRENCY, re.I)


_CURRENCY_CODE = {
    "€": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "pound": "GBP",
    "pounds": "GBP",
    "gbp": "GBP",
    "dollar": "USD",
    "dollars": "USD",
    "usd": "USD",
    "aud": "AUD",
    "cad": "CAD",
    "chf": "CHF",
    "$": "$",
    "¥": "JPY",
    "yen": "JPY",
    "jpy": "JPY",
    "yuan": "CNY",
    "cny": "CNY",
    "rmb": "CNY",
    "₩": "KRW",
    "krw": "KRW",
    "₹": "INR",
    "inr": "INR",
    "rupee": "INR",
    "rupees": "INR",
    "sgd": "SGD",
    "s$": "SGD",
    "nzd": "NZD",
    "hkd": "HKD",
    "thb": "THB",
    "baht": "THB",
    "sek": "SEK",
    "nok": "NOK",
    "dkk": "DKK",
    "kr": "KR",
    "pln": "PLN",
    "zł": "PLN",
    "czk": "CZK",
    "kč": "CZK",
    "huf": "HUF",
    "brl": "BRL",
    "r$": "BRL",
    "mxn": "MXN",
}
# "$" is the dollar of the page's country: it matches any dollar.
_DOLLARS = frozenset({"$", "USD", "AUD", "CAD"})


def _currency(quantity: Quantity) -> str:
    """The currency of a price or of a range of prices ("$15–17 AUD": the
    code wins over the symbol)."""
    names = [str(getattr(quantity, "currency", ""))]
    names += [m["currency"] for m in _CURRENCY.finditer(quantity.text)]
    codes = [_CURRENCY_CODE.get(n.lower(), "") for n in names if n]
    named = [code for code in codes if code and code != "$"]
    return named[0] if named else ("$" if "$" in codes else "")


def _same_price(claimed: Quantity, known: Quantity) -> bool:
    currency = _currency(claimed)
    return (
        bool(currency)
        and (
            currency == (other := _currency(known))
            or {currency, other} <= _DOLLARS
            and "$" in {currency, other}
        )
        and abs(claimed.value - known.value) <= max(0.01, 0.03 * known.value)
    )


WORDING: dict[str, dict[str, str]] = {
    "note": {
        "en": "Not verified, so left out: {values}. No page read for this answer gives them, and Atlas does not compute routes yet.",
        "fr": "Non vérifié, donc retiré : {values}. Aucune page consultée pour cette réponse ne le donne, et Atlas ne calcule pas encore d’itinéraire.",
        "de": "Nicht überprüft und daher weggelassen: {values}. Keine für diese Antwort gelesene Seite nennt es, und Atlas berechnet noch keine Routen.",
        "es": "Sin verificar, por eso se omite: {values}. Ninguna página consultada para esta respuesta lo indica, y Atlas aún no calcula rutas.",
        "it": "Non verificato, quindi omesso: {values}. Nessuna pagina consultata per questa risposta lo indica, e Atlas non calcola ancora gli itinerari.",
    },
    # The same note when a line, a stop, a direction or a direct service was
    # left out: it says where to check them instead.
    "route": {
        "en": "Not verified, so left out: {values}. No page read for this answer confirms them, and Atlas does not compute routes yet: check lines, stops and connections in the operator’s journey planner.",
        "fr": "Non vérifié, donc retiré : {values}. Aucune page consultée pour cette réponse ne le confirme, et Atlas ne calcule pas encore d’itinéraire : vérifiez lignes, arrêts et correspondances dans le calculateur d’itinéraire de l’exploitant.",
        "de": "Nicht überprüft und daher weggelassen: {values}. Keine für diese Antwort gelesene Seite bestätigt es, und Atlas berechnet noch keine Routen: Prüfen Sie Linien, Haltestellen und Umstiege in der Fahrplanauskunft des Verkehrsbetriebs.",
        "es": "Sin verificar, por eso se omite: {values}. Ninguna página consultada para esta respuesta lo confirma, y Atlas aún no calcula rutas: compruebe líneas, paradas y transbordos en el planificador de viajes del operador.",
        "it": "Non verificato, quindi omesso: {values}. Nessuna pagina consultata per questa risposta lo conferma, e Atlas non calcola ancora gli itinerari: controlli linee, fermate e coincidenze nel pianificatore di viaggio dell’operatore.",
    },
    # The same note when the pages read give another line for the route.
    "disputed": {
        "en": "Not verified, so left out: {values}. The pages read for this answer disagree on the line, and Atlas does not compute routes yet: check lines, stops and connections in the operator’s journey planner.",
        "fr": "Non vérifié, donc retiré : {values}. Les pages consultées pour cette réponse ne donnent pas la même ligne, et Atlas ne calcule pas encore d’itinéraire : vérifiez lignes, arrêts et correspondances dans le calculateur d’itinéraire de l’exploitant.",
        "de": "Nicht überprüft und daher weggelassen: {values}. Die für diese Antwort gelesenen Seiten nennen nicht dieselbe Linie, und Atlas berechnet noch keine Routen: Prüfen Sie Linien, Haltestellen und Umstiege in der Fahrplanauskunft des Verkehrsbetriebs.",
        "es": "Sin verificar, por eso se omite: {values}. Las páginas consultadas para esta respuesta no indican la misma línea, y Atlas aún no calcula rutas: compruebe líneas, paradas y transbordos en el planificador de viajes del operador.",
        "it": "Non verificato, quindi omesso: {values}. Le pagine consultate per questa risposta non indicano la stessa linea, e Atlas non calcola ancora gli itinerari: controlli linee, fermate e coincidenze nel pianificatore di viaggio dell’operatore.",
    },
    # Departures left out because they arrive after the time the person gave.
    "late": {
        "en": "Left out because they arrive after {deadline}: {values}.",
        "fr": "Retiré car l’arrivée est après {deadline} : {values}.",
        "de": "Weggelassen, da die Ankunft nach {deadline} liegt: {values}.",
        "es": "Se omite porque llega después de las {deadline}: {values}.",
        "it": "Omesso perché arriva dopo le {deadline}: {values}.",
    },
}


# The clause of the notes that says Atlas computes no route, and what a turn
# whose itineraries a journey planner computed says instead.
_PLANNED: dict[str, tuple[str, str]] = {
    "en": (
        ", and Atlas does not compute routes yet",
        ", nor do the itineraries computed for it",
    ),
    "fr": (
        ", et Atlas ne calcule pas encore d\u2019itin\u00e9raire",
        ", ni les itin\u00e9raires calcul\u00e9s pour elle",
    ),
    "de": (
        ", und Atlas berechnet noch keine Routen",
        ", und die f\u00fcr sie berechneten Verbindungen auch nicht",
    ),
    "es": (
        ", y Atlas a\u00fan no calcula rutas",
        ", ni los itinerarios calculados para ella",
    ),
    "it": (
        ", e Atlas non calcola ancora gli itinerari",
        ", n\u00e9 gli itinerari calcolati per essa",
    ),
}


# The same clause when a journey planner is configured but computed no
# itinerary for this trip (staging -129: it found none for Sydney, and the note
# said that Atlas computes no route).
_NO_PLAN: dict[str, str] = {
    "en": ", and no computed itinerary was found for this trip",
    "fr": ", et aucun itin\u00e9raire calcul\u00e9 n\u2019a \u00e9t\u00e9 trouv\u00e9 pour ce trajet",
    "de": ", und f\u00fcr diese Reise wurde keine berechnete Verbindung gefunden",
    "es": ", y no se encontr\u00f3 ning\u00fan itinerario calculado para este trayecto",
    "it": ", e non \u00e8 stato trovato alcun itinerario calcolato per questo tragitto",
}

_QUOTE_BEFORE = 40


def _quote(text: str, claim: Quantity) -> str:
    """The words of the answer that state a value left out, quoted: its table
    row's name and cell, else its clause (its sentence when the clause is the
    value alone), cut around the value when long."""
    line_start, _ = _line_bounds(text, claim.start)
    if text[line_start:].lstrip().startswith("|"):
        cells = _row_cells(text, claim.start)
        start, end = _cell_bounds(text, claim.start)
        name = (
            text[cells[0][0] : cells[0][1]].strip()
            if cells and cells[0][0] != start
            else ""
        )
        cell = text[start:end].strip()
        return route_facts.quoted(f"{name}: {cell}" if name else cell) or claim.text
    start, end = _clause(text, claim.start, claim.end)
    rest = text[start : claim.start] + text[claim.end : end]
    if not re.search(r"[^\W\d_]{3}", rest):
        start, end = _sentence(text, claim.start, claim.end)
    prefix = ""
    if claim.start - start > _QUOTE_BEFORE:
        cut = text.find(" ", claim.start - _QUOTE_BEFORE, claim.start)
        start, prefix = (cut + 1 if cut >= 0 else claim.start), "\u2026"
    return route_facts.quoted(prefix + text[start:end]) or claim.text


def wording(key: str, language: str) -> str:
    table = WORDING[key]
    return table.get(language, table["en"])


def _line_bounds(text: str, index: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return start, len(text) if end < 0 else end


# --- Leaving out what no source states. -------------------------------------
# Staging -94: markers after every value broke sentences ("environ (duree non
# verifiee) jusqu'au quai", "08:30 (not sourced)-09:30 (not sourced)"). The
# clause stating an unsourced value is left out whole (its parentheses, its
# part of the sentence, or the sentence); a table cell becomes a dash and a
# row without any remaining value goes; one short note lists what was left out.


@dataclass(frozen=True)
class _Range(Quantity):
    low: float = 0.0
    currency: str = ""


# A table cell emptied here (shown as a dash): rows and tables that lost every
# value go, other tables are left as they are.
_LEFT_OUT = "\u2060\u2014\u2060"


def _ranges(text: str, found: list[Quantity]) -> list[Quantity]:
    """Two values of one kind joined as a range ("08:30-09:30") are one claim."""
    merged: list[Quantity] = []
    for quantity in found:
        if merged:
            last = merged[-1]
            between = text[last.end : quantity.start]
            if last.kind == quantity.kind and _RANGE_JOIN.match(between):
                merged[-1] = _Range(
                    last.start,
                    quantity.end,
                    quantity.kind,
                    quantity.value,
                    text[last.start : quantity.end],
                    low=last.value if not isinstance(last, _Range) else last.low,
                    currency=getattr(quantity, "currency", "")
                    or getattr(last, "currency", ""),
                )
                continue
        merged.append(quantity)
    return merged


def _range_values(claim: Quantity) -> tuple[float, ...]:
    return (claim.low, claim.value) if isinstance(claim, _Range) else (claim.value,)


def _sentence(text: str, start: int, end: int) -> tuple[int, int]:
    line_start, line_end = _line_bounds(text, start)
    line = text[line_start:line_end]
    left = line_start
    right = line_end
    for match in route_facts.sentence_marks(line):
        if (
            line_start + match.end() <= start
            and text[line_start + match.end() : line_start + match.end() + 1].isspace()
        ):
            left = line_start + match.end()
        elif line_start + match.start() >= end:
            right = line_start + match.end()
            break
    return left, right


def _clause(text: str, start: int, end: int) -> tuple[int, int]:
    """The part of the sentence around a value, between clause marks."""
    left, right = _sentence(text, start, end)
    marks = [m for m in _CLAUSE_END.finditer(text, left, right)]
    before = [m for m in marks if m.end() <= start]
    after = [m for m in marks if m.start() >= end]
    clause_start = before[-1].end() if before else left
    if after:
        clause_end = after[0].start()
    else:
        clause_end = right - 1 if right > end and text[right - 1] in ".!?" else right
    return clause_start, clause_end


def _advice(text: str, claim: Quantity) -> bool:
    """A time the answer advises the person to aim for or plan, naming no
    service: its clause, or the start of its sentence when only commas lie
    between, has an advice form (_ADVICE) and neither has a service or a
    line code (_SERVICE, _LINE_CODE). In French, "allow time: the train
    leaves at 8:30" states a fact after the colon; "if possible, I advise
    you to arrive at 8:30" advises (tests/test_trip_answers_136.py)."""
    if claim.kind != "clock":
        return False
    left, _ = _sentence(text, claim.start, claim.end)
    start, end = _clause(text, claim.start, claim.end)
    parts = [text[start:end]]
    lead = text[left:start]
    if not re.search(r"[;:()]|\s[\u2013\u2014-]\s", lead):
        parts.append(lead)
    words = " ".join(parts)
    if _SERVICE.search(_folded(words)) or _LINE_CODE.search(words):
        return False
    return any(_ADVICE.search(_folded(part)) for part in parts)


def _span_to_remove(text: str, claim: Quantity) -> tuple[int, int]:
    """The parenthesis holding the value, else its whole sentence.

    Commas inside addresses and lists make clauses unreliable ("Votre
    position, 12 rue ..., est a 10 minutes" lost its predicate and kept a
    fragment): a sentence goes whole, so no broken phrase is left.
    """
    left, right = _sentence(text, claim.start, claim.end)
    opening = text.rfind("(", left, claim.start)
    closing = text.find(")", claim.end, right)
    if (
        opening >= 0
        and closing >= 0
        and ")" not in text[opening : claim.start]
        and "(" not in text[claim.end : closing]
    ):
        start = opening - 1 if opening > 0 and text[opening - 1] == " " else opening
        return start, closing + 1
    return left, right


def _cell_bounds(text: str, index: int) -> tuple[int, int]:
    line_start, line_end = _line_bounds(text, index)
    left = text.rfind("|", line_start, index)
    right = text.find("|", index, line_end)
    return left + 1, (right if right >= 0 else line_end)


def _removal(text: str, claim: Quantity) -> tuple[int, int]:
    """What leaving ``claim`` out removes: its table cell, else its clause."""
    line_start, _ = _line_bounds(text, claim.start)
    if text[line_start:].lstrip().startswith("|"):
        return _cell_bounds(text, claim.start)
    return _span_to_remove(text, claim)


def _row_cells(text: str, index: int) -> list[tuple[int, int]]:
    """The cells of the table row at ``index``, first to last."""
    line_start, line_end = _line_bounds(text, index)
    bars = [i for i in range(line_start, line_end) if text[i] == "|"]
    cells = list(zip(bars, bars[1:]))
    if bars and text[bars[-1] + 1 : line_end].strip():
        cells.append((bars[-1], line_end))
    return [(left + 1, right) for left, right in cells]


def _remainder(text: str, start: int, end: int) -> int:
    """Where the rest of a sentence goes too when its first clause goes.

    A sentence that loses its opening clause up to a semicolon keeps a
    remainder that opens a sentence of its own. Staging -115: "El AVE ...
    tarda unas 2 h 30 min a 3 h; de eso arriesgas llegar justo o tarde."
    left "De eso arriesgas llegar justo o tarde." to open the answer. A
    remainder that states no value, line or place of its own only went on
    from the clause that went, and goes with it; one with its own facts
    stays (staging -102: "the fastest connection takes 3:57 hours").
    """
    if text[start:end].rstrip()[-1:] != ";":
        return end
    line_start, line_end = _line_bounds(text, start)
    before = _ITEM.sub("", text[line_start:start], count=1).strip(" \t*_")
    if before and before[-1:] not in {".", "!", "?"}:
        return end
    stop = re.compile(r"[.!?](?=\s|$)").search(text, end, line_end)
    close = stop.end() if stop else line_end
    rest = text[end:close]
    if not rest.strip() or quantities(rest):
        return end
    if route_facts.mentions(rest, _masked(rest)):
        return end
    return close


def _merged(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Overlapping spans: the widest wins."""
    spans = sorted(spans, key=lambda span: (span[0], -span[1]))
    kept: list[tuple[int, int, str]] = []
    for span in spans:
        if kept and span[0] < kept[-1][1]:
            if span[1] > kept[-1][1]:
                kept[-1] = (kept[-1][0], span[1], kept[-1][2])
            continue
        kept.append(span)
    return kept


def _leave_out(text: str, flagged: list[Quantity]) -> str:
    spans: list[tuple[int, int, str]] = []
    for claim in flagged:
        line_start, _ = _line_bounds(text, claim.start)
        if text[line_start:].lstrip().startswith("|"):
            cells = _row_cells(text, claim.start)
            start, end = _cell_bounds(text, claim.start)
            if cells and cells[0][0] == start:
                # The row's first cell names it (a train, a line): a row that
                # loses it goes whole (staging -115 kept "Hikari | 06:21 |
                # 09:06 | —" without its train number).
                spans.extend((a, b, " " + _LEFT_OUT + " ") for a, b in cells)
            else:
                spans.append((start, end, " " + _LEFT_OUT + " "))
        elif claim.kind == "part":
            spans.append((claim.start, claim.end, ""))
        else:
            start, end = _span_to_remove(text, claim)
            spans.append((start, end, ""))
    kept = _merged(spans)
    # Clauses of one sentence left out one after the other are one removal,
    # and its remainder is read once all of them went.
    joined: list[tuple[int, int, str]] = []
    for span in kept:
        if (
            joined
            and not span[2]
            and not joined[-1][2]
            and not text[joined[-1][1] : span[0]].strip(" \t")
        ):
            joined[-1] = (joined[-1][0], span[1], "")
            continue
        joined.append(span)
    kept = _merged(
        [
            (
                start,
                _remainder(text, start, end) if not replacement else end,
                replacement,
            )
            for start, end, replacement in joined
        ]
    )
    for start, end, replacement in reversed(kept):
        if not replacement and (start == 0 or text[start - 1] == "\n"):
            # A list item that keeps a sentence keeps its marker (staging
            # -102: the item's last sentence lost its "- ").
            marker = _ITEM.match(text[start:end])
            line_end = text.find("\n", end)
            rest = text[end : len(text) if line_end < 0 else line_end]
            if marker and rest.strip():
                start += marker.end()
        following = text[end:]
        if not replacement and (start == 0 or text[start - 1] == "\n"):
            # A sentence that opened its line: the next one opens it now.
            following = following.lstrip(" ")
        ending = text[start:end].rstrip()[-1:]
        if not replacement and ending in {".", "!", "?"}:
            # "X; Y." without "Y.": X ends the sentence now.
            before = text[:start].rstrip()
            if before.endswith(";"):
                cut = len(before) - 1
                text = text[:cut] + ending + text[cut + 1 :]
        text = text[:start] + replacement + following
        # A sentence that lost its opening clause starts with a capital again.
        before = text[:start].rstrip(" *_")
        if not replacement and (not before or before[-1] in ".!?\n"):
            after = len(text[start:]) - len(text[start:].lstrip(" *_"))
            index = start + after
            if index < len(text) and text[index].islower():
                text = text[:index] + text[index].upper() + text[index + 1 :]
    return _tidy(text)


def _tidy(text: str) -> str:
    lines: list[str] = []
    table: list[str] = []

    def flush() -> None:
        rows = [row for row in table if not _empty_row(row)]
        data = [row for row in rows if not re.fullmatch(r"\s*\|[\s:|-]*\|?\s*", row)]
        touched = any(_LEFT_OUT in row for row in table)
        if len(data) > 1 or not touched:  # a header and at least one row
            lines.extend(row.replace(_LEFT_OUT, "—") for row in rows)
        table.clear()

    for line in text.split("\n"):
        if line.lstrip().startswith("|"):
            table.append(line)
            continue
        if table:
            flush()
        cleaned = re.sub(r"[ \t]{2,}", " ", line)
        # Two clauses of one sentence left out together keep one final mark.
        cleaned = re.sub(r"(?<!\.)([.!?])\s*\.(?!\.)", r"\1", cleaned)
        cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
        cleaned = re.sub(r"([,;:])\s*([.!?])", r"\2", cleaned)
        cleaned = re.sub(r"\*\*\s*\*\*|__\s*__|\(\s*\)", "", cleaned).rstrip()
        bare = re.sub(r"^\s*(?:[-*+>]|\d{1,3}[.)])\s*", "", cleaned)
        if line.strip() and not bare.strip(" *_:,;.!?"):
            continue
        lines.append(cleaned)
    if table:
        flush()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


# --- The structure around what was left out. --------------------------------
# Staging -98: "The plan, in three legs:" stayed above the one leg left. When
# content goes, a count that announced it goes, a numbered list is numbered
# again, and an introduction or a heading left with nothing under it goes.

_ITEM = re.compile(r"^(\s*)(?:(\d{1,3})([.)])|[-*+])\s+")
_INTRO = re.compile(r":[\s*_]*$")


def _list_after(lines: list[str], index: int) -> list[int]:
    """The top-level items of the list that follows line ``index``."""
    items: list[int] = []
    base: int | None = None
    for position in range(index + 1, len(lines)):
        line = lines[position]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if _ITEM.match(line) and (base is None or indent <= base):
            base = indent if base is None else base
            items.append(position)
            continue
        if base is not None and indent > base:
            continue
        break
    return items


def _heading(line: str) -> bool:
    """A markdown heading, or a line that is only bold ("**2. Train**")."""
    return line.startswith("#") or bool(re.fullmatch(r"\*\*[^*]+\*\*\s*:?", line))


def _old_index(old: list[str], line: str) -> int:
    """Where a line was before removals; a heading that lost a parenthesis
    ("**1. Walk (10 min)**") is found by its opening words."""
    if line in old:
        return old.index(line)
    opening = line.strip()[:12]
    if _heading(line.strip()) and len(opening) == 12:
        for index, candidate in enumerate(old):
            if candidate.strip().startswith(opening):
                return index
    return -1


def _next_line(lines: list[str], index: int) -> str:
    return next((line for line in lines[index + 1 :] if line.strip()), "")


def _without_count(line: str, count: int) -> str:
    """A count of removed items goes: "The plan, in three legs:" -> "The plan:"."""
    words = [word for word, value in _NUMBER_WORDS.items() if value == count]
    numbers = "|".join(
        sorted(map(re.escape, [str(count), *words]), key=len, reverse=True)
    )
    folded = route_facts.fold(line)
    found = re.search(
        rf"(?:,\s*|\s*)\b(?:in|en|em|auf|a)\s+(?:{numbers})\s+[^\W\d_][\w-]*",
        folded,
    ) or re.search(rf"\b(?:{numbers})\s+(?=[^\W\d_])", folded)
    if found is None:
        return line
    line = re.sub(r"(?<=\S)[ \t]{2,}", " ", line[: found.start()] + line[found.end() :])
    marker = re.match(r"^[\s#>*_]*", line)
    index = marker.end() if marker else 0
    if index < len(line) and line[index].islower():
        line = line[:index] + line[index].upper() + line[index + 1 :]
    return line


def _renumber(lines: list[str], first: int) -> list[str]:
    """Numbered lists that lost items count 1, 2, 3 again."""
    result = list(lines)
    position = 0
    run_index = 0
    while position < len(result):
        match = _ITEM.match(result[position])
        if not match or not match[2]:
            position += 1
            continue
        indent = len(match[1])
        run = [position]
        cursor = position + 1
        while cursor < len(result):
            line = result[cursor]
            item = _ITEM.match(line)
            depth = len(line) - len(line.lstrip())
            if item and item[2] and depth == indent:
                run.append(cursor)
            elif line.strip() and depth <= indent:
                break
            cursor += 1
        numbers = [int(_ITEM.match(result[i])[2]) for i in run]  # type: ignore[index]
        start = first if run_index == 0 else numbers[0]
        if numbers != list(range(start, start + len(run))):
            for offset, line_index in enumerate(run):
                line = result[line_index]
                item = _ITEM.match(line)
                assert item is not None
                result[line_index] = (
                    line[: item.start(2)] + str(start + offset) + line[item.end(2) :]
                )
        run_index += 1
        position = cursor
    return result


_NUMBERED_HEADING = re.compile(
    r"^([ \t]*+(?:#{1,6}[ \t]++)?(?:\*\*|__)?[ \t]*+)(\d{1,3})([.)])(?=\s)"
)


def _heading_number(line: str) -> re.Match[str] | None:
    """The step number of a heading ("**3. Geneva Cornavin**", "### 2) Train")."""
    match = _NUMBERED_HEADING.match(line)
    if (
        match is None
        or not _heading(line.strip())
        and not line.lstrip().startswith("#")
    ):
        return None
    return match


def _renumber_headings(old: list[str], lines: list[str]) -> list[str]:
    """Numbered step headings that lost steps count 1, 2, 3 again (staging
    -102: the answer started at "**3. Geneva Cornavin -> Geneva Airport**")."""
    before = [int(m[2]) for line in old if (m := _heading_number(line))]
    found = [(i, m) for i, line in enumerate(lines) if (m := _heading_number(line))]
    if not before or not found:
        return lines
    start = before[0]
    numbers = [int(m[2]) for _, m in found]
    if numbers == list(range(start, start + len(found))):
        return lines
    result = list(lines)
    for offset, (index, match) in enumerate(found):
        line = result[index]
        result[index] = (
            line[: match.start(2)] + str(start + offset) + line[match.end(2) :]
        )
    return result


def _restructure(original: str, body: str) -> str:
    """Counts, numbers and headings that referred to what was left out."""
    old = original.split("\n")
    lines = body.split("\n")
    first_number = next(
        (int(m[2]) for line in old if (m := _ITEM.match(line)) and m[2]), 1
    )
    drop: set[int] = set()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or _ITEM.match(line) or stripped.startswith("|"):
            continue
        heading = _heading(stripped)
        old_index = _old_index(old, line)
        if not (heading or _INTRO.search(stripped)) or old_index < 0:
            continue
        following = _next_line(lines, index)
        if not following.strip() or _heading(following.strip()):
            # Nothing left under it.
            if _next_line(old, old_index) != following:
                drop.add(index)
            continue
        if heading:
            continue
        before = _list_after(old, old_index)
        after = _list_after(lines, index)
        if not before or len(after) >= len(before):
            continue
        if not after:
            drop.add(index)
            continue
        lines[index] = _without_count(line, len(before))
    lines = [line for index, line in enumerate(lines) if index not in drop]
    lines = _renumber_headings(old, _renumber(lines, first_number))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


# Markdown left by removals: "**1." with its title gone (staging -100), a
# list item with nothing in it. A line keeps its emphasis marks in pairs, and
# a line with no word left goes.


def _paired_parentheses(line: str) -> str:
    """A parenthesis a removal left open is closed where the line ends
    (staging -102: "(source : lemanexpress.com")."""
    depth = 0
    for character in line:
        if character == "(":
            depth += 1
        elif character == ")" and depth:
            depth -= 1
    text = line
    if depth:
        end = re.search(r"[\s.!?:;,]*$", text)
        cut = end.start() if end else len(text)
        text = text[:cut] + ")" * depth + text[cut:]
    return text


# --- What only referred to removed content. ---------------------------------
# Staging -110: after the timed trains were left out of a Tokyo-Kyoto answer,
# "Kodama: too slow — skip it" stayed alone under its heading, and "the
# departure-time window above is the reliable filter" pointed at nothing.

# Words that point back at content above ("the times above", "ci-dessus"),
# five languages.
_BACK = re.compile(
    r"(?<![\w-])(?:above|ci-dessus|plus haut|oben|obige\w*+|arriba|anteriores?|"
    r"sopra|precedent[ei]|mentioned earlier|listed earlier)(?![\w-])",
    re.I,
)
_CLAUSE_SPLIT = re.compile(
    r"\s[\u2013\u2014-]\s|;\s|,\s(?=(?:but|mais|aber|pero|ma)\b)"
)


def _runs(lines: list[str]) -> list[list[int]]:
    """Consecutive list items (blank lines between them allowed)."""
    runs: list[list[int]] = []
    current: list[int] = []
    for index, line in enumerate(lines):
        if _ITEM.match(line):
            current.append(index)
        elif line.strip():
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def without_orphans(original: str, text: str) -> str:
    """Leave out what only referred to removed content.

    A list that lost every item with a figure or a link keeps no item without
    one (they compared or ranked what went); a clause that points back at
    content above ("the window above") goes when content was left out.
    """
    old = original.split("\n")
    new = text.split("\n")
    factual = re.compile(r"\d|\]\(")
    drop: set[int] = set()
    for run in _runs(new):
        items = [new[i].strip() for i in run]
        source = next(
            (
                r
                for r in _runs(old)
                if all(any(old[i].strip() == item for i in r) for item in items)
            ),
            None,
        )
        if source is None or len(source) <= len(run):
            continue
        had = any(factual.search(old[i]) for i in source)
        has = any(factual.search(item) for item in items)
        if had and not has:
            drop.update(run)
    lines = [line for index, line in enumerate(new) if index not in drop]
    kept: list[str] = []
    for line in lines:
        if not _BACK.search(line):
            kept.append(line)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", line)
        rebuilt: list[str] = []
        for sentence in sentences:
            if not _BACK.search(sentence):
                rebuilt.append(sentence)
                continue
            parts = _CLAUSE_SPLIT.split(sentence)
            if len(parts) > 1:
                rest = [p for p in parts if not _BACK.search(p)]
                if rest:
                    clause = " — ".join(rest).rstrip(" ,;:")
                    rebuilt.append(clause if clause[-1:] in ".!?" else clause + ".")
        kept.append(" ".join(rebuilt))
    return _restructure(text, "\n".join(_without_announcements(old, kept)))


def _next_text(lines: list[str], index: int) -> str:
    return next((line for line in lines[index + 1 :] if line.strip()), "")


def _without_announcements(old: list[str], new: list[str]) -> list[str]:
    """A line announcing a list (it ends with ":") goes when its whole list
    was left out.

    Staging -122: a Caen - Paris answer read "..., voici les options
    réalistes:" then only a piece of advice; every option had an unsourced
    time. A line followed by text that is not a list keeps its colon
    ("To be at the airport at 9:00 from home:" before the steps).
    """
    kept: list[str] = []
    for index, line in enumerate(new):
        if line.rstrip().endswith(":") and not _ITEM.match(_next_text(new, index)):
            # Compared without spaces: a French "realistes :" (space before the
            # colon) became "realistes:".
            bare = re.sub(r"\s++", "", line)
            source = next(
                (i for i, o in enumerate(old) if re.sub(r"\s++", "", o) == bare), None
            )
            if source is not None and _ITEM.match(_next_text(old, source)):
                continue
        kept.append(line)
    return kept


def wellformed(text: str) -> str:
    """No unpaired ``**``, ``__`` or backtick on a line, no empty list item."""
    lines: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            lines.append(line)
            continue
        if fenced or line.lstrip().startswith("|"):
            lines.append(line)
            continue
        for mark in ("**", "__", "`"):
            if line.count(mark) % 2:
                line = line.replace(mark, "")
        line = _paired_parentheses(line)
        content = re.sub(r"^\s*(?:[-*+>]|\d{1,3}[.)])\s+", "", line)
        if line.strip() and not re.search(
            r"[^\W_]", re.sub(r"^\W*\d{1,3}[.)]", "", content)
        ):
            continue
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


# Words that tie a sentence to the one before it ("From there, ..." and its
# translations), five languages: an answer never opens with them (staging -113).
_LEAD_BACK = re.compile(
    r"^(?:de là|à partir de là|ensuite|puis|from there|then|after that|"
    r"von dort(?: aus)?|danach|dann|desde allí|desde alli|desde ahí|desde ahi|"
    r"luego|después|despues|da lì|da li|poi|quindi)\b[ \t]*+,?[ \t]*+",
    re.I,
)


def _opening(body: str, found: "Findings") -> str:
    """The person's own words that open a first sentence left out whole.

    Left out for a route fact, the clauses before the route (route_facts);
    left out for a value (staging -114: "To be on time for your lunch at
    Paris Gare de Lyon at 12:30, two departures ..." went for an
    unsourced time), the clauses before the first value left out, holding
    none of them.
    """
    route = found.route
    if route.opening or route.lead_start < 0 or not route.leads:
        return route.opening
    values = [*found.values, *found.elsewhere, *found.advice]
    if not any(
        _removal(body, claim)[0] <= route.lead_start < _removal(body, claim)[1]
        for claim in values
    ):
        return ""
    for end, text, _ in route.leads:
        if not any(claim.start < end for claim in values):
            return text
    return ""


def _opened(original: str, text: str, opening: str, language: str) -> str:
    """The kept answer opening as a reply, never mid-thought.

    When its first sentence went, the person's own words that opened it
    lead (route_facts.Finding.opening); a sentence that only continued the
    removed one loses its link word ("De là, les trains ..." opened an
    answer whose metro leg was left out, staging -113).
    """
    first = original.lstrip().split("\n", 1)[0]
    if not text or text.lstrip().startswith(first.strip()):
        return text
    lead = _LEAD_BACK.match(text)
    if lead and text[lead.end() :][:1].isalpha():
        rest = text[lead.end() :]
        text = rest[0].upper() + rest[1:]
    if opening and opening not in " ".join(text.split()):
        colon = " :" if language == "fr" else ":"
        text = opening[0].upper() + opening[1:] + colon + "\n\n" + text
    return text


# Separators of a list of values ("4h 30m, 4h 45m, or 9h 15m"), five languages.
_LIST_GAP = re.compile(
    r"^[ \t]*+[,/;]?+[ \t]*+(?:(?:or|and|ou|et|oder|und|o|y|e|u)[ \t]++)?+$", re.I
)
OUTLIER = 1.75
# Words that say the values of a line are journey times, five languages.
_DURATION_CUE = re.compile(
    r"(?<![\w-])(?:journey|journeys|travel|trip|ride|duration|takes?|took|"
    r"dure\w*+|durée\w*+|trajets?|fahrzeit\w*+|reisezeit\w*+|dauer\w*+|"
    r"duraci\w*+|tarda\w*+|viaje|durata|impiega\w*+|viaggio)(?![\w-])",
    re.I,
)
_TRAILING_UNIT = re.compile(r"[ \t]?+(?:min\w*+|mn|m)(?![\w-])", re.I)


def _durations(line: str) -> list[Quantity]:
    """The journey times of a line: durations, and "4h 30" when the line
    says it gives journey times."""
    found = quantities(line, generous=bool(_DURATION_CUE.search(line)))
    items: dict[int, Quantity] = {}
    for q in found:
        if q.kind == "duration":
            unit = _TRAILING_UNIT.match(line, q.end)
            end = unit.end() if unit else q.end
            items.setdefault(q.start, Quantity(q.start, end, q.kind, q.value, q.text))
    return [items[start] for start in sorted(items)]


def outliers_left_out(text: str) -> str:
    """A duration listed with others that is far longer than them goes.

    Staging -114: "journey times around 4h 30m, 4h 45m, or 9h 15m" kept a
    slow connection read in a snippet next to the route's usual times. In a
    list of three durations or more, one longer than OUTLIER times the
    middle of the others is no typical time and leaves the list.
    """
    lines: list[str] = []
    for line in text.split("\n"):
        found = _durations(line)
        cuts: list[tuple[int, int]] = []
        for index, item in enumerate(found):
            others = sorted(q.value for q in found if q is not item)
            if index == 0 or len(others) < 2:
                continue
            middle = others[len(others) // 2]
            previous = found[index - 1]
            if item.value > OUTLIER * middle and _LIST_GAP.match(
                line[previous.end : item.start]
            ):
                cuts.append((previous.end, item.end))
        for start, end in reversed(cuts):
            line = line[:start] + line[end:]
        lines.append(line)
    return "\n".join(lines)


# --- Times worked out from other values; the time to arrive by. -------------
# Staging -126 (2026-11-19, seven trips on a phone): a Munich - Berlin answer
# lost its unsourced "4h to 4h30m" but kept "to arrive by 14:00, depart no
# later than about 09:30", worked out from it; a Madrid - Barcelona answer
# for 13:00 offered a train arriving at 16:44 without saying it was too late.
# Code reads the times; the model writes (owner principle T8).

# How far a time may sit from "other time +/- duration" and still be read as
# worked out from them ("about 09:30" for 14:00 - 4 h 29).
DERIVED_MINUTES = 10
# How far a time may sit from "sourced time +/- sourced duration" to be that
# arithmetic ("about 09:30" for 14:00 - 4 h 29).
SUM_MINUTES = 5


def _clock_text(minutes: float) -> str:
    hours, rest = divmod(int(round(minutes)) % 1440, 60)
    return f"{hours:02d}:{rest:02d}"


def _gap(first: float, second: float) -> float:
    """Minutes between two times of day, around midnight too."""
    gap = abs(first - second) % 1440
    return min(gap, 1440 - gap)


def _any_clock(text: str) -> bool:
    return bool(spoken_times(text)) or any(
        q.kind == "clock" for q in quantities(text, generous=True)
    )


def _clock_sentences(texts: list[str]) -> list[set[float]]:
    """The clock times of each source sentence (a timetable row) holding two
    or more."""
    found: list[set[float]] = []
    for text in texts:
        for sentence in _EVIDENCE_SENTENCE.split(text):
            clocks = {
                q.value
                for q in quantities(sentence, generous=True)
                if q.kind == "clock" and q.value == q.value
            }
            if len(clocks) > 1:
                found.append(clocks)
    return found


def derived_clocks(
    body: str,
    claims: list[Quantity],
    read: list[Quantity],
    durations: list[float],
    pairs: list[set[float]],
    owned: list[float],
) -> list[Quantity]:
    """The clock times of an answer worked out from a duration left out.

    A time that is another time of its sentence (the person's deadline, a
    departure) plus or minus a duration no page gives is no fact, even when a
    page happens to give the same time for something else. It stays when a
    page gives it together with that other time (a timetable row), and a time
    the person gave is theirs.
    """
    if not durations:
        return []

    def together(value: float, other: float) -> bool:
        return any(
            any(_gap(value, item) < 0.5 for item in clocks)
            and any(_gap(other, item) < 0.5 for item in clocks)
            for clocks in pairs
        )

    found: list[Quantity] = []
    for claim in claims:
        if claim.kind != "clock":
            continue
        left, right = _sentence(body, claim.start, claim.end)
        others = [
            q.value
            for q in read
            if q.kind == "clock"
            and q.value == q.value
            and left <= q.start < right
            and not claim.start <= q.start < claim.end
        ]
        values = _range_values(claim)
        for value in values:
            if value != value or any(_gap(value, own) < 0.5 for own in owned):
                continue
            anchors = [*others, *(item for item in values if item != value)]
            if any(together(value, anchor) for anchor in anchors):
                continue
            if any(
                _gap(value, anchor + sign * duration) <= DERIVED_MINUTES
                for anchor in anchors
                for duration in durations
                for sign in (1, -1)
            ):
                found.append(claim)
                break
    return found


# Words of the person's request that make a time the time to arrive by
# (folded, five languages): anywhere in its sentence ...
_ARRIVAL_NEED = re.compile(
    r"(?<![\w-])(?:arriv\w*+|be (?:at|in|there)|get (?:to|there)|reach\w*+|"
    r"flight|plane|appointment|meeting|lunch|dinner|interview|"
    r"etre (?:a|au|aux|en|la|sur)|rendez-vous|rdv|vol|avion|reunion|dejeuner|"
    r"diner|entretien|estar (?:en|a|alli|ahi)|llegar|llego|llegada|cita|vuelo|"
    r"comida|almuerzo|cena|ankommen|ankomme|ankunft|sein|termin|flug|treffen|"
    r"essere (?:a|in|li|la|al|alla)|arrivare|arrivo|appuntamento|volo|aereo|"
    r"riunione|pranzo)(?![\w-])"
)
# ... or right before the time ("by 2pm", "avant 9h", "bis 14 Uhr").
_BY = re.compile(
    r"(?<![\w-])(?:by|before|no later than|at the latest|until|avant|pour|"
    r"d.ici|au plus tard|bis|vor|spatestens|antes de|para|a mas tardar|entro|"
    r"prima|al piu tardi)(?:[ \t]++(?:the|a|la|las|le|les|l|um|alle|delle|"
    r"dalle|de|des|h))*+[ \t]*+$"
)
# A time the person leaves at is no time to arrive by ("leave after 8").
_LEAVING = re.compile(
    r"(?<![\w-])(?:leav\w*+|depart\w*+|after|from|starting|partir|pars|part|"
    r"partant|apres|des|a partir de|salir|salgo|saliendo|despues de|desde|ab|"
    r"abfahr\w*+|losfahr\w*+|partire|parto|partenza|dopo|dalle)"
    r"(?:[ \t]++(?:at|a|las|la|les|um|alle|le|the|around|about|vers|sobre|"
    r"hacia|gegen|verso|circa|environ|de|du|del|d))*+[ \t]*+$"
)
_REQUEST_SENTENCE = re.compile(r"(?:[^\n.!?]|[.!?](?!\s|$))+")


def arrival_deadline(text: str) -> float | None:
    """The one time a message says the person must arrive by (minutes)."""
    folded = route_facts.fold(text)
    found: set[float] = set()
    for sentence in _REQUEST_SENTENCE.finditer(folded):
        start, end = sentence.span()
        need = bool(_ARRIVAL_NEED.search(folded, start, end))
        for q in quantities(text[start:end], generous=True):
            if q.kind != "clock" or q.value != q.value:
                continue
            before = folded[max(start, start + q.start - 40) : start + q.start]
            if _LEAVING.search(before):
                continue
            if need or _BY.search(before):
                found.add(q.value % 1440)
        if need:
            # Times in words ("midi trente") have no position to read.
            found.update(value % 1440 for value in spoken_times(text[start:end]))
    return found.pop() if len(found) == 1 else None


# Words that make a time of an answer an arrival ("llega a las 16:44",
# "arrives 16:44", "13:42 -> 16:44", "an 16:44") or a departure.
_ARRIVES = re.compile(
    r"(?<![\w-])(?:arriv\w*+|llega\w*+|ankunft|ankommt|ankommen|kommt|"
    r"reach\w*+|gets? in)(?![\w-])|→|->|(?<![\w-])an[ \t]*+$"
)
_DEPARTS = re.compile(
    r"(?<![\w-])(?:depart\w*+|leav\w*+|sale|salen|salida|saliendo|part\w*+|"
    r"abfahr\w*+|fahrt|ab)(?![\w-])"
)
# An answer that already says an option is too late, or that it is the
# evening before: no check needed.
_SAID_LATE = re.compile(
    r"too late|trop tard|demasiado tarde|zu spat|troppo tardi|after the "
    r"requested|apres l.heure|despues de la hora|nach der gewunschten|"
    r"dopo l.ora|tonight|this evening|the (?:day|evening|night) before|"
    r"ce soir|la veille|esta noche|la vispera|la noche anterior|heute abend|"
    r"am vorabend|stasera|la sera prima|la vigilia"
)
_ARRIVAL_COLUMN = re.compile(r"arriv|llegada|ankunft|(?<!\w)(?:arr|an)(?!\w)")
_DEPARTURE_COLUMN = re.compile(
    r"depart|salida|abfahrt|partenza|(?<!\w)(?:dep|ab)(?!\w)"
)


def _header_cells(body: str, index: int) -> list[str]:
    """The header labels (folded) of the table holding the row at ``index``."""
    start, _ = _line_bounds(body, index)
    while start > 0:
        start, _ = _line_bounds(body, start - 1)
        if not body[start:].lstrip().startswith("|"):
            return []
        if route_facts.table_header(body, start):
            return [
                route_facts.fold(body[a:b]).strip() for a, b in _row_cells(body, start)
            ]
    return []


def _late_row(
    body: str, claims: list[Quantity], deadline: float, owned: list[float]
) -> list[tuple[int, int, str]]:
    """The rows of a timetable whose arrival column is after the deadline."""
    found: list[tuple[int, int, str]] = []
    done: set[int] = set()
    for claim in claims:
        line_start, line_end = _line_bounds(body, claim.start)
        if line_start in done or not body[line_start:].lstrip().startswith("|"):
            continue
        done.add(line_start)
        header = _header_cells(body, line_start)
        cells = _row_cells(body, line_start)
        if not header or len(header) != len(cells):
            continue
        columns = list(zip(cells, header, strict=True))
        arrivals = _column(claims, columns, _ARRIVAL_COLUMN, "clock")
        departures = _column(claims, columns, _DEPARTURE_COLUMN, "clock")
        departure = departures[0] if len(departures) == 1 else None
        arrival: float | None = None
        if len(arrivals) == 1:
            arrival = arrivals[0].value
        elif not arrivals and departure is not None:
            took = _column(claims, columns, _DURATION_COLUMN, "duration")
            if len(took) == 1:
                arrival = departure.value + took[0].value
        if arrival is None or any(_gap(arrival, own) < 0.5 for own in owned):
            continue
        if _is_late(departure.value if departure else None, arrival, deadline):
            label = (
                f"{departure.text} → {_clock_text(arrival)}"
                if departure
                else _clock_text(arrival)
            )
            found.append((cells[0][0], line_end, label))
    return found


_DURATION_COLUMN = re.compile(
    r"duration|duree|dauer|fahrzeit|reisezeit|duracion|durata|travel time|"
    r"temps de trajet|tiempo de viaje"
)


def _column(
    claims: list[Quantity],
    columns: list[tuple[tuple[int, int], str]],
    label: re.Pattern[str],
    kind: str,
) -> list[Quantity]:
    """The values of one kind in the row cells whose header matches ``label``."""
    return [
        claim
        for (start, end), header in columns
        if label.search(header)
        for claim in claims
        if start <= claim.start < end
        and claim.kind == kind
        and claim.value == claim.value
    ]


def _is_late(departure: float | None, arrival: float, deadline: float) -> bool:
    """Arrives after the deadline on the trip's day (an arrival before its
    departure is overnight: not read)."""
    if departure is not None and arrival < departure:
        return False
    return arrival > deadline + 0.5 and arrival < 1440


def late_departures(
    body: str,
    claims: list[Quantity],
    deadline: float,
    owned: list[float],
    computed: list[str] | None = None,
) -> list[tuple[int, int, str]]:
    """Departures of an answer that arrive after the time the person gave.

    Read per sentence (a table row): a time after an arrival word is the
    arrival, the time before it the departure; without an arrival, one
    departure and one duration give it. A sentence or row whose every option
    arrives after the deadline goes (start, end, "13:42 -> 16:44"); a sentence
    that already says so, or that is the evening before, stays.
    """
    computed_text = "\n".join(computed or [])
    folded = route_facts.fold(body)
    found = _late_row(body, claims, deadline, owned)
    groups: dict[tuple[int, int], list[Quantity]] = {}
    for claim in claims:
        line_start, _ = _line_bounds(body, claim.start)
        if body[line_start:].lstrip().startswith("|"):
            continue
        groups.setdefault(_sentence(body, claim.start, claim.end), []).append(claim)
    for (left, right), group in groups.items():
        line_start, line_end = _line_bounds(body, left)
        line = body[line_start:line_end].strip()
        if len(line) > 10 and line in computed_text:
            continue
        if _SAID_LATE.search(folded, left, right):
            continue
        clocks = [
            c
            for c in group
            if c.kind == "clock"
            and c.value == c.value
            and not any(_gap(v, own) < 0.5 for v in _range_values(c) for own in owned)
        ]
        durations = [c for c in group if c.kind == "duration"]
        options: list[tuple[float | None, float, str]] = []
        previous_end = left
        previous: Quantity | None = None
        previous_arrival = False
        for clock in sorted(clocks, key=lambda c: c.start):
            segment = folded[previous_end : clock.start]
            if isinstance(clock, _Range):
                span = clock.value - clock.low
                if any(m in clock.text for m in ("→", "->")) or any(
                    abs(d.value - span) <= 5 for d in durations
                ):
                    options.append(
                        (
                            clock.low,
                            clock.value,
                            f"{_clock_text(clock.low)} → {_clock_text(clock.value)}",
                        )
                    )
                previous, previous_arrival = None, True
            elif _ARRIVES.search(segment):
                departure = (
                    previous.value
                    if previous is not None and not previous_arrival
                    else None
                )
                label = (
                    f"{previous.text} → {clock.text}"
                    if departure is not None and previous is not None
                    else clock.text
                )
                options.append((departure, clock.value, label))
                previous, previous_arrival = clock, True
            else:
                previous, previous_arrival = clock, False
            previous_end = clock.end
        if not options:
            leaving = [
                c
                for c in clocks
                if not isinstance(c, _Range)
                and _DEPARTS.search(folded[max(left, c.start - 40) : c.start])
            ]
            if len(clocks) == 1 and len(leaving) == 1 and len(durations) == 1:
                start = leaving[0]
                arrival = start.value + durations[0].value
                options.append(
                    (start.value, arrival, f"{start.text} → {_clock_text(arrival)}")
                )
        if options and all(
            _is_late(departure, arrival, deadline) for departure, arrival, _ in options
        ):
            found += [(left, right, label) for _, _, label in options]
    return found


@dataclass
class Findings:
    """What a travel answer states that no source, computation or person gave."""

    values: list[Quantity]
    route: route_facts.Finding
    # Times the sources give for another day: the start of the claim, the day.
    dated: dict[int, str] = field(default_factory=dict)
    # Values a source gives for another day than the trip's, written with
    # that day: sourced, but no use for this trip; left out without a note.
    elsewhere: list[Quantity] = field(default_factory=list)
    # Values of the opposite direction: never a fact of this trip.
    reverse: list[Quantity] = field(default_factory=list)
    # Unsourced times of advice naming no service (_advice): a target the
    # person could aim for, not a fact to rely on; left out without a note.
    advice: list[Quantity] = field(default_factory=list)
    # Departures that arrive after the time the person must be there by
    # (start, end of their sentence or row, "13:42 -> 16:44"), and that time.
    late: list[tuple[int, int, str]] = field(default_factory=list)
    deadline: str = ""
    # A journey planner computed itineraries this turn: the note never says
    # that Atlas computes no route (staging -128).
    planned: bool = False
    # A journey planner is configured: without a plan this turn, the note
    # says that none was computed, never that Atlas computes no route
    # (staging -129, Sydney).
    planner: bool = False
    # Each value left out, quoted from the words of the answer that state it
    # (start of the value: quote); never the bare value (staging -129 listed
    # "13 minutes").
    quotes: dict[int, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(
            self.values
            or self.route.spans
            or self.elsewhere
            or self.reverse
            or self.advice
            or self.late
        )

    def __len__(self) -> int:
        return (
            len(self.values)
            + len(self.route.spans)
            + len(self.elsewhere)
            + len(self.reverse)
            + len(self.advice)
            + len(self.late)
        )

    def advice_only(self) -> bool:
        """Only advice goes: no note to write, nothing to rewrite."""
        return bool(self.advice) and len(self) == len(self.advice)

    def late_labels(self) -> list[str]:
        return list(dict.fromkeys(label for _, _, label in self.late if label))

    def labels(self) -> list[str]:
        return list(
            dict.fromkeys(
                " ".join(text.split())
                for text in (
                    *(
                        self.quotes.get(c.start, c.text)
                        + (f" ({self.dated[c.start]})" if c.start in self.dated else "")
                        for c in self.values
                    ),
                    *self.route.labels,
                )
            )
        )

    def note(self, language: str) -> str:
        """The one closing note: what was left out, and where to check."""
        late = ""
        if self.late:
            listed = self.late_labels()
            late = wording("late", language).format(
                deadline=self.deadline,
                values=", ".join(listed[:6]) + (", …" if len(listed) > 6 else ""),
            )
        if not self.labels():
            return late
        if late:
            return self._unverified(language) + " " + late
        return self._unverified(language)

    def _unverified(self, language: str) -> str:
        key = "route" if self.route.route else "note"
        if (
            not self.values
            and self.route.disputed
            and self.route.disputed
            == sum(1 for _, _, labels in self.route.spans if labels)
        ):
            key = "disputed"
        text = wording(key, language)
        if self.planned:
            old, new = _PLANNED.get(language, _PLANNED["en"])
            text = text.replace(old, "" if key == "disputed" else new)
        elif self.planner:
            old, _ = _PLANNED.get(language, _PLANNED["en"])
            text = text.replace(old, _NO_PLAN.get(language, _NO_PLAN["en"]))
        return text.format(values=self.listed()).replace("….", "…")

    def listed(self) -> str:
        values = self.labels()
        return ", ".join(values[:6]) + (", …" if len(values) > 6 else "")


def _planner_enabled() -> bool:
    from services.orchestrator import transit_planner

    return transit_planner.enabled()


def _empty_row(row: str) -> bool:
    """A row that lost values here and keeps at most its label."""
    cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
    if _LEFT_OUT not in cells:
        return False
    filled = [cell for cell in cells if cell not in {"", "—", "-", _LEFT_OUT}]
    return len(filled) <= 1


@dataclass
class Turn:
    """The travel facts of one turn: what was read, computed and asked."""

    active: bool = False
    position: Position | None = field(default=None, repr=False)
    place: str = field(default="", repr=False)
    # The starting point's names in the languages the map service gave.
    place_names: list[str] = field(default_factory=list, repr=False)
    evidence: Grounding = field(default_factory=Grounding)
    computed: list[str] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)
    hold: Callable[[], bool] | None = None
    # The URLs read in this turn (D7) and the answer language, set by the pipeline.
    retrieved: Callable[[], list[str]] = field(default=lambda: [], repr=False)
    language: str = "en"
    zone: str = "UTC"
    journey_offered: bool = False
    journey_called: bool = False
    # A journey planner computed itineraries this turn (transit_planner).
    planned: bool = False
    # The last plan computed with itineraries, as code rendered it, and the
    # clock times of its legs: the answer gives this block unaltered
    # (transit_planner.planned_answer), never a paraphrase of it.
    plan: str = ""
    plan_times: set[str] = field(default_factory=set)
    # Each itinerary's departure and arrival (HH:MM): the model's sums of the
    # plan (a margin before the deadline, a later option) are worked out from
    # them.
    plan_ends: list[tuple[str, str]] = field(default_factory=list)
    # The places the plan's first legs reach from its origin, the origin as
    # the plan names it, and whether it is the device position: a caveat that
    # the walk from the start is not covered is contradicted by the plan.
    plan_starts: list[str] = field(default_factory=list)
    plan_origin: str = ""
    plan_from_device: bool = False
    # Every itinerary of the plan starts with a walk from its origin.
    plan_walks_first: bool = False
    # The lines and trains each option of the plan rides, in its order.
    plan_services: list[list[str]] = field(default_factory=list)
    # The computed plan when it is doubtful (night only, far too slow): a
    # fallback after what the pages read say (transit_planner.doubtful_answer),
    # why it is doubtful, and the pages code read about the direct services.
    doubtful: str = ""
    doubt_reason: str = ""
    direct_pages: list[str] = field(default_factory=list)
    results: int = 0
    started: float = field(default_factory=time.monotonic, repr=False)
    held: bool = False
    marked: int = 0
    # The texts read, each with the page it came from (travel_rebuild).
    linked: list[tuple[str, str]] = field(default_factory=list, repr=False)
    # Pages read that are about another place than the trip's (page keys,
    # set by travel_rebuild): their texts support none of its facts.
    excluded: set[str] = field(default_factory=set, repr=False)
    # Durations an earlier check of this turn left out (minutes): a rewrite
    # that keeps a time worked out from one of them keeps no fact.
    dropped: list[float] = field(default_factory=list, repr=False)
    # The clock of the turn's journey plan (an aware datetime, set by
    # transit_planner): every later check reads the person's days ("tomorrow",
    # "on Friday") on it, as the plan did. Staging -141 posttests: a check run
    # after midnight read a Spanish "Friday" as the next week's Friday and cut
    # the opening that dated the plan's Friday.
    clock: Any = field(default=None, repr=False)

    def deadline(self) -> float | None:
        """When the person must be at the destination (minutes), from the last
        message of theirs that gives a time; None when none or unclear."""
        for text in reversed(self.asked):
            found = arrival_deadline(text)
            if found is not None or _any_clock(text):
                return found
        return None

    def passages(self, cited: frozenset[str] | None = None) -> list[str]:
        """The texts read this turn, without those of the pages about another
        place; with ``cited``, only those of these pages (page keys)."""
        if not self.excluded and cited is None:
            return list(self.evidence.evidence)
        pages: dict[str, set[str]] = {}
        for url, text in self.linked:
            pages.setdefault(_text_key(text), set()).add(page_key(url))
        kept: list[str] = []
        for text in self.evidence.evidence:
            found = pages.get(_text_key(text), set()) - {""}
            if found and found <= self.excluded:
                continue
            if cited is not None and not found & cited:
                continue
            kept.append(text)
        return kept

    def pages(self) -> list[tuple[str, str]]:
        """The texts read with their page, without the pages about another place."""
        return [(u, t) for u, t in self.linked if page_key(u) not in self.excluded]

    def places(self, answer: str) -> frozenset[str]:
        """The words of the person's messages, starting point and answer."""
        names = _name_words(" ".join(self.place_names))
        return frozenset(
            word
            for word in [
                *names,
                *normalized(" ".join((*self.asked, self.place, answer))).split(),
            ]
            if len(word) >= 3
        )

    def elapsed(self) -> float:
        """Seconds since the turn started (its checks were set up)."""
        return time.monotonic() - self.started

    def located(
        self,
        *,
        place: str = "",
        place_names: list[str] | None = None,
        position: Position | None = None,
    ) -> None:
        """The person's position or stated place for this turn (memory only)."""
        self.place = place or self.place
        if place_names is not None:
            self.place_names = place_names
        elif place:
            self.place_names = []
        self.position = position or self.position
        self.activate()

    def activate(self) -> None:
        self.active = True
        if not self.held and self.hold is not None:
            self.held = self.hold()

    def collect(self, value: object) -> None:
        if self.active:
            # A forum or social post is no source of a trip's facts (staging
            # -114: "Acela runs Monday-Friday" from a Reddit thread).
            value = redirects.without_forums(value)
            self.evidence.collect(value)
            self._link(value, "")

    def _link(self, value: object, url: str) -> None:
        """Source texts with their page, the same fields grounding reads."""
        if isinstance(value, dict):
            if value.get("error"):
                return
            found = value.get("link") or value.get("url")
            url = found if isinstance(found, str) else url
            text = "\n".join(
                item
                for key, item in value.items()
                if key in {"title", "snippet", "text", "content"}
                and isinstance(item, str)
            )
            if text.strip() and sum(len(t) for _, t in self.linked) < 128_000:
                self.linked.append((url, text[:20_000]))
            for key in ("data", "results", "passages"):
                if key in value:
                    self._link(value[key], url)
        elif isinstance(value, list):
            for row in value:
                self._link(row, url)

    def collect_text(self, text: str) -> None:
        if self.active:
            self.evidence.add(text)

    def compute(self, text: str) -> None:
        """Values written by code this turn (journey plan, straight-line distance)."""
        self.computed.append(text)

    def trip_dates(self, zone: str = "", now: Any = None) -> list[str]:
        """The days the person named for the trip (ISO), in their zone (or
        ``zone``: where their device is, for the journey planner), relative to
        ``now`` when the caller has its own clock, else the plan's."""
        return user_dates(self.asked, zone or self.zone, now or self.clock)

    def today(self) -> str:
        """Today in the person's zone (ISO), on the plan's clock when there is
        one."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        from services.orchestrator import datecalc

        instant = self.clock or datecalc.utc_now()
        try:
            return instant.astimezone(ZoneInfo(self.zone)).date().isoformat()
        except (ZoneInfoNotFoundError, ValueError):
            return instant.astimezone(ZoneInfo("UTC")).date().isoformat()

    def plan_gaps(self) -> list[float]:
        """Minutes the computed plan gives by arithmetic: each itinerary's
        duration, its margin before the time the person must be there by (and
        from its departure), and the gaps between the options' departures and
        between their arrivals (staging -132: a French "12:17, only 13 minutes
        before your lunch" for 12:30 was listed as not verified)."""

        def minutes(clock: str) -> float:
            return int(clock[:2]) * 60 + int(clock[3:5])

        ends = [
            (minutes(a), minutes(b))
            for a, b in self.plan_ends
            if re.fullmatch(r"\d\d:\d\d", a) and re.fullmatch(r"\d\d:\d\d", b)
        ]
        deadline = self.deadline()
        gaps: set[float] = set()
        for depart, arrive in ends:
            gaps.add((arrive - depart) % 1440)
            if deadline is not None:
                # An option arriving after the deadline says by how much.
                gaps.update(
                    {
                        (deadline - arrive) % 1440,
                        (arrive - deadline) % 1440,
                        (deadline - depart) % 1440,
                    }
                )
        for column in (0, 1):
            values = [pair[column] for pair in ends]
            gaps.update(_gap(a, b) for a in values for b in values if a != b)
        return sorted(gap for gap in gaps if gap > 0)

    def other_days(self) -> dict[float, set[str]]:
        """Clock times the sources give only next to another date than the trip's.

        Read per sentence of a source (a timetable row): a time in a sentence
        that names dates, none of them a day the person named, is a time of
        those dates (staging -102: times of 23.11.26 for a trip tomorrow).
        """
        asked = set(self.trip_dates())
        if not asked:
            return {}
        other: dict[float, set[str]] = {}
        same: set[float] = set()
        for text in self.passages():
            for sentence in _EVIDENCE_SENTENCE.split(text):
                clocks = [
                    q.value
                    for q in quantities(sentence, generous=True)
                    if q.kind == "clock"
                ]
                if not clocks:
                    continue
                dates = other_dates(sentence, asked)
                if dates:
                    for value in clocks:
                        other.setdefault(value, set()).update(dates)
                else:
                    same.update(clocks)
        return {value: dates for value, dates in other.items() if value not in same}

    def _read_values(
        self, texts: list[str] | None, places: frozenset[str]
    ) -> list[tuple[str, Quantity]]:
        """The values of the sources (each with its sentence), the computed
        ones and the person's; a duration or distance of another leg left out."""
        found: list[tuple[str, Quantity]] = []
        for text in self.passages() if texts is None else texts:
            for sentence in _LEG_SENTENCE.split(text):
                values = quantities(sentence, generous=True)
                if not values:
                    continue
                elsewhere = another_leg(sentence, places)
                found += [
                    (sentence, q)
                    for q in values
                    if not (elsewhere and q.kind in {"duration", "distance", "price"})
                ]
        for text in (*self.computed, *self.asked):
            found += [(text, q) for q in quantities(text, generous=True)]
        return found

    def known(
        self,
        other: dict[float, set[str]] | None = None,
        *,
        texts: list[str] | None = None,
        places: frozenset[str] = frozenset(),
    ) -> dict[Kind, list[Quantity]]:
        values: dict[Kind, list[Quantity]] = {
            "duration": [],
            "distance": [],
            "clock": [],
            "price": [],
        }
        for _, quantity in self._read_values(texts, places):
            values[quantity.kind].append(quantity)
            if isinstance(quantity, _Range):
                values[quantity.kind].append(
                    replace(quantity, value=quantity.low, low=quantity.low)
                )
        for text in self.asked:
            values["clock"].extend(
                Quantity(0, 0, "clock", value, "") for value in spoken_times(text)
            )
        other = self.other_days() if other is None else other
        if other:
            # A time the sources give only for another day is not known for
            # the trip's day; the answer may still give it with its date.
            owned = {
                q.value
                for text in (*self.computed, *self.asked)
                for q in quantities(text, generous=True)
                if q.kind == "clock"
            }
            values["clock"] = [
                quantity
                for quantity in values["clock"]
                if quantity.value in owned
                or not any(_same("clock", quantity.value, o) for o in other)
            ]
        return values

    def stated_durations(
        self,
        *,
        texts: list[str] | None = None,
        places: frozenset[str] = frozenset(),
    ) -> list[float]:
        """Durations the sources write as durations ("4 h 02 min", "Fahrzeit 4h29"),
        not clock times read as durations too."""
        found = self._read_values(texts, places)
        clocks = {
            (id(sentence), q.start, q.end) for sentence, q in found if q.kind == "clock"
        }
        return [
            q.value
            for sentence, q in found
            if q.kind == "duration" and (id(sentence), q.start, q.end) not in clocks
        ]

    def owned_values(self, answer: str) -> str:
        """The travel values of ``answer``, written as the travel check reads them.

        The unsourced details review runs first and treats this text as the
        person's own words, so a travel value is checked (and rewritten) once,
        here, never by both reviews.
        """
        parts: list[str] = []
        for quantity in quantities(answer):
            parts.append(quantity.text)
            parts.extend(re.findall(r"\d+(?:[.,]\d+)?", quantity.text))
        # "demain", "tomorrow", "Freitag": the dates the person named are theirs.
        parts.extend(self.trip_dates())
        # Lines and stops of route sentences: checked here, together.
        parts.extend(route_facts.mentions(answer, _masked(answer)))
        return " ".join(parts)

    def findings(self, body: str) -> Findings:
        """The unsourced travel values and route facts of an answer body."""
        read = quantities(body)
        claims = _ranges(body, read)
        claims = [
            claim
            for claim in claims
            if not _MARGIN.search(body[slice(*_clause(body, claim.start, claim.end))])
            # A table's header row labels its columns ("Arrival (before
            # 10:00)"): never a claim.
            and not route_facts.table_header(body, claim.start)
        ]
        other = self.other_days()
        places = self.places(body)
        known = self.known(other, places=places)
        sourced_clocks = [quantity.value for quantity in known["clock"]]
        page_clocks = [
            q.value
            for text in (*self.passages(), *self.computed)
            for q in quantities(text, generous=True)
            if q.kind == "clock"
        ]
        stated = self.stated_durations(places=places)
        read_pages = {page_key(url) for url, _ in self.linked if url}
        scoped: dict[
            frozenset[str], tuple[dict[Kind, list[Quantity]], list[float]]
        ] = {}
        today = min(self.trip_dates(), default="")

        def cited(claim: Quantity) -> tuple[dict[Kind, list[Quantity]], list[float]]:
            """The values of the pages read that the claim's sentence cites:
            a value cited to a page comes from that page (staging -118: "takes
            7 hours" cited to a page that gave it for another leg)."""
            left, right = _sentence(body, claim.start, claim.end)
            urls = (
                frozenset(
                    page_key(a or b) for a, b in _CITATION.findall(body[left:right])
                )
                & read_pages
            )
            if not urls:
                return known, stated
            if urls not in scoped:
                texts = self.passages(urls)
                scoped[urls] = (
                    self.known(other, texts=texts, places=places),
                    self.stated_durations(texts=texts, places=places),
                )
            return scoped[urls]

        dated: dict[int, str] = {}

        def other_day(claim: Quantity) -> bool:
            """A time the sources give for another day, written with that day."""
            if claim.kind != "clock" or not other:
                return False
            start, end = _line_bounds(body, claim.start)
            written = named_dates(body[start:end], today)
            days = [
                dates
                for value in _range_values(claim)
                for item, dates in other.items()
                if _same("clock", value, item)
            ]
            if len(days) < len(_range_values(claim)):
                return False
            if all(dates & written for dates in days):
                return True
            dated[claim.start] = min(days[0])
            return False

        def matches(
            claim: Quantity,
            value: float,
            known: dict[Kind, list[Quantity]],
            stated: list[float],
        ) -> bool:
            if value != value:  # a placeholder ("12 h 2x") states no time
                return False
            if claim.kind == "price":
                return any(_same_price(claim, item) for item in known[claim.kind])
            if any(_same(claim.kind, value, item.value) for item in known[claim.kind]):
                return True
            if claim.kind == "distance":
                return False
            # One fact, two spellings: "4h30" written for a trip a source
            # gives as "4 h 30 min"; "about 4 h" for "4 h 02 min".
            if claim.kind == "clock" and any(
                abs(value - item) < 0.5 for item in stated
            ):
                return True
            return any(_rounded(value, item) for item in stated)

        plan_gaps = self.plan_gaps()

        def supported(claim: Quantity) -> bool:
            values, durations = cited(claim)
            if all(
                matches(claim, value, values, durations)
                for value in _range_values(claim)
            ):
                return True
            if claim.kind == "duration" and all(
                any(abs(value - gap) <= 1 for gap in plan_gaps)
                for value in _range_values(claim)
            ):
                # Arithmetic on the computed plan's own times.
                return True
            if other_day(claim):
                return True
            if claim.kind == "clock":
                return worked_out(claim)
            if claim.kind != "duration":
                return False
            # A duration between two sourced times of the same line ("10:05 to
            # 12:00, 1 h 55 min") is arithmetic on sourced values. The time
            # the person must arrive by is no source of a trip's duration
            # (staging -126: "by 14:00 ... about 09:30" sourced "4h30").
            start, end = _line_bounds(body, claim.start)
            times = [
                q.value
                for q in quantities(body[start:end])
                if q.kind == "clock"
                and any(_same("clock", q.value, value) for value in sourced_clocks)
                and any(_same("clock", q.value, value) for value in page_clocks)
            ]
            return any(
                abs(((later - earlier) % 1440) - claim.value) <= 1
                for earlier in times
                for later in times
                if later != earlier
            )

        # The durations the answer states that a page gives.
        kept_durations: list[float] = []

        def worked_out(claim: Quantity) -> bool:
            """A time that is another time of its sentence, the person's or a
            page's, plus or minus a duration the answer states and a page
            gives ("4 h 30 min ... by 14:00, leave by 09:30"): arithmetic on
            kept, sourced values."""
            left, right = _sentence(body, claim.start, claim.end)
            anchors = [
                q.value
                for q in quantities(body[left:right])
                if q.kind == "clock"
                and q.value == q.value
                and not claim.start <= left + q.start < claim.end
                and any(_gap(q.value, item) < 0.5 for item in sourced_clocks)
            ]
            return bool(anchors) and all(
                any(
                    _gap(value, anchor + sign * duration) <= SUM_MINUTES
                    for anchor in anchors
                    for duration in kept_durations
                    for sign in (1, -1)
                )
                for value in _range_values(claim)
            )

        def another_day(claim: Quantity) -> bool:
            """Written after a date of the sentence that is not the trip's
            ("on 2026-11-16, a train took 10h 34m" for a trip on the 19th,
            staging -114)."""
            if not today:
                return False
            left, _ = _sentence(body, claim.start, claim.end)
            before = named_dates(body[left : claim.start], today)
            return bool(before) and not before & set(self.trip_dates())

        elsewhere = [claim for claim in claims if another_day(claim)]
        claims = [claim for claim in claims if claim not in elsewhere]
        reverse = [
            claim
            for claim in claims
            if reverse_leg(
                body[slice(*_sentence(body, claim.start, claim.end))],
                self.place,
                self.place_names,
            )
        ]
        claims = [claim for claim in claims if claim not in reverse]
        read = [claim for claim in read if claim not in reverse]
        kept_durations.extend(
            value
            for claim in claims
            if claim.kind == "duration" and supported(claim)
            for value in _range_values(claim)
        )
        flagged = [claim for claim in claims if not supported(claim)]
        # Lines, stops, directions, transfers and "direct" claims: together in
        # one passage of a source read this turn (route_facts.py).
        owned_clocks = [
            q.value
            for text in (*self.asked, *self.computed)
            for q in quantities(text, generous=True)
            if q.kind == "clock"
        ] + [value for text in self.asked for value in spoken_times(text)]
        # A time worked out from a duration left out goes with it (staging
        # -126: "4h to 4h30m" went, "depart no later than about 09:30" for
        # 14:00 stayed because a page gave an unrelated 09:30).
        removed = [
            value
            for claim in (*flagged, *reverse, *elsewhere)
            if claim.kind == "duration"
            for value in _range_values(claim)
        ]
        self.dropped.extend(
            value
            for value in removed
            if value not in self.dropped
            and not any(_same("duration", value, item) for item in stated)
        )
        flagged += derived_clocks(
            body,
            [c for c in claims if c not in flagged],
            read,
            [*removed, *self.dropped],
            _clock_sentences([*self.passages(), *self.computed]),
            owned_clocks,
        )
        deadline = self.deadline()
        late = (
            late_departures(body, claims, deadline, owned_clocks, self.computed)
            if deadline is not None
            else []
        )
        route = route_facts.review(
            body,
            _masked(body),
            [
                (q.start, q.end, q.value)
                for q in read
                if q.kind == "clock" and q.value == q.value
            ],
            self.passages(),
            [*self.asked, self.place],
            self.computed,
            owned_clocks,
            given=[self.place],
            pages=self.pages(),
            plan=self.plan,
        )
        advice = [claim for claim in flagged if _advice(body, claim)]
        flagged = [claim for claim in flagged if claim not in advice]
        # A sentence the values check already leaves out needs no second reason.
        taken = [
            _removal(body, claim) for claim in (*flagged, *reverse, *elsewhere, *advice)
        ]
        taken += [(start, end) for start, end, _ in late]
        route.spans = [
            span
            for span in route.spans
            if not any(a <= span[0] and span[1] <= b for a, b in taken)
        ]
        return Findings(
            flagged,
            route,
            {
                start: day
                for start, day in dated.items()
                if any(c.start == start for c in flagged)
            },
            elsewhere,
            reverse,
            advice,
            late,
            _clock_text(deadline) if deadline is not None else "",
            planned=self.planned,
            planner=_planner_enabled(),
            quotes={claim.start: _quote(body, claim) for claim in flagged},
        )

    def kept(self, body: str, found: Findings) -> str:
        """The body without what ``found`` holds, well formed."""
        removed = [
            *found.values,
            *found.elsewhere,
            *found.reverse,
            *found.advice,
            *(Quantity(s, e, "route", 0.0, label) for s, e, label in found.late),
            *(
                Quantity(
                    s,
                    e,
                    "part" if (s, e) in found.route.exact else "route",
                    0.0,
                    body[s:e],
                )
                for s, e, _ in found.route.spans
            ),
        ]
        text = wellformed(_restructure(body, _leave_out(body, removed)))
        if not removed:
            return text.rstrip()
        text = wellformed(without_orphans(body, text)).rstrip()
        return _opened(body, text, _opening(body, found), self.language)

    def left_out(self, body: str, found: Findings, language: str) -> str:
        """The body without what ``found`` holds, and one note."""
        return "\n\n".join(
            part for part in (self.kept(body, found), found.note(language)) if part
        )

    def check(self, answer: str, language: str) -> str:
        """Leave out the clauses that state unsourced travel values; one note."""
        if not self.active or not answer.strip():
            return answer
        body, fold, footer = answer.partition("<details>")
        found = self.findings(body)
        if not found:
            return answer
        self.marked = len(found)
        answer = self.left_out(body, found, language)
        if fold:
            answer += "\n\n" + fold + footer
        return answer.rstrip()

    def metadata(self) -> dict[str, Any]:
        return {"active": self.active, "marked": self.marked, "held": self.held}


current: ContextVar[Turn | None] = ContextVar("travel_claims", default=None)


def collect_tool_output(name: str, data: object) -> None:
    """Sources read by a tool this turn: the same fields grounding reads."""
    turn = current.get()
    if turn is None or not turn.active:
        return
    if name in {"web_search", "web_fetch"}:
        turn.collect(data)
    else:
        turn.collect({"text": json.dumps(data, ensure_ascii=False)[:20_000]})
