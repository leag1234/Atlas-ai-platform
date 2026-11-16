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
or the journey plan tool was offered) the answer is held, then:

- a duration or distance found in no source, in no computation and not in the
  person's words is removed and replaced by a short "not verified" marker;
- a clock time found nowhere is kept but marked "not sourced", because a
  timetable row without its time is useless and the reader must know;
- one note at the end of the answer says why, in the answer's language.

Units and duration forms are parsed by code for the five interface languages
(km, m, mi, min, h, hours, heures, Stunden, horas, ore...), never by a list of
sentences. Durations in an answer are read strictly (``1 h 20`` without a
minute unit may be a clock time, so it is not read as a duration); sources are
read generously (``2h05`` there counts as both a time and a duration), so a
sourced value is not mistaken for an invented one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from services.orchestrator.grounding import Grounding
from services.orchestrator.location import Position

Kind = Literal["duration", "distance", "clock"]

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
# "12h30", "12 h 30", "12:30", "9.45" is not read (a decimal).
_CLOCK = re.compile(r"(?<![\w.,:/])(\d{1,2})[   ]?(?:h|:)[   ]?(\d{2})(?!\d)", re.I)
# "2 pm", "11am", "14 Uhr": hours of the day without minutes.
_CLOCK_HOUR = re.compile(r"(?<![\w.,:/])(\d{1,2})[   ]?(am|pm|uhr)(?!\w)", re.I)
_MINUTES_ONLY = re.compile(rf"{_NUMBER}{_SPACE}{_MINUTES}\.?{_END}", re.I)
_HOURS_ONLY = re.compile(rf"{_NUMBER}{_SPACE}{_HOURS}\.?(?![\w²³/:])(?![   ]?\d)", re.I)
_KM = re.compile(rf"{_NUMBER}{_SPACE}{_KILOMETRES}{_END}", re.I)
_M_WORD = re.compile(rf"{_NUMBER}{_SPACE}{_METRES}{_END}", re.I)
# A bare lower-case "m" only: "12 M" is a million.
_M_SYMBOL = re.compile(rf"{_NUMBER}{_SPACE}m{_END}")
_MI = re.compile(rf"{_NUMBER}{_SPACE}{_MILES}{_END}", re.I)
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
# A margin the person is advised to keep is advice, not a fact to source.
_MARGIN = re.compile(
    r"\b(?:marge|margin|buffer|puffer|margen|margine|avance|ahead|early|im voraus|"
    r"antelaci[oó]n|anticipo|reserve|réserve|spare)\b",
    re.I,
)


@dataclass(frozen=True)
class Quantity:
    start: int
    end: int
    kind: Kind
    value: float  # minutes, metres, or minutes of the day
    text: str


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
        match: re.Match[str], kind: Kind, value: float, *, claim: bool = True
    ) -> None:
        taken.append((match.start(), match.end()))
        if claim:
            found.append(Quantity(match.start(), match.end(), kind, value, match[0]))

    for match in _HOURS_MINUTES.finditer(plain):
        add(match, "duration", _number(match[1]) * 60 + int(match[2]))
    for match in _CLOCK.finditer(plain):
        if not free(match.start(), match.end()):
            continue
        hours, minutes = int(match[1]), int(match[2])
        if minutes >= 60:
            continue
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
    # "20-25 min", "1,5 to 2 km": the lower bound of a range goes with the value.
    widened = []
    for quantity in found:
        lower = _RANGE_START.search(plain, 0, quantity.start)
        if (
            lower is not None
            and lower.end() == quantity.start
            and quantity.kind != "clock"
        ):
            quantity = Quantity(
                lower.start(),
                quantity.end,
                quantity.kind,
                quantity.value,
                text[lower.start() : quantity.end],
            )
        widened.append(quantity)
    return sorted(widened, key=lambda q: (q.start, q.kind))


def _same(kind: Kind, claimed: float, known: float) -> bool:
    if kind == "clock":
        return abs(claimed - known) < 0.5
    if kind == "duration":
        return abs(claimed - known) <= max(0.5, 0.05 * known)
    return abs(claimed - known) <= max(25.0, 0.03 * known)


WORDING: dict[str, dict[str, str]] = {
    "duration": {
        "en": "(duration not verified)",
        "fr": "(durée non vérifiée)",
        "de": "(Dauer nicht überprüft)",
        "es": "(duración sin verificar)",
        "it": "(durata non verificata)",
    },
    "distance": {
        "en": "(distance not verified)",
        "fr": "(distance non vérifiée)",
        "de": "(Entfernung nicht überprüft)",
        "es": "(distancia sin verificar)",
        "it": "(distanza non verificata)",
    },
    "clock": {
        "en": "(not sourced)",
        "fr": "(non sourcé)",
        "de": "(ohne Quelle)",
        "es": "(sin fuente)",
        "it": "(senza fonte)",
    },
    "note": {
        "en": "Not verified: the values marked above come from no source read for this answer, no computation and nothing you wrote. Atlas does not compute routes or travel times yet: check them with the transport operator.",
        "fr": "Non vérifié : les valeurs marquées ci-dessus ne viennent d’aucune source consultée pour cette réponse, d’aucun calcul ni de votre message. Atlas ne calcule pas encore d’itinéraire ni de temps de trajet : vérifiez-les auprès du transporteur.",
        "de": "Nicht überprüft: Die oben markierten Werte stammen aus keiner für diese Antwort gelesenen Quelle, keiner Berechnung und nicht aus Ihrer Nachricht. Atlas berechnet noch keine Routen oder Reisezeiten: Prüfen Sie sie beim Verkehrsunternehmen.",
        "es": "Sin verificar: los valores marcados arriba no proceden de ninguna fuente consultada para esta respuesta, de ningún cálculo ni de su mensaje. Atlas aún no calcula rutas ni tiempos de viaje: compruébelos con el transportista.",
        "it": "Non verificato: i valori segnati sopra non provengono da alcuna fonte consultata per questa risposta, da alcun calcolo né dal suo messaggio. Atlas non calcola ancora itinerari né tempi di viaggio: li verifichi presso il vettore.",
    },
}


def wording(key: str, language: str) -> str:
    table = WORDING[key]
    return table.get(language, table["en"])


def _line_bounds(text: str, index: int) -> tuple[int, int]:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    return start, len(text) if end < 0 else end


@dataclass
class Turn:
    """The travel facts of one turn: what was read, computed and asked."""

    active: bool = False
    position: Position | None = field(default=None, repr=False)
    place: str = field(default="", repr=False)
    evidence: Grounding = field(default_factory=Grounding)
    computed: list[str] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)
    hold: Callable[[], bool] | None = None
    # The URLs read in this turn (D7) and the answer language, set by the pipeline.
    retrieved: Callable[[], list[str]] = field(default=lambda: [], repr=False)
    language: str = "en"
    journey_offered: bool = False
    journey_called: bool = False
    results: int = 0
    held: bool = False
    marked: int = 0

    def located(self, *, place: str = "", position: Position | None = None) -> None:
        """The person's position or stated place for this turn (memory only)."""
        self.place = place or self.place
        self.position = position or self.position
        self.activate()

    def activate(self) -> None:
        self.active = True
        if not self.held and self.hold is not None:
            self.held = self.hold()

    def collect(self, value: object) -> None:
        if self.active:
            self.evidence.collect(value)

    def collect_text(self, text: str) -> None:
        if self.active:
            self.evidence.add(text)

    def compute(self, text: str) -> None:
        """Values written by code this turn (journey plan, straight-line distance)."""
        self.computed.append(text)

    def known(self) -> dict[Kind, list[float]]:
        values: dict[Kind, list[float]] = {"duration": [], "distance": [], "clock": []}
        for text in (*self.evidence.evidence, *self.computed, *self.asked):
            for quantity in quantities(text, generous=True):
                values[quantity.kind].append(quantity.value)
        return values

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
        return " ".join(parts)

    def check(self, answer: str, language: str) -> str:
        """Remove unsourced durations and distances, mark unsourced clock times."""
        if not self.active or not answer.strip():
            return answer
        claims = [
            q
            for q in quantities(answer)
            if not _MARGIN.search(answer[max(0, q.start - 40) : q.end + 40])
        ]
        if not claims:
            return answer
        known = self.known()
        sourced_clocks = known["clock"]

        def supported(claim: Quantity) -> bool:
            if any(
                _same(claim.kind, claim.value, value) for value in known[claim.kind]
            ):
                return True
            if claim.kind != "duration":
                return False
            # A duration between two sourced times of the same line ("10:05 to
            # 12:00, 1 h 55 min") is arithmetic on sourced values.
            start, end = _line_bounds(answer, claim.start)
            times = [
                q.value
                for q in quantities(answer[start:end])
                if q.kind == "clock"
                and any(_same("clock", q.value, value) for value in sourced_clocks)
            ]
            return any(
                abs(((later - earlier) % 1440) - claim.value) <= 1
                for earlier in times
                for later in times
                if later != earlier
            )

        flagged = [claim for claim in claims if not supported(claim)]
        if not flagged:
            return answer
        self.marked = len(flagged)
        edits: list[tuple[int, int, str]] = []
        for claim in flagged:
            if claim.kind == "clock":
                edits.append((claim.end, claim.end, " " + wording("clock", language)))
            else:
                edits.append((claim.start, claim.end, wording(claim.kind, language)))
        for start, end, replacement in sorted(edits, reverse=True):
            answer = answer[:start] + replacement + answer[end:]
        # One note, after the answer and before its folded source list.
        note = wording("note", language)
        fold = answer.rfind("<details>")
        if fold > 0:
            answer = answer[:fold].rstrip() + "\n\n" + note + "\n\n" + answer[fold:]
        else:
            answer = answer.rstrip() + "\n\n" + note
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
