"""Relative dates resolved by code: the turn's calendar and a check of the answer (T8).

"il fait quel temps demain a Bayeux" was answered "demain lundi 16 novembre 2026"
on Monday 16 November (staging 2026-11-16). A chat turn had no date in its
context: the date_time tool is offered on calendar questions only (exact_tools),
so the model read today from a search result's consulted_at and guessed which
day tomorrow is. Two pieces close the class, both deterministic:

- ``note``: a question that names a relative day (today, tomorrow, yesterday, a
  weekday, the weekend, next week, in N days...) gets the calendar around today,
  computed by code in the person's time zone, with the dates also written in the
  answer language. The model reads its dates there instead of working them out.
  Other turns keep the request they have always sent, so recorded provider
  exchanges stay valid.
- ``repair`` and ``Gate``: every answer is checked. A relative day word followed
  by a date or a weekday ("demain lundi 16 novembre") must name the day code
  computes; a weekday written with its date must be that date's weekday. A near
  miss is repaired in place, in the words and form the answer used; the stream
  and the stored answer receive the same text.

Weekday and month names come from CLDR (Babel) for any answer language. Babel
does not ship CLDR's relative day fields, so the relative day words are a fixed
list in the five interface languages; other languages get the weekday check.
Nothing here goes into prompts/.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.orchestrator import datecalc
from services.orchestrator.reminder_parse import NUMBERS, _one_edit, fold

Sink = Callable[[dict[str, object]], Awaitable[None]]

# Folded (lower case, no accents) relative day words and their offset from today.
# "Morgen" (morning) and "manana" (morning) are told apart from tomorrow by the
# word before them (_MORNING_BEFORE).
RELATIVE_DAYS: dict[str, int] = {
    "today": 0,
    "tomorrow": 1,
    # Chat abbreviations of tomorrow, out of reach of one edit.
    "tmrw": 1,
    "tmrrw": 1,
    "yesterday": -1,
    "day after tomorrow": 2,
    "day before yesterday": -2,
    "aujourd'hui": 0,
    "aujourdhui": 0,
    "demain": 1,
    "hier": -1,
    "apres-demain": 2,
    "apres demain": 2,
    "avant-hier": -2,
    "avant hier": -2,
    "heute": 0,
    "morgen": 1,
    "gestern": -1,
    "ubermorgen": 2,
    "uebermorgen": 2,
    "vorgestern": -2,
    "hoy": 0,
    "manana": 1,
    "ayer": -1,
    "pasado manana": 2,
    "anteayer": -2,
    "antes de ayer": -2,
    "antier": -2,
    "oggi": 0,
    "domani": 1,
    "ieri": -1,
    "dopodomani": 2,
    "dopo domani": 2,
    "altro ieri": -2,
    "altroieri": -2,
    # A few more languages, for the question cue and the answer check.
    "vandaag": 0,
    "gisteren": -1,
    "overmorgen": 2,
    "eergisteren": -2,
    "hoje": 0,
    "amanha": 1,
    "ontem": -1,
    "depois de amanha": 2,
    "anteontem": -2,
    "dzisiaj": 0,
    "jutro": 1,
    "wczoraj": -1,
    "pojutrze": 2,
    "idag": 0,
    "i dag": 0,
    "imorgon": 1,
    "i morgen": 1,
    "igar": -1,
    "i gar": -1,
    "avui": 0,
    "dema": 1,
    "ahir": -1,
    "bugun": 0,
    "yarin": 1,
    "сегодня": 0,
    "завтра": 1,
    "вчера": -1,
    "послезавтра": 2,
    "позавчера": -2,
}
# Scripts written without spaces: matched without word boundaries, for the cue.
_UNSPACED_CUES = (
    "今天",
    "明天",
    "昨天",
    "后天",
    "後天",
    "前天",
    "今日",
    "明日",
    "昨日",
    "明後日",
)
# Only for deciding that a question is about a relative day.
_EXTRA_CUES = (
    "tonight",
    "ce soir",
    "cette nuit",
    "heute abend",
    "heute nacht",
    "esta noche",
    "stasera",
    "stanotte",
    # Frequent misspellings that one edit does not reach.
    "tommorow",
    "tommorrow",
    "2morrow",
    "2moro",
    "aujourd hui",
    "ajourd'hui",
    "ojourd'hui",
)
_MORNING_BEFORE = re.compile(
    r"(?:\b(?:heute|gestern|vorgestern|ubermorgen|morgen|am|jeden|guten|frueh|fruh|"
    r"la|esta|esa|por|cada|una|toda|de|del|manana|buenos)[ \t]+)$"
)
_SPACE = r"[ \t\u00a0\u202f]"
_S = _SPACE + "{1,3}"
_CONNECTORS = (
    "will be",
    "is",
    "was",
    "c'est",
    "ce sera",
    "sera",
    "est",
    "etait",
    "ist",
    "wird",
    "war",
    "es",
    "fue",
    "era",
    "sara",
    "e",
)
_GAP = (
    r"(?P<gap>[ \t\u00a0\u202f,(:*\u2013\u2014-]{1,6}"
    rf"(?:(?:{'|'.join(re.escape(c).replace(chr(92) + ' ', _S) for c in _CONNECTORS)})"
    r"[ \t\u00a0\u202f,(:*]{1,5})?)"
)
# A weekday alone after a relative word ("demain lundi") is checked only when the
# weekday is not the start of a list ("demain, lundi et mardi").
_LIST_AFTER = re.compile(rf"^(?:{_S}|,)(?:et|and|und|y|e|ou|or|oder|o|bis)\b")
_PERIOD_CUES = re.compile(
    r"\b(?:week[-\s]?+ends?|wochenende\w*|fin(?:de)?\s+de\s+semana|finde|"
    r"fine[-\s]?+settimana|(?:next|this|coming)\s+week|"
    r"semaine\s+(?:prochaine|qui\s+vient)|cette\s+semaine|"
    r"(?:nachste|naechste|kommende|diese)\s+woche|(?:esta|proxima)\s+semana|"
    r"semana\s+(?:que\s+viene|proxima)|(?:questa|prossima)\s+settimana|"
    r"settimana\s+(?:prossima|che\s+viene))\b"
)
_WEEKEND = re.compile(
    r"\b(?:week[-\s]?+ends?|wochenende\w*|fin(?:de)?\s+de\s+semana|finde|"
    r"fine[-\s]?+settimana|fim\s+de\s+semana|weekend\w*)\b"
)
_QTY = (
    r"(?:\d{1,3}|"
    + "|".join(sorted((w for w in NUMBERS if len(w) > 1), key=len, reverse=True))
    + ")"
)
_UNITS = r"(?:jours?|semaines?|days?|weeks?|tag(?:e|en)?|wochen?|dias?|semanas?|giorni|settimane?)"
_IN_DAYS = re.compile(
    rf"\b(?:dans|d'ici|in|within|en|dentro\s+de|tra|fra|entro)\s+"
    rf"(?:(?:les|the|next|los|las|i|le|la)\s+)?{_QTY}\s+(?:(?:prochains?|next)\s+)?{_UNITS}\b"
    rf"|\b{_QTY}\s+{_UNITS}\s+(?:ago|plus\s+tard|later|from\s+now|fa)\b"
    rf"|\b(?:il\s+y\s+a|vor|hace)\s+{_QTY}\s+{_UNITS}\b"
)
_WORD = re.compile(r"[^\W\d_]+")


def _key(name: str) -> str:
    return re.sub(r"[ \t\u00a0\u202f]+", " ", fold(name).strip()).rstrip(".")


def _alternation(names: list[str]) -> str:
    return "|".join(
        re.escape(name).replace(r"\ ", _S)
        for name in sorted(set(names), key=len, reverse=True)
    )


_RELATIVE = re.compile(
    rf"(?<!\w)(?P<rel>{_alternation(list(RELATIVE_DAYS))})(?!\w)",
)
_CUES = re.compile(rf"(?<!\w)(?:{_alternation(list(_EXTRA_CUES))})(?!\w)")
_UNSPACED = re.compile("|".join(_UNSPACED_CUES))
_TYPO_TARGETS = tuple(
    w for w in RELATIVE_DAYS if " " not in w and len(w) >= 6 and w.isascii()
)
# Ordinary words one edit away from a relative day word.
_NOT_TYPOS = frozenset({"domain", "morgan", "gestirn"})


@lru_cache(maxsize=1)
def _all_weekdays() -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Weekday names of every CLDR language, folded.

    Latin names shorter than five letters are left out ("sun", "sed", "mane" and
    the like are ordinary words); other scripts keep their names, matched
    without word boundaries (Chinese and Japanese write no spaces).
    """
    from babel import Locale, localedata

    latin: set[str] = set()
    other: set[str] = set()
    for identifier in localedata.locale_identifiers():
        if "_" in identifier:
            continue
        try:
            locale = Locale.parse(identifier)
        except (ValueError, LookupError):
            continue
        for context in ("format", "stand-alone"):
            for name in locale.days[context]["wide"].values():
                folded = fold(name).strip()
                if folded.isascii():
                    if len(folded) >= 5:
                        latin.add(folded)
                elif len(folded) >= 2:
                    other.add(folded)
    return (
        re.compile(rf"(?<!\w)(?:{_alternation(sorted(latin))})(?!\w)"),
        re.compile(_alternation(sorted(other))),
    )


def mentions_relative_date(question: str, previous: str = "") -> bool:
    """Whether the question names a day relative to today, in any language.

    A short follow-up ("et samedi ?", "and in Caen?") inherits the previous
    question's subject, like exact_tools.relevant.
    """
    text = fold(question[:4000])
    latin, other = _all_weekdays()
    if (
        _RELATIVE.search(text)
        or _CUES.search(text)
        or _UNSPACED.search(text)
        or _PERIOD_CUES.search(text)
        or _IN_DAYS.search(text)
        or latin.search(text)
        or other.search(text)
        or any(
            _one_edit(word, target)
            for word in _WORD.findall(text)
            if len(word) >= 6 and word not in _NOT_TYPOS
            for target in _TYPO_TARGETS
        )
    ):
        return True
    return bool(previous) and len(text) <= 120 and mentions_relative_date(previous)


def home_zone(name: str | None) -> str:
    """The person's IANA zone; "UTC" is the request default, as in the chat tools."""
    if name and name != "UTC":
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
        else:
            return name
    return datecalc.DEFAULT_ZONE


def local_today(zone_name: str | None, now: datetime | None = None) -> date:
    return (now or datecalc.utc_now()).astimezone(ZoneInfo(home_zone(zone_name))).date()


def _babel_locale(language: str) -> str:
    from babel import Locale, UnknownLocaleError

    try:
        Locale.parse(language)
    except (ValueError, UnknownLocaleError, TypeError):
        return "en"
    return language


def calendar(zone_name: str | None, language: str, now: datetime | None = None) -> str:
    """The calendar around today, in the person's zone, for the system context."""
    from babel.dates import format_date

    zone = home_zone(zone_name)
    instant = (now or datecalc.utc_now()).astimezone(ZoneInfo(zone))
    today = instant.date()
    offset = instant.strftime("%z")
    locale = _babel_locale(language)

    def day(value: date) -> str:
        english = f"{datecalc.WEEKDAYS[value.weekday()]} {value.isoformat()}"
        if locale == "en":
            return english
        return f"{english} ({format_date(value, 'full', locale=locale)})"

    labels = {
        -2: "day before yesterday",
        -1: "yesterday",
        0: "today",
        1: "tomorrow",
        2: "day after tomorrow",
    }
    lines = [
        "Calendar computed by code from the clock, in the person's time zone "
        f"{zone} (UTC{offset[:3]}:{offset[3:]}), where it is now {instant:%H:%M}. "
        "Read every relative date from it (today, tomorrow, yesterday, a weekday, "
        "this weekend, next week, in N days): never work out a date or a weekday "
        "yourself, and write a date with the weekday listed here."
    ]
    for shift in range(-2, 14):
        lines.append(
            f"- {labels.get(shift, f'in {shift} days')}: {day(today + timedelta(days=shift))}"
        )
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    if today.weekday() == 6:
        saturday = today - timedelta(days=1)
    monday = today - timedelta(days=today.weekday())
    for label, start, length in (
        ("this weekend", saturday, 1),
        ("next weekend", saturday + timedelta(days=7), 1),
        ("this week", monday, 6),
        ("next week", monday + timedelta(days=7), 6),
    ):
        end = start + timedelta(days=length)
        lines.append(
            f"- {label}: {datecalc.WEEKDAYS[start.weekday()]} {start.isoformat()} "
            f"to {datecalc.WEEKDAYS[end.weekday()]} {end.isoformat()}"
        )
    return "\n".join(lines)


def note(
    question: str,
    previous: str,
    zone_name: str | None,
    language: str,
    now: datetime | None = None,
) -> str:
    """System context for this turn: the calendar when the question needs it."""
    if not mentions_relative_date(question, previous):
        return ""
    return "\n\n" + calendar(zone_name, language, now) + "\n"


# --- Checking the answer -----------------------------------------------------


@dataclass(frozen=True)
class _Name:
    index: int
    context: str
    width: str


@dataclass(frozen=True)
class _Names:
    locale: str
    weekdays: dict[str, _Name]
    months: dict[str, _Name]
    relative: re.Pattern[str]
    dated: re.Pattern[str]
    anchors: re.Pattern[str]
    wide: re.Pattern[str]
    # Words a date phrase is made of, and prefixes of the words that start one.
    vocabulary: frozenset[str]
    vocabulary_prefixes: frozenset[str]
    anchor_prefixes: frozenset[str]


def _date_pattern(prefix: str, months: str) -> str:
    day = rf"(?P<{prefix}day>[0-3]?\d)(?!\d)(?P<{prefix}suf>er|st|nd|rd|th|\.)?"
    month = rf"(?P<{prefix}mon>{months})(?!\w)\.?"
    year = rf"(?P<{prefix}year>(?:19|20|21)\d\d)(?!\d)"
    first = rf"{day}{_S}(?:(?:de|of){_S})?{month}(?:,?{_S}(?:(?:de|del){_S})?{year})?"
    return first


def _month_first(prefix: str, months: str) -> str:
    day = rf"(?P<{prefix}day>[0-3]?\d)(?!\d)(?P<{prefix}suf>st|nd|rd|th)?"
    month = rf"(?P<{prefix}mon>{months})(?!\w)\.?"
    year = rf"(?P<{prefix}year>(?:19|20|21)\d\d)(?!\d)"
    return rf"{month}{_S}{day}(?:,?{_S}{year})?"


@lru_cache(maxsize=32)
def _names(language: str) -> _Names:
    from babel import Locale

    locale = _babel_locale(language)
    data = Locale.parse(locale)
    weekdays: dict[str, _Name] = {}
    months: dict[str, _Name] = {}
    for width in ("wide", "abbreviated"):
        for context in ("format", "stand-alone"):
            for index, name in data.days[context][width].items():
                key = _key(name)
                if len(key) >= 2:
                    weekdays.setdefault(key, _Name(index, context, width))
            for index, name in data.months[context][width].items():
                key = _key(name)
                if len(key) >= 3:
                    months.setdefault(key, _Name(index, context, width))
    weekday = rf"(?P<wd>{_alternation(list(weekdays))})(?!\w)\.?"
    month_names = _alternation(list(months))
    article = rf"(?:(?:le|the|den|el|il|lo){_S})?"
    dates = rf"(?:{_date_pattern('a', month_names)}|{_month_first('b', month_names)})"
    # Markdown emphasis may close after the weekday ("**Mardi** 17 novembre").
    after = rf"\*{{0,2}},?{_S}{article}"
    dated = re.compile(rf"(?<!\w){weekday}{after}{dates}")
    relative = re.compile(
        rf"(?<!\w)(?P<rel>{_alternation(list(RELATIVE_DAYS))})(?!\w){_GAP}"
        rf"(?:{weekday}{after}{dates}"
        rf"|{_date_pattern('c', month_names)}|{_month_first('d', month_names)}"
        rf"|(?P<alone>{_alternation(list(weekdays))})(?!\w))"
    )
    starts = {re.split(r"[\s'-]", word)[0] for word in RELATIVE_DAYS}
    starts |= {re.split(r"[\s'-]", word)[0] for word in weekdays}
    anchors = re.compile(rf"(?<!\w)(?:{_alternation(sorted(starts))})(?!\w)")
    wide = re.compile(
        rf"(?<!\w)(?:{_alternation([k for k, v in weekdays.items() if v.width == 'wide'])})(?!\w)"
    )
    words = {
        word
        for phrase in (
            *RELATIVE_DAYS,
            *_CONNECTORS,
            *weekdays,
            *months,
            "le the den el il lo de of del er st nd rd th",
        )
        for word in re.findall(r"\w+", phrase)
    }
    return _Names(
        locale,
        weekdays,
        months,
        relative,
        dated,
        anchors,
        wide,
        frozenset(words),
        frozenset(w[:n] for w in words for n in range(1, len(w) + 1)),
        frozenset(
            w[:n]
            for start in starts
            for w in re.findall(r"\w+", start)[:1]
            for n in range(1, len(w) + 1)
        ),
    )


# Characters a date phrase may contain between its words.
_PHRASE_MARKS = frozenset(" \t\u00a0\u202f,(:*.-\u2013\u2014'")


def _pending(tail: str, names: _Names) -> bool:
    """Whether the folded text from an anchor to the end may still grow into a
    longer date phrase: every word and mark in it can belong to one."""
    if any(not c.isalnum() and c != "_" and c not in _PHRASE_MARKS for c in tail):
        return False
    words = re.findall(r"\w+", tail)
    open_word = words.pop() if words and re.search(r"\w$", tail) else None
    if not all(word.isdigit() or word in names.vocabulary for word in words):
        return False
    return (
        open_word is None
        or open_word.isdigit()
        or open_word in names.vocabulary_prefixes
    )


def _form(names: _Names, kind: str, index: int, like: _Name, original: str) -> str:
    from babel import Locale

    data = Locale.parse(names.locale)
    table = data.days if kind == "day" else data.months
    value = str(table[like.context][like.width][index])
    if original.rstrip(".") == original and value.endswith("."):
        value = value[:-1]
    if len(original) > 1 and original.isupper():
        value = value.upper()
    elif original[:1].isupper():
        value = value[:1].upper() + value[1:]
    elif original[:1].islower():
        value = value[:1].lower() + value[1:]
    return value


def _nearest(day: int, month: int, anchor: date) -> date | None:
    best: date | None = None
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((candidate - anchor).days) < abs((best - anchor).days):
            best = candidate
    return best


def _suffix(original: str, day: int) -> str:
    if original in {"st", "nd", "rd", "th"}:
        if 10 <= day % 100 <= 20:
            return "th"
        return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    if original == "er":
        return "er" if day == 1 else ""
    return original


class _Edit:
    def __init__(self, text: str, names: _Names) -> None:
        self.text = text
        self.names = names
        self.changes: list[tuple[int, int, str]] = []

    def span(self, match: re.Match[str], group: str) -> tuple[int, int, str]:
        start, end = match.span(group)
        return start, end, self.text[start:end]

    def weekday(self, match: re.Match[str], group: str, index: int) -> None:
        start, end, original = self.span(match, group)
        like = self.names.weekdays[_key(original)]
        if like.index != index:
            self.changes.append(
                (start, end, _form(self.names, "day", index, like, original))
            )

    def date(self, match: re.Match[str], prefix: str, value: date) -> None:
        start, end, original = self.span(match, prefix + "day")
        self.changes.append((start, end, str(value.day)))
        if match.group(prefix + "suf"):
            start, end, original = self.span(match, prefix + "suf")
            self.changes.append((start, end, _suffix(original, value.day)))
        start, end, original = self.span(match, prefix + "mon")
        like = self.names.months[_key(original)]
        if like.index != value.month:
            self.changes.append(
                (start, end, _form(self.names, "month", value.month, like, original))
            )
        if match.group(prefix + "year"):
            start, end, _ = self.span(match, prefix + "year")
            self.changes.append((start, end, str(value.year)))

    def result(self) -> str:
        text = self.text
        for start, end, value in sorted(self.changes, reverse=True):
            text = text[:start] + value + text[end:]
        return text


def _stated(
    match: re.Match[str], prefixes: str, names: _Names, anchor: date
) -> tuple[str, date] | None:
    for prefix in prefixes:
        if match.group(prefix + "day") is None:
            continue
        day = int(match.group(prefix + "day"))
        month = names.months[_key(match.group(prefix + "mon"))].index
        year = match.group(prefix + "year")
        if year:
            try:
                return prefix, date(int(year), month, day)
            except ValueError:
                return None
        value = _nearest(day, month, anchor)
        return (prefix, value) if value is not None else None
    return None


# A date beside a relative word is repaired only when it is a near miss: a date
# further away is another reference frame (a quotation, an anniversary).
NEAR_MISS_DAYS = 7
# A weekday with a date but no year is checked only near today.
UNDATED_RANGE_DAYS = 60


def _repair_relative(text: str, before: str, names: _Names, today: date) -> str:
    folded = fold(before + text)
    edit = _Edit(before + text, names)
    for match in names.relative.finditer(folded):
        if match.start() < len(before):
            continue
        rel = re.sub(r"\s+", " ", match.group("rel"))
        if rel in {"morgen", "manana"} and _MORNING_BEFORE.search(
            folded[max(0, match.start() - 20) : match.start()]
        ):
            continue
        expected = today + timedelta(days=RELATIVE_DAYS[rel])
        if match.group("alone") is not None:
            name = names.weekdays[_key(match.group("alone"))]
            gap = match.group("gap")
            if (
                name.width != "wide"
                or "," in gap
                or any(dash in gap for dash in "-\u2013\u2014")
                or _LIST_AFTER.match(folded[match.end() : match.end() + 12])
            ):
                continue
            edit.weekday(match, "alone", expected.weekday())
            continue
        stated = _stated(match, "abcd", names, expected)
        if stated is None:
            continue
        prefix, value = stated
        if value != expected:
            # A wrong year alone is a slip too ("demain 17 novembre 2025").
            same_year = _nearest(value.day, value.month, expected) or value
            if abs((same_year - expected).days) > NEAR_MISS_DAYS:
                continue
            edit.date(match, prefix, expected)
        if match.group("wd") is not None:
            edit.weekday(match, "wd", expected.weekday())
    return edit.result()[len(before) :]


def asked_weekdays(question: str, language: str) -> frozenset[int]:
    """Weekdays the person named (the weekend names two): their intent.

    When an answer writes one of them with a date of another weekday, the date is
    what was worked out wrong; otherwise the weekday is.
    """
    names = _names(language)
    text = fold(question[:4000])
    found = {
        names.weekdays[_key(match.group(0))].index
        for match in names.wide.finditer(text)
    }
    if _WEEKEND.search(text):
        found |= {5, 6}
    return frozenset(found)


def _repair_weekdays(
    text: str, before: str, names: _Names, today: date, asked: frozenset[int]
) -> str:
    folded = fold(before + text)
    edit = _Edit(before + text, names)
    for match in names.dated.finditer(folded):
        if match.start() < len(before):
            continue
        stated = _stated(match, "ab", names, today)
        if stated is None:
            continue
        prefix, value = stated
        if (
            not match.group(prefix + "year")
            and abs((value - today).days) > UNDATED_RANGE_DAYS
        ):
            continue
        written = names.weekdays[_key(match.group("wd"))].index
        if written == value.weekday():
            continue
        if written in asked:
            shift = next(
                d
                for d in range(-3, 4)
                if (value + timedelta(days=d)).weekday() == written
            )
            edit.date(match, prefix, value + timedelta(days=shift))
        else:
            edit.weekday(match, "wd", value.weekday())
    return edit.result()[len(before) :]


def repair(
    text: str,
    language: str,
    today: date,
    before: str = "",
    asked: frozenset[int] = frozenset(),
) -> str:
    """The answer with its relative dates and weekdays made consistent with today.

    ``before`` is text already sent just before ``text``: it is read for context
    and never changed.
    """
    if not text:
        return text
    names = _names(language)
    text = _repair_relative(text, before, names, today)
    return _repair_weekdays(text, before, names, today, asked)


# The longest phrase the checks read is well under this: text that ends more
# than this far before the end of the held text is final.
WINDOW = 240
_CONTEXT = 24


class Rewriter:
    """Streams answer text through ``repair``, holding back only what may still
    become a date phrase."""

    def __init__(
        self, language: str, today: date, asked: frozenset[int] = frozenset()
    ) -> None:
        self.language = language
        self.today = today
        self.asked = asked
        self.held = ""
        self.sent = ""

    def _cut(self) -> int:
        names = _names(self.language)
        held = self.held
        folded = fold(held)
        # Only text that may still become a date phrase is held: a last word
        # that may grow into a phrase's first word, or a phrase still open.
        # Any other text goes out at once (first-content latency unchanged).
        cut = len(held)
        tail = re.search(r"\w+$", folded)
        if tail and tail.group(0) in names.anchor_prefixes:
            cut = tail.start()
        start = max(0, len(held) - WINDOW)
        for found in names.anchors.finditer(folded, start):
            if found.start() >= cut:
                break
            if _pending(folded[found.start() :], names):
                cut = found.start()
                break
        # A phrase that started earlier and runs over the cut stays whole.
        moved = True
        while moved:
            moved = False
            for pattern in (names.relative, names.dated):
                for match in pattern.finditer(folded, max(0, cut - WINDOW)):
                    if match.start() < cut < match.end():
                        cut, moved = match.start(), True
        return cut

    def _emit(self, text: str) -> str:
        out = repair(text, self.language, self.today, self.sent[-_CONTEXT:], self.asked)
        self.sent = (self.sent + out)[-_CONTEXT:]
        return out

    def feed(self, text: str) -> str:
        self.held += text
        cut = self._cut()
        if cut <= 0:
            return ""
        ready, self.held = self.held[:cut], self.held[cut:]
        return self._emit(ready)

    def finish(self) -> str:
        ready, self.held = self.held, ""
        return self._emit(ready) if ready else ""


class Gate:
    """Streams answer text through a Rewriter; other events keep their order."""

    def __init__(self, sink: Sink, rewriter: Rewriter) -> None:
        self.sink = sink
        self.rewriter = rewriter

    async def __call__(self, event: dict[str, object]) -> None:
        delta = event.get("delta")
        if (
            len(event) == 1
            and isinstance(delta, dict)
            and len(delta) == 1
            and isinstance(delta.get("content"), str)
        ):
            text = self.rewriter.feed(str(delta["content"]))
            if text:
                await self.sink({"delta": {"content": text}})
            return
        await self.flush()
        await self.sink(event)

    async def flush(self) -> None:
        text = self.rewriter.finish()
        if text:
            await self.sink({"delta": {"content": text}})
