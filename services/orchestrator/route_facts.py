"""Route facts in a travel answer come from a page read in the turn.

Staging -98 (2026-11-16, phone, device position): three real trips kept
invented local-transit facts because the travel check read only numbers: a
metro transfer at a station the second line does not serve, a train said to
stop under the airport terminal when its source named only the city, an S-Bahn
leg "from general knowledge". Atlas has no routing engine (owner decision):
a line or train number, a platform, a stop or station on a route, a direction,
a transfer, a "direct" service, a frequency or an operator written in a travel
answer is a fact, and it must come from a page read in this turn.

The unit checked is the sentence (a table row; a parenthesis when the rest of
the sentence holds without it). Its route anchors (line identifiers, place
names in route positions, "direct", a frequency, its clock times) must appear
together in one passage of one source, within ``WINDOW`` characters: one page
naming the line and another naming the stop do not source the transfer. A unit
that fails is left out whole by the travel check (travel_claims), a sentence
that only continues it ("Trains run every few minutes.") goes with it, and the
closing note says the route details could not be verified and where to check.

Names the person gave are theirs only when no source writes them (an address,
a place written in another language); a name the sources do write must sit with
the rest of the claim. Clock times the person gave or code computed are never
required. Any country, any language: the words below are the closed classes of
transit vocabulary of the five interface languages, never places or sentences.
Every pass is linear in the size of the answer and of the sources.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from services.orchestrator.grounding import normalized
from services.orchestrator.unsourced_details import (
    COMMON,
    CONNECTORS,
    STOP,
    TITLES,
)

# Characters of normalized source text that one claim may span.
WINDOW = 500
# The opening of a passage: a page naming a line there is about that line.
LEAD = 400

# Transit vocabulary, folded (lower case, no accents), five languages.
TRANSIT_WORDS = frozenset(
    (
        "line lines ligne lignes linie linien linea lineas linee metro metros "
        "subway underground tube s-bahn u-bahn sbahn ubahn bahn tram trams tramway "
        "tramways strassenbahn straßenbahn tranvia tranvias tranvie bus buses busse "
        "autobus rer ter tgv ice ic ec ir re rb rex train trains zug zuge treno treni "
        "tren trenes voie voies quai quais platform platforms track tracks gleis "
        "bahnsteig anden andenes binario binari"
    ).split()
)
# Station-type words: dropped from a name before matching ("Gare Part-Dieu" and
# "Lyon Part-Dieu" name one place; "Hbf" is also written "Hauptbahnhof").
STATION_WORDS = frozenset(
    (
        "gare gares station stations bahnhof hauptbahnhof hbf bf estacion "
        "estaciones stazione stazioni arret arrets stop stops haltestelle "
        "fermata fermate parada paradas"
    ).split()
)
# Place-type words: no name alone ("the airport"), part of one with another
# word ("Geneva Airport" is not "Geneva").
PLACE_WORDS = frozenset(
    (
        "aeroport airport flughafen aeropuerto aeroporto terminal port hafen "
        "puerto porto"
    ).split()
)
_ROUTE_VERBS = (
    r"prene?z|prendre|prends|take|taking|nehmen|nimm|tome|toma|tomar|prendi|"
    r"prenda|prendere|descende?z|descendre|get off|alight|aussteigen|steigen|"
    r"baje|bajar|bajese|scendi|scenda|scendere|change[zr]?|changement|"
    r"transfer\w*|umsteig\w*|umstieg|cambi\w*|correspondances?|connections?|"
    r"connecting|anschluss\w*|coincidenc\w*|direction|towards?|richtung|"
    r"direccion|direzione|terminus|endstation"
)
# Words that make a sentence about a route (with a name in it).
_ROUTE = re.compile(
    r"(?<![\w-])(?:"
    + _ROUTE_VERBS
    + "|"
    + "|".join(
        sorted(map(re.escape, TRANSIT_WORDS | STATION_WORDS), key=len, reverse=True)
    )
    + r")(?![\w-])|→|->",
)
_TRANSIT = re.compile(
    r"(?<![\w-])(?:"
    + "|".join(
        sorted(map(re.escape, TRANSIT_WORDS | STATION_WORDS), key=len, reverse=True)
    )
    + r")(?![\w-])"
)
_LINE_WORDS = re.compile(r"(?<![\w-])(?:lines?|lignes?|linien?|lineas?|linee)(?![\w-])")
# Words a place name follows on a route ("until Bellecour", "direction X",
# "bis Marienplatz", "-> Geneva"), an article allowed in between.
_SLOT = re.compile(
    r"(?:(?<![\w-])(?:direction|directions|towards?|richtung|direccion|direzione|"
    r"vers|jusqu|a|au|aux|at|to|from|depuis|de|du|des|via|bis|nach|ab|von|vom|"
    r"zum|zur|am|an|in|im|hasta|desde|en|hacia|para|fino|da|dal|dalla|verso|per|"
    r"until|till|entre|between|and|et|und|y|e|"
    + "|".join(sorted(STATION_WORDS, key=len, reverse=True))
    + r"|terminus|endstation)|→|->|>)"
    r"\s+(?:(?:la|le|les|l|the|der|die|das|den|dem|el|los|las|il|lo|gli)\s+)?$"
)
# The answer's own words for "direct" (adjectives; adverbs count next to a
# transit or station word, "achetez directement en ligne" is no route).
_DIRECT = re.compile(
    r"(?<![\w-])(?:direct|directe|directs|directes|direkt|direkte[nrs]?|directo|"
    r"directa|directos|directas|diretto|diretta|diretti|dirette|nonstop|non-stop|"
    r"sans (?:changement|correspondance)|without (?:changing|a change|changes)|"
    r"ohne umsteigen|sin (?:transbordos?|cambios?)|senza cambi\w*)(?![\w-])"
)
_DIRECT_ADVERB = re.compile(
    r"(?<![\w-])(?:directement|directly|direttamente|directamente)(?![\w-])"
)
_FREQUENCY = re.compile(
    r"(?<![\w-])(?:every|toutes les|tous les|alle|cada|ogni)\s+(?:\d+|few|couple|"
    r"quelques|paar|pocos|pochi|deux|two|zwei|dos|due|trois|three|drei|tres|tre|"
    r"cinq|five|funf|cinco|cinque|dix|ten|zehn|diez|dieci|quinze|fifteen|"
    r"funfzehn|quince|quindici|vingt|twenty|zwanzig|veinte|venti|trente|thirty|"
    r"dreissig|treinta|trenta)\b.{0,14}?(?:min\w*|heures?|hours?|stunden?|horas?|"
    r"ore)\b|(?<![\w-])(?:frequen\w*|frecuen\w*|takt\w*|cadenc\w*)"
)
# A sentence that says its own content is unsourced ("from general knowledge").
_DISCLAIMER = re.compile(
    r"general knowledge|from memory|not (?:been )?verified|connaissances? "
    r"generales?|de memoire|non verifie|allgemeinwissen|aus dem gedachtnis|"
    r"nicht (?:uberpruft|verifiziert)|conocimiento general|de memoria|"
    r"sin verificar|conoscenz\w* general\w*|a memoria|non verificat\w*"
)
# A sentence that names a gap ("The connections between A and B are not given
# by the sources", "check it in the operator's planner") states no route: the
# places it names are what is missing, not a claim (staging -102: a rewrite
# saying so was rejected for naming the person's own stop).
_NEGATION = re.compile(
    r"(?<![\w-])(?:not|no|none|cannot|never|\w+n['’]t|n|ne|pas|aucune?|jamais|"
    r"kein\w*|nicht|nie|ningun\w*|nunca|nessun\w*|non)(?![\w-])"
)
_SOURCE_WORDS = re.compile(
    r"(?<![\w-])(?:sources?|pages?|quellen?|seiten?|fuentes?|paginas?|fonti|"
    r"pagine|precis\w*|indiqu\w*|given|stated|listed|shown|confirm\w*|"
    r"verifi\w*|uberpruf\w*|bestatig\w*|angegeben\w*|genannt\w*|indica\w*|"
    r"specifica\w*|detail\w*)(?![\w-])"
)
_CHECK = re.compile(
    r"(?<![\w-])(?:check|verify|confirm|look up|verifiez|verifier|consultez|"
    r"consulter|prufen|uberprufen|compruebe|comprueba|consulte|verifique|"
    r"verifica|controlli|controlla|consulta)(?![\w-])"
)
_PLANNER = re.compile(
    r"(?<![\w-])(?:planner|planificateur|calculateur|journey|itinerair\w*|"
    r"fahrplan\w*|reiseauskunft|auskunft|planificador|pianificatore|app|"
    r"application|site|website|operator\w*|exploitant\w*|transporteur\w*|"
    r"verkehrsbetrieb\w*|betreiber\w*|operador\w*|operatore)(?![\w-])"
)


def gap(folded: str) -> bool:
    """Whether a sentence says what could not be found or where to check it."""
    return bool(
        (_NEGATION.search(folded) and _SOURCE_WORDS.search(folded))
        or (_CHECK.search(folded) and _PLANNER.search(folded))
    )


# A line, train or platform identifier after its word ("ligne B", "RER A",
# "TER 3105", "Gleis 12"), or a code alone ("S8", "U3", "M14").
_LINE_WORD = re.compile(
    r"(?<![\w-])("
    + "|".join(sorted(map(re.escape, TRANSIT_WORDS), key=len, reverse=True))
    + r")\.?[  ]+(?:(?:n[°o]|nr|no)\.?[  ]*)?"
)
# Not a clock time or a quantity: "train 06:38", "bus 5 min".
_LINE_ID = re.compile(
    r"(\d{1,5}[a-z]?|[A-Z]{1,3}\d{0,4})(?![\w-])"
    r"(?![ \u00a0]?(?::|h\b|h ?\d|min|km|m\b|%|€|\$))"
)
_CODE = re.compile(r"(?<![\w-])([A-Z]{1,3})(\d{1,4})[a-z]?(?![\w-])")
_TOKEN = re.compile(r"[^\W\d_]+(?:[-‐][^\W\d_]+)*")
_MARKERS = re.compile(r"[\s\-*+>#|.)\d_]*")

# Prepositions, question words and the verbs of a trip request, five
# languages: they open sentences ("From Landshuter Allee, take ...") and tie no
# page to a trip (staging -102).
FUNCTION_WORDS = frozenset(
    (
        "from into onto over under near next via toward towards until till need "
        "needs want would could should will must have going get getting go come "
        "back home where when what which how there here then "
        "depuis vers chez jusqu dois doit devons veux voudrais faut aller venir "
        "rentrer comment quand quel quelle quels quelles avec sans dans sous pres "
        "von vom aus bei mit ohne uber unter neben nach zum zur bis wie wann wo "
        "welche muss mochte will fahren kommen "
        "desde hacia hasta para con sin cerca donde cuando cual quiero "
        "necesito tengo debo voy llegar "
        "dal dalla dallo verso fino con senza vicino dove quando come quale voglio "
        "devo vado andare arrivare"
    ).split()
)
_GENERIC = (
    FUNCTION_WORDS
    | COMMON
    | STOP
    | TITLES
    | frozenset(normalized(w) for w in TRANSIT_WORDS)
    | frozenset(
        (
            "direction towards toward richtung direccion direzione terminus "
            "endstation sie ihr ihre you vous nous wir option etape step leg "
            "abschnitt tramo tratta am im an lundi mardi mercredi jeudi vendredi samedi "
            "dimanche monday tuesday wednesday thursday friday saturday sunday "
            "montag dienstag mittwoch donnerstag freitag samstag sonntag"
        ).split()
    )
)


def fold(text: str) -> str:
    """Lower case without accents, one character for one (offsets are kept)."""
    out: list[str] = []
    for character in text:
        lower = character.lower()
        if len(lower) != 1:
            lower = character
        base = [
            c
            for c in unicodedata.normalize("NFKD", lower)
            if not unicodedata.combining(c)
        ]
        out.append(base[0] if base else lower)
    return "".join(out)


@dataclass(frozen=True)
class Anchor:
    kind: str  # line, name, direct, frequency, clock
    key: str
    text: str
    patterns: tuple[re.Pattern[str], ...]
    position: int = field(default=-1, compare=False)


@dataclass
class Unit:
    start: int
    end: int  # the final mark of a sentence, or the end of its text
    row: bool = False
    anchors: list[Anchor] = field(default_factory=list)
    route: bool = False
    flavour: bool = False  # transit words or a frequency, whatever the anchors
    disclaimer: bool = False
    gap: bool = False
    parentheses: list[tuple[int, int, list[Anchor]]] = field(default_factory=list)
    cells: list[tuple[int, list[Anchor]]] = field(default_factory=list)
    computed: bool = False


@dataclass
class Finding:
    """What the travel check leaves out: spans to remove and what they stated."""

    # (start, end, what it stated); a sentence that only continued a removed
    # one states nothing of its own.
    spans: list[tuple[int, int, list[str]]] = field(default_factory=list)
    # Spans left out because the pages read name another line for the route.
    disputed: int = 0

    @property
    def route(self) -> bool:
        """A line, a stop, a direction or a direct service is left out."""
        return any(labels for _, _, labels in self.spans)

    @property
    def labels(self) -> list[str]:
        return [label for _, _, labels in self.spans for label in labels]


def _word(token: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(token)}(?!\w)")


_TRANSIT_N = "|".join(
    sorted({normalized(w) for w in TRANSIT_WORDS}, key=len, reverse=True)
)


def _line_patterns(identifier: str) -> tuple[re.Pattern[str], ...]:
    value = normalized(identifier).replace(" ", "")
    code = re.fullmatch(r"([a-z]{1,3})(\d{1,4})", value)
    if code:
        return (re.compile(rf"(?<!\w){code[1]} ?{code[2]}(?!\w)"),)
    escaped = re.escape(value)
    forms = [
        rf"(?<!\w)(?:{_TRANSIT_N})(?: \w+)? {escaped}(?!\w)",
        rf"(?<!\w){escaped} (?:{_TRANSIT_N})(?!\w)",
    ]
    if value.isdigit():
        forms.append(rf"(?<!\w)[a-z]{{1,3}}{escaped}(?!\w)")
    return (re.compile("|".join(forms)),)


def _clock_pattern(minutes: float) -> re.Pattern[str]:
    hours, rest = divmod(int(minutes) % 1440, 60)
    twelve = hours % 12 or 12
    suffix = "p" if hours >= 12 else "a"
    return re.compile(
        rf"(?<!\d)0?{hours} ?(?:h ?)?{rest:02d}(?!\d)"
        rf"|(?<!\d){twelve}(?: ?{rest:02d})? ?{suffix} ?m(?!\w)"
    )


_DIRECT_N = re.compile(
    r"(?<!\w)(?:direct\w*|direkt\w*|dirett\w*|nonstop|non stop|sans changement|"
    r"sans correspondance|0 correspondance|without chang\w*|no change\w*|"
    r"ohne umsteigen|sin transbordo\w*|sin cambio\w*|senza cambi\w*)"
)
_FREQUENCY_N = re.compile(
    r"(?<!\w)(?:every|toutes les|tous les|alle|cada|ogni) \w+ (?:\w+ )?"
    r"(?:min\w*|heures?|hours?|stunden?|horas?|ore)(?!\w)"
    r"|(?<!\w)(?:frequen\w*|frecuen\w*|takt\w*|cadenc\w*)"
)


class Sources:
    """The turn's texts, normalized once: passages, their words, anchor hits."""

    def __init__(
        self,
        passages: Iterable[str],
        own: Iterable[str],
        given: Iterable[str] = (),
    ) -> None:
        self.passages = [p for p in (normalized(t) for t in passages) if p]
        self.words = frozenset(" ".join(self.passages).split())
        # The starting point the person typed or their device gave: known.
        self.given = frozenset(normalized(" ".join(given)).split())
        self.own = frozenset(normalized(" ".join(own)).split()) | self.given
        self._hits: dict[str, list[list[int]]] = {}

    def hits(self, pattern: re.Pattern[str]) -> list[list[int]]:
        key = pattern.pattern
        if key not in self._hits:
            self._hits[key] = [
                [m.start() for m in pattern.finditer(p)] for p in self.passages
            ]
        return self._hits[key]

    def _positions(self, anchor: Anchor, index: int) -> list[list[int]] | None:
        """Where the anchor's words are in a passage, None when it is absent.

        A name of three words or more is found without one of its words but
        the last (staging -106: "Madrid Puerta de Atocha" is written "Madrid
        Atocha" on a timetable and was listed as not verified).
        """
        hits = [self.hits(p)[index] for p in anchor.patterns]
        missing = [i for i, found in enumerate(hits) if not found]
        if not missing:
            return hits
        if (
            anchor.kind == "name"
            and len(hits) >= 3
            and len(missing) == 1
            and missing[0] != len(hits) - 1
        ):
            return [found for found in hits if found]
        return None

    def together(self, anchors: list[Anchor]) -> bool:
        """All anchors within one window of one passage (no anchor: true)."""
        patterns = [p for a in anchors for p in a.patterns]
        if not patterns:
            return True
        lines = [p for a in anchors if a.kind == "line" for p in a.patterns]
        lead = (
            bool(lines)
            and all(a.kind in {"line", "name"} for a in anchors)
            and any(a.kind == "name" for a in anchors)
        )
        line_hits = [self.hits(p) for p in lines]
        for index in range(len(self.passages)):
            located = [self._positions(a, index) for a in anchors]
            positions = [h for found in located if found is not None for h in found]
            if any(found is None for found in located):
                positions = [[]]
            if all(positions) and _window(positions):
                return True
            # A page about the line (it names the line in its opening) lists
            # its stops anywhere (staging -104: the T8 article named both
            # airport stations far below its lead).
            if (
                lead
                and all(positions)
                and all(any(h < LEAD for h in hits[index]) for hits in line_hits)
            ):
                return True
        return False


class Sites:
    """The pages read, grouped by site, to weigh a line number they disagree on.

    Staging -112: "The Airport Link T2 Line connects the airport to Central
    Station" came from a job-fair directions page; the operator's page and
    the airport's guide both give the T8 for those stations. A line stated
    with a route (the line and two of its places together) is kept unless
    more sites give another line of the same kind next to one of those places
    than give this one with them; a site naming both lines is no vote (a
    station page listing every line).
    """

    def __init__(
        self,
        pages: Iterable[tuple[str, str]],
        own: Iterable[str],
        given: Iterable[str] = (),
    ) -> None:
        grouped: dict[str, list[str]] = {}
        for url, text in pages:
            host = re.sub(r"^www\.", "", re.sub(r"^[a-z]+://", "", url.lower())).split(
                "/"
            )[0]
            if host and text.strip():
                grouped.setdefault(host, []).append(text)
        self._texts = grouped
        self._own = list(own)
        self._given = list(given)
        self._sources: dict[str, Sources] = {}

    def _of(self, host: str) -> Sources:
        if host not in self._sources:
            self._sources[host] = Sources(self._texts[host], self._own, self._given)
        return self._sources[host]

    def disputed(self, anchors: list[Anchor]) -> bool:
        if len(self._texts) < 2:
            return False
        for part in _segments(anchors):
            lines = [a for a in part if a.kind == "line"]
            names = [a for a in part if a.kind == "name"]
            if len(lines) != 1 or len(names) < 2:
                continue
            line = lines[0]
            code = re.fullmatch(r"([a-z]{1,3})(\d{1,4})", line.key)
            if not code:
                continue
            other = re.compile(rf"(?<!\w){code[1]} ?(\d{{1,4}})(?!\w)")
            support = 0
            rivals: dict[str, int] = {}
            for host in self._texts:
                sources = self._of(host)
                if any(found for p in line.patterns for found in sources.hits(p)):
                    support += sources.together([line, *names])
                    continue
                numbers = {
                    match[1].lstrip("0")
                    for passage in sources.passages
                    for match in other.finditer(passage)
                } - {code[2].lstrip("0")}
                for number in numbers:
                    key = f"{code[1]}{number}"
                    rival = Anchor("line", key, key, _line_patterns(key), line.position)
                    # One of the route's places with the other line is a
                    # vote: the operator's page names its own stations.
                    if any(sources.together([name, rival]) for name in names):
                        rivals[key] = rivals.get(key, 0) + 1
            if any(count > support for count in rivals.values()):
                return True
        return False


def _window(positions: list[list[int]]) -> bool:
    merged = sorted((pos, i) for i, hits in enumerate(positions) for pos in hits)
    need = len(positions)
    counts = [0] * need
    have = left = 0
    for pos, index in merged:
        counts[index] += 1
        if counts[index] == 1:
            have += 1
        while have == need:
            if pos - merged[left][0] <= WINDOW:
                return True
            first = merged[left][1]
            counts[first] -= 1
            if counts[first] == 0:
                have -= 1
            left += 1
    return False


def sentence_marks(line: str) -> list[re.Match[str]]:
    """Where sentences end in a line: a full stop, or a semicolon outside
    parentheses (two clauses: staging -102 lost "the fastest connection takes
    3:57 hours" with an unsourced "direct" before its semicolon)."""
    marks: list[re.Match[str]] = []
    for match in re.finditer(r"[.!?;](?=\s|$)", line):
        if match[0] == ";" and line.count("(", 0, match.start()) > line.count(
            ")", 0, match.start()
        ):
            continue
        marks.append(match)
    return marks


def _units(body: str) -> list[Unit]:
    """Sentences (split as the travel check splits them) and table rows."""
    units: list[Unit] = []
    offset = 0
    for line in body.split("\n"):
        if line.lstrip().startswith("|"):
            units.append(Unit(offset, offset + len(line), row=True))
        else:
            start = 0
            for match in sentence_marks(line):
                text = line[start : match.start()]
                if text.strip():
                    lead = len(text) - len(text.lstrip())
                    units.append(Unit(offset + start + lead, offset + match.start()))
                start = match.end()
            text = line[start:]
            if text.strip():
                lead = len(text) - len(text.lstrip())
                units.append(Unit(offset + start + lead, offset + len(line)))
        offset += len(line) + 1
    return units


def _names(plain: str, folded: str, start: int, end: int) -> list[Anchor]:
    """Place names in route positions, multi-word or hyphenated names."""
    tokens = list(_TOKEN.finditer(plain, start, end))
    found: list[Anchor] = []
    index = 0
    while index < len(tokens):
        first = tokens[index]
        # A capital opening the sentence ("Nehmen", "Take") or a capitalised
        # pronoun or article ("Sie", "The") starts no name.
        opening = not _MARKERS.sub("", plain[start : first.start()])
        if (
            not first[0][0].isupper()
            or normalized(first[0]) in _GENERIC
            or (opening and not _shaped(first[0]) and _breaks(tokens, index, plain))
        ):
            index += 1
            continue
        last = index
        following = index + 1
        while following < len(tokens):
            gap = plain[tokens[following - 1].end() : tokens[following].start()]
            word = tokens[following][0]
            if gap in {" ", " "} and word[0].isupper():
                if normalized(word) in _GENERIC:
                    break
                last = following
                following += 1
                continue
            connector = normalized(word) in CONNECTORS | {"sur", "sous", "am", "im"}
            if (
                gap in {" ", " "}
                and connector
                and following + 1 < len(tokens)
                and plain[tokens[following].end() : tokens[following + 1].start()]
                in {" ", " ", "'", "’"}
                and tokens[following + 1][0][0].isupper()
            ):
                last = following + 1
                following += 2
                continue
            break
        sequence = tokens[index : last + 1]
        index = following
        text = plain[first.start() : tokens[last].end()]
        # Acronyms name operators and agencies more often than places.
        words = [
            w
            for token in sequence
            if not (len(token[0]) > 1 and token[0].isupper())
            for w in normalized(token[0]).split()
            if w not in _GENERIC and len(w) > 1
        ]
        distinct = [w for w in words if w not in STATION_WORDS]
        if not distinct or all(w in PLACE_WORDS for w in distinct):
            continue
        before = plain[start : first.start()]
        initial = not _MARKERS.sub("", before)
        shaped = any(_shaped(token[0]) for token in sequence)
        multi = len(distinct) >= 2
        preceding = folded[max(start, first.start() - 30) : first.start()]
        slot = bool(_SLOT.search(preceding.replace("'", " ").replace("’", " ")))
        if initial and not (multi or shaped):
            continue
        if not (multi or shaped or slot):
            continue
        found.append(
            Anchor(
                "name",
                " ".join(distinct),
                " ".join(text.split()),
                tuple(_word(w) for w in distinct),
                first.start(),
            )
        )
    return found


def _shaped(word: str) -> bool:
    """A hyphenated name or an inner capital ("Part-Dieu", "McAllister").

    A hyphenated word whose last part is lower case is a locution, not a name
    (staging -112: a French locution for "beyond" opening a sentence was
    listed as an unverified stop).
    """
    if word.isupper():
        return False
    if "-" in word or "‐" in word:
        last = re.split(r"[-‐]", word)[-1]
        return bool(last[:1].isupper())
    return any(c.isupper() for c in word[1:])


def _breaks(tokens: list[re.Match[str]], index: int, plain: str) -> bool:
    """The opening capital is an ordinary word: no capitalised word follows it
    in the same name ("Prenez un"), or a pronoun does ("Nehmen Sie")."""
    if index + 1 >= len(tokens):
        return True
    gap = plain[tokens[index].end() : tokens[index + 1].start()]
    following = tokens[index + 1][0]
    return (
        gap not in {" ", " "}
        or not following[0].isupper()
        or normalized(following) in _GENERIC
    )


def _lines(plain: str, folded: str, start: int, end: int) -> list[Anchor]:
    found: dict[str, Anchor] = {}
    for match in _LINE_WORD.finditer(folded, start, end):
        identifier = _LINE_ID.match(plain, match.end(), end)
        # "train TER": a transit word after another is no line identifier.
        if identifier is None or fold(identifier[1]) in TRANSIT_WORDS:
            continue
        key = normalized(identifier[1]).replace(" ", "")
        text = plain[match.start() : identifier.end()]
        found.setdefault(
            key, Anchor("line", key, text, _line_patterns(key), match.start())
        )
    for match in _CODE.finditer(plain, start, end):
        key = (match[1] + match[2]).lower()
        if key not in found:
            found[key] = Anchor(
                "line", key, match[0], _line_patterns(key), match.start()
            )
    return list(found.values())


def _read(
    unit: Unit,
    plain: str,
    folded: str,
    clocks: list[tuple[int, int, float]],
    sources: Sources,
    owned_clocks: list[float],
) -> None:
    """The unit's anchors, and whether it states a route."""
    start, end = unit.start, unit.end
    text = folded[start:end]
    lines = _lines(plain, folded, start, end)
    names = _names(plain, folded, start, end)
    names = [
        name
        for name in names
        # The starting point the person or their device gave is known (staging
        # -102: "Gratte-Ciel" and the person's street were listed as not
        # verified). Another name of the person's own that no source writes (a
        # place in another language) is theirs; written by a source, it must
        # sit with the rest of the claim.
        if not (
            all(w in sources.given for w in name.key.split())
            or (
                all(w in sources.own for w in name.key.split())
                and not any(w in sources.words for w in name.key.split())
            )
        )
    ]
    route_words = bool(_ROUTE.search(text))
    transit = bool(_TRANSIT.search(text))
    # "en ligne", "online": a line word alone is no vehicle for "directement".
    vehicle = bool(_TRANSIT.search(_LINE_WORDS.sub(" ", text)))
    anchors: list[Anchor] = [*lines, *names]
    direct = _DIRECT.search(text) or (vehicle and _DIRECT_ADVERB.search(text))
    if direct:
        anchors.append(
            Anchor(
                "direct",
                "direct",
                plain[start + direct.start() : start + direct.end()],
                (_DIRECT_N,),
                start + direct.start(),
            )
        )
    frequency = _FREQUENCY.search(text)
    if frequency:
        anchors.append(
            Anchor(
                "frequency",
                "frequency",
                plain[start + frequency.start() : start + frequency.end()],
                (_FREQUENCY_N,),
                start + frequency.start(),
            )
        )
    timed = [
        (clock_start, clock_end, value)
        for clock_start, clock_end, value in clocks
        if start <= clock_start < end
        and not any(abs(value - own) < 0.5 for own in owned_clocks)
    ]
    # "arrives at Geneva Airport around 8:43" (staging -100): a place reached
    # at a time is a route fact, verb or no verb.
    unit.route = bool(
        lines or direct or frequency or (names and (route_words or timed))
    )
    unit.flavour = transit or bool(frequency)
    unit.disclaimer = bool(_DISCLAIMER.search(text))
    # A sentence naming what is missing, with no line, time or service of its
    # own, states no route.
    unit.gap = gap(text) and not (lines or direct or frequency or timed)
    if unit.gap:
        unit.route = False
    unit.anchors = anchors
    if not unit.route:
        return
    for clock_start, clock_end, value in timed:
        anchors.append(
            Anchor(
                "clock",
                f"clock:{value:g}",
                plain[clock_start:clock_end],
                (_clock_pattern(value),),
                clock_start,
            )
        )
    if unit.row:
        cells: list[tuple[int, list[Anchor]]] = []
        cell_start = start
        for match in re.finditer(r"\|", plain[start:end]):
            cell_end = start + match.start()
            inside = [a for a in anchors if _in(plain, a, cell_start, cell_end)]
            if inside and a_route(inside):
                cells.append((cell_start, inside))
            cell_start = start + match.end()
        unit.cells = cells
        return
    for match in re.finditer(r"\(([^()]*)\)", plain[start:end]):
        inner_start, inner_end = start + match.start(1), start + match.end(1)
        inside = [a for a in anchors if _in(plain, a, inner_start, inner_end)]
        if inside:
            unit.parentheses.append((inner_start, inner_end, inside))


def a_route(anchors: list[Anchor]) -> bool:
    """Anchors that state a route, not only a time (times are the values check's)."""
    return any(a.kind != "clock" for a in anchors)


def _in(plain: str, anchor: Anchor, start: int, end: int) -> bool:
    return bool(anchor.text) and anchor.text in " ".join(plain[start:end].split())


def _segments(anchors: list[Anchor]) -> list[list[Anchor]]:
    """One part per line of a transfer, the transfer stop in both.

    "Line A to Charpennes, then line B to Part-Dieu": line A's page lists
    Charpennes, line B's page lists Charpennes and Part-Dieu; no page needs to
    hold both lines. A stop line B does not serve still fails its part.
    """
    ordered = sorted(anchors, key=lambda a: a.position)
    lines = [i for i, a in enumerate(ordered) if a.kind == "line"]
    if len(lines) < 2:
        return [ordered]
    cuts = [0, *lines[1:], len(ordered)]
    parts = [ordered[a:b] for a, b in zip(cuts, cuts[1:], strict=False) if a < b]
    for previous, part in zip(parts, parts[1:], strict=False):
        line = next((a for a in previous if a.kind == "line"), None)
        stops = [
            a
            for a in previous
            if a.kind == "name" and (line is None or a.position > line.position)
        ] or [a for a in previous if a.kind == "name"]
        if stops:
            part.append(stops[-1])
    return parts


def _failing(sources: Sources, anchors: list[Anchor]) -> list[list[Anchor]]:
    return [part for part in _segments(anchors) if not sources.together(part)]


def _mine(anchor: Anchor, sources: Sources) -> bool:
    """A name the person gave (the question, the conversation, the device)."""
    return anchor.kind == "name" and all(w in sources.own for w in anchor.key.split())


_QUOTED_CHARACTERS = 90


def quoted(text: str) -> str:
    """A removed claim as the note quotes it: plain words, cut at a word."""
    plain = re.sub(r"\[([^\]\n]*+)\]\([^)\s]*+\)", r"\1", text)
    plain = " ".join(re.sub(r"[*_`#>|]+", " ", plain).split()).strip(" -–,;:")
    if len(plain) > _QUOTED_CHARACTERS:
        plain = plain[:_QUOTED_CHARACTERS].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return f"“{plain}”" if plain else ""


def _claim(body: str, start: int, end: int, part: list[Anchor]) -> str:
    """The words of a sentence that state a failing part: from the clause
    before its first anchor to its last anchor."""
    placed = [a for a in part if a.position >= 0 and a.text]
    if not placed:
        return body[start:end]
    first = min(a.position for a in placed)
    last = max(a.position + len(a.text) for a in placed)
    marks = [m.end() for m in re.finditer(r"[,;:(]\s*", body[start:first])]
    return body[start + (marks[-1] if marks else 0) : max(last, first)]


def _labels(
    anchors: list[Anchor],
    sources: Sources,
    body: str = "",
    span: tuple[int, int] = (0, 0),
) -> list[str]:
    """What the note lists: the claims of the parts no passage holds, quoted
    from the sentence when it is known.

    Without the sentence, a name no page writes is listed; a name the person gave never is (staging
    -102 listed the person's own stop as not verified). When every name is
    known on its own, what is not verified is the connection the sentence
    states: the note quotes it (staging -109 listed "Barcelona Sants – Madrid
    Puerta de Atocha – Almudena Grandes – Cercanías", stations the pages do
    name, as if they were the unverified claim). Without the sentence, its
    names then its times are joined.
    """
    failing = _failing(sources, anchors) or [anchors]
    if body and span[1] > span[0]:
        # The note quotes claims, never a lone word cut from one (staging
        # -112 listed a locution after a quoted claim).
        part = [a for p in failing for a in p]
        if claim := quoted(_claim(body, span[0], span[1], part)):
            return [claim]
    stated = [a for part in failing for a in part if a.kind != "clock"]
    alone = [
        a.text for a in stated if not _mine(a, sources) and not sources.together([a])
    ]
    if alone:
        return alone
    joined = [a for part in failing for a in sorted(part, key=lambda a: a.position)]
    names = list(dict.fromkeys(a.text for a in joined if a.kind != "clock"))
    times = list(dict.fromkeys(a.text for a in joined if a.kind == "clock"))
    if not names:
        return times
    return [" – ".join(names) + (f" ({', '.join(times)})" if times else "")]


def review(
    body: str,
    plain: str,
    clocks: list[tuple[int, int, float]],
    passages: Iterable[str],
    own: Iterable[str],
    computed: Iterable[str],
    owned_clocks: list[float],
    given: Iterable[str] = (),
    pages: Iterable[tuple[str, str]] = (),
) -> Finding:
    """The route units no source of the turn supports, and what follows them.

    ``plain`` is ``body`` with code and link targets blanked (same offsets);
    ``clocks`` are the clock times read in ``body`` (start, end, minutes);
    ``pages`` are the texts read with their address (``Sites``).
    """
    sources = Sources(passages, own, given)
    sites = Sites(pages, own, given)
    folded = fold(plain)
    computed_text = "\n".join(computed)
    finding = Finding()
    units = _units(body)
    removed: list[bool] = []
    for unit in units:
        line_start = body.rfind("\n", 0, unit.start) + 1
        line_end = body.find("\n", unit.start)
        line = body[line_start : len(body) if line_end < 0 else line_end].strip()
        if len(line) > 10 and line in computed_text:
            # The journey plan code wrote it (journey.py).
            unit.computed = True
            removed.append(False)
            continue
        _read(unit, plain, folded, clocks, sources, owned_clocks)
        if not unit.route or not a_route(unit.anchors):
            removed.append(False)
            continue
        if not _failing(sources, unit.anchors):
            if unit.row or not sites.disputed(unit.anchors):
                removed.append(False)
                continue
            finding.disputed += 1
            finding.spans.append(
                (
                    unit.start,
                    unit.end,
                    [quoted(body[unit.start : unit.end])],
                )
            )
            removed.append(True)
            continue
        if unit.row:
            labels = _labels(unit.anchors, sources, body, (unit.start, unit.end))
            for cell_start, _ in unit.cells:
                finding.spans.append((cell_start, cell_start + 1, labels))
                labels = []
            removed.append(False)
            continue
        outside = [
            a
            for a in unit.anchors
            if not any(a in inside for _, _, inside in unit.parentheses)
        ]
        alone = [
            (inner_start, inner_end, inside)
            for inner_start, inner_end, inside in unit.parentheses
            if a_route(inside) and _failing(sources, outside + inside)
        ]
        if alone and (not a_route(outside) or not _failing(sources, outside)):
            # The rest of the sentence holds: only its unsourced parentheses go.
            for inner_start, inner_end, inside in alone:
                finding.spans.append(
                    (
                        inner_start,
                        inner_end,
                        _labels(inside, sources, body, (inner_start, inner_end)),
                    )
                )
            removed.append(False)
            continue
        finding.spans.append(
            (
                unit.start,
                unit.end,
                _labels(unit.anchors, sources, body, (unit.start, unit.end)),
            )
        )
        removed.append(True)
    if not finding.spans:
        return finding
    # A sentence that only continues a removed one ("Trains run every few
    # minutes.", "This leg is from general knowledge.") goes with it, and a
    # sentence saying its content is unsourced is replaced by the note.
    previous_removed = False
    for unit, gone in zip(units, removed, strict=True):
        # A heading, markdown or bold-only ("**2. Train**"), heads what
        # follows; it never continues the sentence before it.
        line_start = body.rfind("\n", 0, unit.start) + 1
        line_end = body.find("\n", unit.start)
        line = body[line_start : len(body) if line_end < 0 else line_end].strip()
        heading = line.startswith("#") or bool(
            re.fullmatch(r"(\*\*|__)[^*_]+(\*\*|__)\s*:?", line)
        )
        if gone:
            previous_removed = True
            continue
        if unit.computed or unit.row:
            previous_removed = False
            continue
        if not body[unit.start : unit.end].strip(" \n*_-.0123456789)"):
            continue
        # Its own lines, places or times: a sentence of its own, not a
        # continuation (staging -102: sourced ICE times went with a removed
        # "direct" before them).
        own_facts = any(a.kind in {"line", "name"} for a in unit.anchors) or any(
            unit.start <= start < unit.end for start, _, _ in clocks
        )
        if unit.disclaimer or (
            previous_removed
            and not heading
            and unit.flavour
            and not own_facts
            and not unit.gap
        ):
            finding.spans.append((unit.start, unit.end, []))
            previous_removed = True
            continue
        previous_removed = False
    return finding


def mentions(body: str, plain: str) -> list[str]:
    """Line identifiers and place names of the route sentences of ``body``.

    The unsourced details review leaves them to the travel check, so a route
    fact is checked (and left out) once.
    """
    sources = Sources([], [])
    folded = fold(plain)
    texts: list[str] = []
    for unit in _units(body):
        _read(unit, plain, folded, [], sources, [])
        if unit.route:
            texts += [a.text for a in unit.anchors if a.kind in {"line", "name"}]
    return texts
