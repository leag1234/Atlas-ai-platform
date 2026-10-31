"""Deterministic checks for precise claims in same-conversation evidence."""

from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import re
import time
import unicodedata


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
    claims: list[Claim] = []
    for match in re.finditer(r"[^\n]+?(?:[.!?](?=\s|$)|(?=\n|$))", text):
        sentence = match[0]
        if any(start <= match.start() < end for start, end in fences):
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
    """Quoted titles, else runs of consecutive capitalised words (stop words trimmed)."""
    quoted = [normalized(match[1]) for match in QUOTED.finditer(question)]
    if quoted:
        return quoted
    entities = []
    for run in re.finditer(
        r"\b[A-ZÀ-Ý][\wÀ-ÿ]*(?:[-'][\wÀ-ÿ]+)*(?:\s+[A-ZÀ-Ý][\wÀ-ÿ]*(?:[-'][\wÀ-ÿ]+)*)*",
        question,
    ):
        words = normalized(run[0]).split()
        while words and (words[0] in STOP or words[0] in QUESTION_WORDS):
            words.pop(0)
        while words and words[-1] in STOP:
            words.pop()
        if words:
            entities.append(" ".join(words))
    return entities


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
        if not content:
            return ""
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
                if not source_values or source_values & claim_values:
                    continue
                overlap = content & terms(source_sentence)
                if overlap and len(overlap) / len(content) >= 0.75:
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

    def entity_missing(self, question: str) -> bool:
        """True when some entity named by the question appears in no fetched passage."""
        passages = [normalized(passage) for passage in self.evidence]
        return any(
            not any(
                re.search(r"\b" + re.escape(entity) + r"\b", passage)
                for passage in passages
            )
            for entity in question_entities(question)
        )

    def check(self, answer: str, question: str, language: str) -> str:
        started = time.monotonic()
        if CREATIVE.search(normalized(question)) or (
            small_talk(question) and not self.evidence
        ):
            return answer
        claims = extract_claims(answer)
        self.claims_checked = len(claims)
        for claim in reversed(claims):
            source_sentence = self.contradicted(claim)
            if not source_sentence:
                continue
            self.claims_changed += 1
            note = (
                "\n\n"
                + wording("contradicted", language)
                + "\n> "
                + source_sentence
                + "\n\n"
            )
            answer = answer[: claim.end] + note + answer[claim.end :].lstrip()
        answer = answer.rstrip()
        if self.evidence and self.entity_missing(question):
            answer = wording("missing", language) + "\n\n" + answer
        if premise(question):
            self.premises_checked = 1
            correction = self.correction(question, language)
            if correction:
                answer = correction + "\n\n" + answer
            else:
                assumed = Claim(
                    0, len(question), question, tuple(YEAR.findall(question))
                )
                if (
                    assumed.values
                    and all(
                        re.search(r"\b" + re.escape(value) + r"\b", normalized(answer))
                        for value in assumed.values
                    )
                    and self.contradicted(assumed)
                ):
                    answer = wording("premise_unsupported", language) + "\n\n" + answer
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
