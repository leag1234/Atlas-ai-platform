"""Deterministic checks for precise claims in same-conversation evidence."""

from bisect import bisect_right
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import re
import time
import unicodedata

from services.orchestrator.link_requests import own_words, split_pasted


def normalized(text: str) -> str:
    text = re.sub(r"(?i)https?://\S+|`[^`]*`|\[(?:source\s+)?\d+\]", "", text)
    folded = unicodedata.normalize("NFKD", text.casefold())
    return " ".join(
        re.findall(
            r"\w+",
            "".join(
                character
                for character in folded
                if not unicodedata.combining(character)
            ),
        )
    )


STOP = frozenset(
    "a an the of to in on for by with was were is are it its this that and or "
    "as at from has have had which who why when how did does not no "
    "le la les l un une des du de d en au aux pour par avec et ou est sont "
    "a ete etait ont qui que qu quel quelle pourquoi quand comment il elle "
    "ce cet cette ces son sa ses pas ne dans".split()
)
UNCERTAIN = re.compile(
    r"\b(?:cannot|can t|could not|unable to|"
    r"no (?:reliable |verified )?(?:source|evidence|record)|"
    r"ne peux pas|ne peut pas|impossible de|"
    r"aucun\w* (?:source|preuve|trace)|je ne sais pas)\b"
)
ATTRIBUTION = re.compile(
    r"\b(?:author\w*|wrote|written|publish\w*|novel|book|"
    r"auteur\w*|ecrit\w*|publie\w*|edite\w*|roman|livre)\b"
)
QUOTED = re.compile(r'["“«]([^"”»\n]{2,160})["”»]')
NUMBER = re.compile(
    r"(?<!\w)\d+(?:[.,]\d+)?(?:\s?%|\s?(?:million|billion|milliard)s?)?\b"
)
YEAR = re.compile(r"\b(?:1[0-9]{3}|20[0-9]{2})\b")
CREATIVE = re.compile(
    r"^(?:(?:please|can you|could you|peux tu|pourrais tu|pouvez vous) )?"
    r"(?:imagine|invent\w*|translate|tradui\w*|rewrite|reformul\w*|"
    r"correct this|corrige ce|write a fictional|ecris une fiction)\b"
)
RELATIONS = (
    r"\b(?:open\w*|inaugur\w*|ouvert\w*|ouvri\w*)\b",
    r"\b(?:built|build\w*|construct\w*|construi\w*)\b",
    r"\b(?:publish\w*|publie\w*|edit\w*|parution|paru\w*)\b",
    r"\b(?:born|birth|naiss\w*|ne|nee)\b",
    r"\b(?:found(?:ed|ing)|fond(?:e|ee|ation))\b",
)


def terms(text: str) -> set[str]:
    folded = normalized(text)
    for index, relation in enumerate(RELATIONS):
        folded = re.sub(relation, f"relation{index}", folded)
    aliases = dict(
        zip(
            "janvier fevrier mars avril mai juin juillet aout septembre octobre novembre decembre auteur editeur roman livre".split(),
            "january february march april may june july august september october november december author publisher novel book".split(),
            strict=True,
        )
    )
    return {aliases.get(word, word) for word in folded.split()} - STOP


def small_talk(question: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:hello|hi|hey|bonjour|salut|merci|thanks|thank you|how are you|"
            r"comment vas tu|ca va|guten tag|hallo|hola|ciao)",
            normalized(question),
        )
    )


# User-facing wording added by grounding, in the five UI languages (default English).
WORDING: dict[str, dict[str, str]] = {
    "missing": {
        "en": "No retrieved source mentions this entity.",
        "fr": "Aucune source consultée ne mentionne cette entité.",
        "de": "Keine abgerufene Quelle erwähnt diese Entität.",
        "es": "Ninguna fuente consultada menciona esta entidad.",
        "it": "Nessuna fonte consultata menziona questa entità.",
    },
    "contradicted": {
        "en": "A retrieved source contradicts this claim:",
        "fr": "Une source consultée contredit cette affirmation :",
        "de": "Eine abgerufene Quelle widerspricht dieser Aussage:",
        "es": "Una fuente consultada contradice esta afirmación:",
        "it": "Una fonte consultata contraddice questa affermazione:",
    },
    "contradicted_item": {
        "en": "A retrieved source contradicts {claim}:",
        "fr": "Une source consultée contredit {claim} :",
        "de": "Eine abgerufene Quelle widerspricht {claim}:",
        "es": "Una fuente consultada contradice {claim}:",
        "it": "Una fonte consultata contraddice {claim}:",
    },
    "premise_unsupported": {
        "en": "The question's premise is contradicted by a retrieved source.",
        "fr": "La prémisse de la question est contredite par une source consultée.",
        "de": "Die Prämisse der Frage wird durch eine abgerufene Quelle widerlegt.",
        "es": "La premisa de la pregunta es contradicha por una fuente consultada.",
        "it": "La premessa della domanda è contraddetta da una fonte consultata.",
    },
    "premise_incorrect": {
        "en": "The premise is incorrect: the retrieved source gives {date}, not {old}.",
        "fr": "La prémisse est incorrecte : la source consultée indique {date}, et non {old}.",
        "de": "Die Prämisse ist falsch: Die abgerufene Quelle nennt {date}, nicht {old}.",
        "es": "La premisa es incorrecta: la fuente consultada indica {date}, no {old}.",
        "it": "La premessa è errata: la fonte consultata indica {date}, non {old}.",
    },
}


def wording(key: str, language: str) -> str:
    table = WORDING[key]
    return table.get(language, table["en"])


_HEADING = re.compile(
    r"^[ \t]*+(?:#{1,6}+[ \t]|(?:\*\*|__)[^*_\n]++(?:\*\*|__)[ \t]*+:?+[ \t]*+$)"
)
_ITEM = re.compile(r"^[ \t]*+(?:[-*+]|\d{1,3}+[.)])[ \t]++")


def _section(text: str, position: int) -> int:
    """The answer section a position is in: headings counted before it."""
    return sum(1 for line in text[:position].split("\n")[:-1] if _HEADING.match(line))


def _list_item(text: str, position: int) -> bool:
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    return bool(_ITEM.match(text[start : len(text) if end < 0 else end]))


def _list_end(text: str, position: int) -> int:
    """The end of the list a list item belongs to (its last item's line)."""
    end = text.find("\n", position)
    while end >= 0:
        following = text.find("\n", end + 1)
        line = text[end + 1 : len(text) if following < 0 else following]
        if not _ITEM.match(line) and not (
            line.startswith((" ", "\t")) and line.strip()
        ):
            return end
        end = following
    return len(text)


def _quoted_claim(text: str, limit: int = 90) -> str:
    plain = re.sub(r"\[([^\]\n]*)\]\([^)\s]*\)", r"\1", text)
    plain = " ".join(re.sub(r"[*_`#>|]+", " ", _ITEM.sub("", plain)).split())
    plain = plain.strip(" -–,;:.")
    if len(plain) > limit:
        plain = plain[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return f"“{plain}”"


@dataclass(frozen=True)
class Claim:
    start: int
    end: int
    text: str
    values: tuple[str, ...]


def extract_claims(text: str) -> list[Claim]:
    """Extract sentence claims while preserving offsets and fenced code."""
    text = text.split("<details>", 1)[0]
    fences = [
        (match.start(), match.end())
        for match in re.finditer(r"```[\s\S]*?(?:```|$)", text)
    ]
    fence_starts = [start for start, _ in fences]
    claims: list[Claim] = []
    for match in re.finditer(r"[^\n]+?(?:[.!?](?=\s|$)|(?=\n|$))", text):
        sentence = match[0]
        # Fences are sorted and disjoint: one lookup, not a pass over all of
        # them for every sentence (quadratic on many fences).
        fence = bisect_right(fence_starts, match.start()) - 1
        if fence >= 0 and match.start() < fences[fence][1]:
            continue
        plain = normalized(sentence)
        if (
            sentence.rstrip().endswith("?")
            or sentence.lstrip().startswith((">", "http", "<"))
            or UNCERTAIN.search(plain)
            or re.fullmatch(r"[\s\d+*/=().,−-]+", sentence)
        ):
            continue
        precision = re.sub(
            r"(?i)https?://[^\s)>]+|`[^`]*`|\[(?:source\s+)?\d+\]", "", sentence
        )
        values = []
        for number in NUMBER.finditer(precision):
            prefix = normalized(precision[max(0, number.start() - 24) : number.start()])
            if re.search(
                r"\b(?:not|pas|ni|instead of|plutot que)(?: en| in)?$", prefix
            ):
                continue
            values.append(normalized(number[0]))
        values.extend(normalized(match[1]) for match in QUOTED.finditer(precision))
        attribution = ATTRIBUTION.search(plain)
        if attribution:
            values.extend(
                normalized(name)
                for name in re.findall(r"\b[A-ZÀ-Ý][\wÀ-ÿ]+", sentence)
                if normalized(name) not in STOP
            )
        if values or attribution:
            claims.append(Claim(match.start(), match.end(), sentence, tuple(values)))
    return claims


def premise(question: str) -> bool:
    text = normalized(question)
    return bool(
        not CREATIVE.search(text)
        and re.search(
            r"\b(?:why|pourquoi|how did|comment|since|puisque|now that)\b", text
        )
        and (YEAR.search(text) or ATTRIBUTION.search(text))
    )


# Questions asking for a precise fact (who, when, where, how many...) in the five
# UI languages, after normalized() (casefolded, accents removed).
FACT_CUE = re.compile(
    r"\b(?:who|when|where|which year|what year|how many|how much|how long|"
    r"qui|quand|quel|quelle|quels|quelles|combien|en quelle annee|"
    r"wer|wann|wo|welche|welcher|welches|wie viele|wie viel|"
    r"quien|cuando|donde|cual|cuanto|cuanta|cuantos|cuantas|"
    r"chi|quando|dove|quale|quali|quanto|quanta|quanti|quante)\b"
)


def needs_grounding(question: str) -> bool:
    """Decide before any streaming whether a turn's answer is held and checked."""
    text = normalized(question)
    if not text or CREATIVE.search(text) or small_talk(question):
        return False
    return bool(
        premise(question)
        or FACT_CUE.search(text)
        or YEAR.search(question)
        or NUMBER.search(question)
    )


def factual_query(question: str) -> str:
    """Return a neutral query for a question carrying a factual premise."""
    text = normalized(question)
    if CREATIVE.search(text):
        return ""
    if not premise(question):
        return ""
    question = re.sub(
        r"(?i)\b(?:why|pourquoi|how did|comment|since|puisque)\b", "", question
    )
    question = YEAR.sub("", question)
    return question.strip()[:500]


QUESTION_WORDS = frozenset(
    "what where which whom whose who when why how quel quelle quels quelles ou".split()
)


def question_entities(question: str) -> list[str]:
    """Quoted titles, else runs of consecutive capitalised words (stop words trimmed).

    A lone capitalised word opening a sentence is skipped.
    """
    quoted = [normalized(match[1]) for match in QUOTED.finditer(question)]
    if quoted:
        return quoted
    entities = []
    for run in re.finditer(
        r"\b[A-ZÀ-Ý][\wÀ-ÿ]*(?:[-'][\wÀ-ÿ]+)*(?:\s+[A-ZÀ-Ý][\wÀ-ÿ]*(?:[-'][\wÀ-ÿ]+)*)*",
        question,
    ):
        before = question[: run.start()].rstrip()
        if (not before or before[-1] in ".?!:") and " " not in run[0].strip():
            # A single capitalised word opening a sentence is usually an ordinary word
            # ("Donne tes sources."), not a name: it must not trigger the missing notice.
            continue
        words = normalized(run[0]).split()
        while words and (words[0] in STOP or words[0] in QUESTION_WORDS):
            words.pop(0)
        while words and words[-1] in STOP:
            words.pop()
        if words:
            entities.append(" ".join(words))
    return entities


# What the person gave is a source of the answer (owner rule 2026-11-18): a
# pasted text, an attached file, and what they say about themselves ("I'm at
# 12 Harbour Road", "Estoy en la Calle de Alcala 20"). What they ask about is
# not: a question or a request names the subject to check. A clause states
# something about the person when it names them (first person) and neither
# asks nor reports a belief to check ("I think it opened in 1925").
FIRST_PERSON = frozenset(
    "i im ive me my mine myself we our ours us "
    "je j moi mon ma mes nous notre nos suis "
    "ich mich mir mein meine meinem meinen meiner meines wir uns unser unsere "
    "yo estoy estamos soy vivo vivimos mi mis nuestro nuestra nuestros nuestras "
    "io sono siamo abito mio mia miei mie nostro nostra nostri nostre".split()
)
ASKING = re.compile(
    r"\b(?:what|whats|why|how|hows|tell me|explain|describe|summari[sz]e|give me|"
    r"show me|find|list|can you|could you|is it|is that|true|"
    r"think|believe|heard|read that|guess|suppose|"
    r"quoi|pourquoi|comment|dis moi|explique|decris|resume|donne moi|montre moi|"
    r"trouve|peux tu|pouvez vous|est ce que|est ce|vrai|pense|crois|ai lu|entendu|"
    r"warum|wie|was ist|was sind|sag mir|erklar\w*|beschreib\w*|zeig mir|gib mir|"
    r"finde|kannst du|konnen sie|stimmt|glaube|denke|gelesen|gehort|"
    r"que es|por que|como|dime|explica|dame|muestra|busca|puedes|verdad|creo|"
    r"pienso|lei|oido|"
    r"perche|come|cosa|dimmi|spiega|descrivi|riassumi|dammi|mostra|trova|puoi|"
    r"vero|credo|penso|letto|sentito)\b"
)
_CLAUSE = re.compile(
    r"([\u00bf\u00a1]?+)((?:[^.!?;:,\n\u00bf\u00a1]|[.,:](?=\S))*+)([.!?;:,\n]?+)"
)


def statements(text: str) -> list[str]:
    """The clauses of the person's own words that state something about them.

    A clause naming the person starts a statement; the clauses after it in the
    same sentence continue it ("I'm at 5 Elm Street, Brennholm") until one asks.
    """
    found: list[str] = []
    stating = False
    for match in _CLAUSE.finditer(text):
        opening, body, end = match[1], match[2], match[3]
        plain = normalized(body)
        if plain:
            if (
                opening == "\u00bf"
                or end == "?"
                or ASKING.search(plain)
                or FACT_CUE.search(plain)
            ):
                stating = False
            elif stating or FIRST_PERSON.intersection(plain.split()):
                found.append(body)
                stating = True
        if end in {".", "!", "?", "\n"} or opening == "\u00bf":
            stating = False
    return found


def provided(messages: Sequence[str], documents: Sequence[str] = ()) -> tuple[str, str]:
    """What the person gave, normalized: (pasted texts and files, statements)."""
    texts = list(documents)
    stated: list[str] = []
    for message in messages:
        own, pasted = split_pasted(message)
        texts.extend(pasted)
        stated.extend(statements(own))
    return (
        normalized("\n".join(texts)[:400_000]),
        normalized("\n".join(stated)[:20_000]),
    )


def _names(text: str, phrase: str) -> bool:
    return (
        bool(phrase) and re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None
    )


# A name followed by its number names one item among others of its kind: a
# train ("Nozomi 233", "ICE 597"), a flight, a line, a gate. Never a date
# ("November 18"), a year, a clock time or a quantity.
DESIGNATOR = re.compile(
    r"(?<![\w-])([A-ZÀ-Ý][\wÀ-ÿ'-]*)\s+(?:n[°o]\.?\s*|nr\.?\s*|#\s*)?"
    r"(\d{1,5}[a-zA-Z]?)(?![\w-])(?!\s?(?::\d|[.,]\d|h\b|h\d|min|km|%))"
)
_CALENDAR = frozenset(
    "january february march april may june july august september october "
    "november december janvier fevrier mars avril mai juin juillet aout "
    "septembre octobre novembre decembre januar februar marz juni juli "
    "oktober dezember enero febrero marzo abril mayo junio julio agosto "
    "septiembre setiembre octubre noviembre diciembre gennaio febbraio marzo "
    "aprile maggio giugno luglio settembre ottobre dicembre monday tuesday "
    "wednesday thursday friday saturday sunday lundi mardi mercredi jeudi "
    "vendredi samedi dimanche montag dienstag mittwoch donnerstag freitag "
    "samstag sonntag lunes martes miercoles jueves viernes sabado domingo "
    "lunedi martedi mercoledi giovedi venerdi sabato domenica".split()
)


def designators(text: str) -> set[tuple[str, str]]:
    """The numbered items a sentence names, as (name, number)."""
    found: set[tuple[str, str]] = set()
    for match in DESIGNATOR.finditer(text):
        name = normalized(match[1])
        number = normalized(match[2])
        if not name or name in STOP or name in _CALENDAR or YEAR.fullmatch(number):
            continue
        found.add((name, number))
    return found


@dataclass
class Grounding:
    evidence: list[str] = field(default_factory=list)
    characters: int = 0
    claims_checked: int = 0
    claims_changed: int = 0
    premises_checked: int = 0
    latency_ms: float = 0.0

    def add(self, text: str) -> None:
        text = text[: max(0, 128000 - self.characters)]
        if text.strip():
            self.evidence.append(text)
            self.characters += len(text)

    def collect(self, value: object) -> None:
        """Collect only source-bearing fields, never tool arguments or errors."""
        if isinstance(value, dict):
            if value.get("error"):
                return
            if "computed_tables" in value:
                self.add(json.dumps(value["computed_tables"], ensure_ascii=True))
            text = " ".join(
                item
                for key, item in value.items()
                if key in {"title", "snippet", "text", "content"}
                and isinstance(item, str)
            )
            self.add(text)
            for key in ("data", "results", "passages"):
                if key in value:
                    self.collect(value[key])
        elif isinstance(value, list):
            for row in value:
                self.collect(row)

    def contradicted(self, claim: Claim) -> str:
        if not claim.values:
            return ""
        content = terms(claim.text) - set(" ".join(claim.values).split())
        # One shared word does not make two sentences about the same thing
        # (staging -112: "Nozomi 1: 06:00 → 08:08" was "contradicted" by
        # "Nozomi 233 · 07:03 → 09:37", another train of the same timetable).
        if len(content) < 2 and not designators(claim.text):
            return ""
        own = designators(claim.text)
        claim_values = set(claim.values)
        supporting_passages = [
            passage
            for passage in self.evidence
            if all(
                re.search(r"\b" + re.escape(value) + r"\b", normalized(passage))
                for value in claim_values
            )
        ]
        if supporting_passages:
            return ""
        # A contradiction is about the same thing: a source sentence that does
        # not name the claim's own entities says nothing about it (a city news
        # headline was quoted under a bakery's opening hours, staging 2026-11-14).
        entities = question_entities(claim.text)
        for passage in self.evidence:
            for match in re.finditer(r"[^\n]+?(?:[.!?](?=\s|$)|(?=\n|$))", passage):
                source_sentence = match[0].strip()
                source = normalized(source_sentence)
                if re.search(
                    r"\b(?:not|never|pas|jamais|aucun\w*|false|incorrect|faux)\b",
                    source,
                ):
                    continue
                source_values = {
                    normalized(number[0])
                    for number in NUMBER.finditer(source_sentence)
                    if not re.search(
                        r"\b(?:not|pas|ni|instead of|plutot que)(?: en| in)?$",
                        normalized(
                            source_sentence[
                                max(0, number.start() - 24) : number.start()
                            ]
                        ),
                    )
                }
                source_values.update(
                    normalized(quoted[1]) for quoted in QUOTED.finditer(source_sentence)
                )
                # A numbered item (a train, a flight, a line) is its own
                # entity: the source must name the same one.
                if own and not own <= designators(source_sentence):
                    continue
                source_values -= {value for _, value in own}
                if not source_values or source_values & claim_values:
                    continue
                if not all(
                    re.search(r"\b" + re.escape(entity) + r"\b", source)
                    for entity in entities
                ):
                    continue
                overlap = content & terms(source_sentence)
                if content and overlap and len(overlap) / len(content) >= 0.75:
                    return source_sentence.strip()
        return ""

    def correction(self, question: str, language: str) -> str:
        years = set(YEAR.findall(question))
        if len(years) != 1:
            return ""
        relation = next(
            (
                pattern
                for pattern in RELATIONS
                if re.search(pattern, normalized(question))
            ),
            "",
        )
        names = {
            normalized(name)
            for name in re.findall(r"\b[A-ZÀ-Ý][\wÀ-ÿ]+", question)
            if normalized(name) not in STOP
        }
        if not relation or not names:
            return ""
        candidates: dict[str, str] = {}
        for passage in self.evidence:
            for match in re.finditer(r"[^\n]+?(?:[.!?](?=\s|$)|(?=\n|$))", passage):
                text = normalized(match[0])
                dates = set(YEAR.findall(text))
                if (
                    len(dates) == 1
                    and names <= set(text.split())
                    and re.search(relation, text)
                    and not re.search(r"\b(?:not|never|pas|jamais)\b", text)
                ):
                    candidates[next(iter(dates))] = match[0].strip()
        if len(candidates) != 1 or years == set(candidates):
            return ""
        date = next(iter(candidates))
        old = next(iter(years))
        return (
            wording("premise_incorrect", language).format(date=date, old=old)
            + "\n\n> "
            + candidates[date]
        )

    def missing_entities(self, question: str) -> list[str]:
        """Entities named by the question that appear in no fetched passage."""
        passages = [normalized(passage) for passage in self.evidence]
        return [
            entity
            for entity in question_entities(question)
            if not any(
                re.search(r"\b" + re.escape(entity) + r"\b", passage)
                for passage in passages
            )
        ]

    def entity_missing(self, question: str) -> bool:
        """True when some entity named by the question appears in no fetched passage."""
        return bool(self.missing_entities(question))

    def check(
        self,
        answer: str,
        question: str,
        language: str,
        *,
        messages: Sequence[str] = (),
        documents: Sequence[str] = (),
    ) -> str:
        """Notes follow the claim they qualify, in ``language``; never a loose line.

        Staging 2026-11-15: "No retrieved source mentions this entity." was put on
        top of French answers, in English and attached to nothing, because the
        question it read had been rewritten by the service (a position note). A
        note now exists only next to a precise claim of the answer that it is
        about: a contradiction quotes a source sentence naming the claim's own
        entities, a missing-entity note follows the first claim naming that
        entity. Without such a claim there is no note.

        What the person gave is a source (staging 2026-11-18: a summary of a
        pasted council report got "No retrieved source mentions this entity."
        after its first sentence, and a walking route the same note after the
        address the person gave). ``messages`` are the person's messages (the
        question alone by default), ``documents`` the files they provided. The
        question's entities are read in the person's own words, never in a
        pasted text; an entity a pasted text, a file or the person's statement
        about themselves names is never missing; a claim whose precise values a
        pasted text or a file holds restates it and is not checked against the
        web.
        """
        started = time.monotonic()
        given, stated = provided(messages or (question,), documents)
        question = own_words(question)
        if CREATIVE.search(normalized(question)) or (
            small_talk(question) and not self.evidence
        ):
            return answer
        claims = extract_claims(answer)
        self.claims_checked = len(claims)
        notes: dict[int, list[str]] = {}
        missing = [
            entity
            for entity in (self.missing_entities(question) if self.evidence else [])
            if not _names(given, entity) and not _names(stated, entity)
        ]
        # One contradiction note per section of the answer, and a source
        # sentence quoted once (staging -112 repeated one quote under twelve
        # trains of a list).
        quoted: set[str] = set()
        noted_sections: set[int] = set()
        for index, claim in enumerate(claims):
            if claim.values and all(_names(given, value) for value in claim.values):
                continue
            source_sentence = self.contradicted(claim)
            if source_sentence:
                section = _section(answer, claim.start)
                if source_sentence in quoted or section in noted_sections:
                    continue
                quoted.add(source_sentence)
                noted_sections.add(section)
                self.claims_changed += 1
                if _list_item(answer, claim.start):
                    # Under the list, never between its items: it says which
                    # item it is about.
                    notes.setdefault(index, []).append(
                        wording("contradicted_item", language).format(
                            claim=_quoted_claim(claim.text)
                        )
                        + "\n> "
                        + source_sentence
                    )
                else:
                    notes.setdefault(index, []).append(
                        wording("contradicted", language) + "\n> " + source_sentence
                    )
                continue
            plain = normalized(claim.text)
            named = [
                entity
                for entity in missing
                if re.search(r"\b" + re.escape(entity) + r"\b", plain)
            ]
            if named:
                missing = [entity for entity in missing if entity not in named]
                notes.setdefault(index, []).append(wording("missing", language))
        correction = ""
        if premise(question):
            self.premises_checked = 1
            correction = self.correction(question, language)
            assumed = Claim(0, len(question), question, tuple(YEAR.findall(question)))
            if not correction and assumed.values and self.contradicted(assumed):
                # The premise note follows the answer's own claim repeating it.
                repeating = next(
                    (
                        index
                        for index, claim in enumerate(claims)
                        if all(
                            re.search(
                                r"\b" + re.escape(value) + r"\b",
                                normalized(claim.text),
                            )
                            for value in assumed.values
                        )
                    ),
                    None,
                )
                if repeating is not None:
                    notes.setdefault(repeating, []).append(
                        wording("premise_unsupported", language)
                    )
        for index in sorted(notes, reverse=True):
            claim = claims[index]
            end = (
                _list_end(answer, claim.start)
                if _list_item(answer, claim.start)
                else claim.end
            )
            note = "\n\n" + "\n\n".join(notes[index]) + "\n\n"
            answer = answer[:end].rstrip() + note + answer[end:].lstrip()
        answer = answer.rstrip()
        if correction:
            # Self-contained: it names the source's date and the question's own.
            answer = correction + "\n\n" + answer
        self.latency_ms += (time.monotonic() - started) * 1000
        return answer

    def metadata(self) -> dict[str, int | float]:
        return {
            "claims_checked": self.claims_checked,
            "claims_changed": self.claims_changed,
            "premises_checked": self.premises_checked,
            "latency_ms": round(self.latency_ms, 3),
        }


current: ContextVar[Grounding | None] = ContextVar("grounding", default=None)
