"""Deterministic, auditable lexical scoring; no model judges or network access."""

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import re
from typing import Any, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROFILES = ("atlas-qwen", "atlas-glm", "atlas-deepseek")


class Source(BaseModel):
    url: str
    consulted_on: str

    @model_validator(mode="after")
    def check(self) -> "Source":
        date.fromisoformat(self.consulted_on)
        if not self.url.startswith("https://"):
            raise ValueError("source_requires_https")
        return self


class Fact(BaseModel):
    key: str
    value: str
    kind: Literal["text", "number", "date", "version"]
    aliases: list[str] = Field(default_factory=list)
    units: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    origin: str | None = None
    other_contexts: list[str] = Field(default_factory=list)
    target_contexts: list[str] = Field(default_factory=list)


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    language: Literal["fr", "en"]
    category: Literal["stable", "nonexistent", "changing", "adversarial"]
    entity: str
    entity_type: str | None = None
    question: str
    expected_facts: list[Fact]
    forbidden_claims: list[Fact]
    expectation: Literal["answer", "abstain"]
    as_of: str | None = None
    verification: list[dict[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def check(self) -> "Case":
        if self.category == "nonexistent":
            if (
                self.expectation != "abstain"
                or self.expected_facts
                or not self.verification
            ):
                raise ValueError("negative_requires_search_evidence_and_abstention")
        elif not self.expected_facts or any(not f.sources for f in self.expected_facts):
            raise ValueError("expected_facts_require_sources")
        if self.category == "changing" and not self.as_of:
            raise ValueError("changing_requires_as_of")
        if self.as_of:
            date.fromisoformat(self.as_of)
        for fact in self.expected_facts + self.forbidden_claims:
            if not fact.value.strip():
                raise ValueError("empty_fact")
            if fact.kind == "date":
                date.fromisoformat(fact.value)
        return self


def load_cases(path: Path) -> list[Case]:
    data = json.loads(path.read_text())
    if data["schema_version"] != 1:
        raise ValueError("unsupported_schema")
    cases = [Case.model_validate(row) for row in data["cases"]]
    if len({c.id for c in cases}) != len(cases):
        raise ValueError("duplicate_case")
    return cases


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("œ", "oe").replace("’", "'")
    text = re.sub(r"[‐‑–—]", "-", text)
    text = re.sub(r"(?<=\d)[ ,](?=\d{3}(?:\D|$))", "", text)
    return re.sub(r"\s+", " ", text).strip()


MONTHS = (
    "january janvier jan",
    "february fevrier feb",
    "march mars mar",
    "april avril apr",
    "may mai",
    "june juin jun",
    "july juillet jul",
    "august aout aug",
    "september septembre sep sept",
    "october octobre oct",
    "november novembre nov",
    "december decembre dec",
)


def value_pattern(fact: Fact) -> str:
    values = [
        re.escape(normalize(v)).replace(r"\-", r"[- ]")
        for v in [fact.value, *fact.aliases]
    ]
    if fact.kind == "date":
        d = date.fromisoformat(fact.value)
        month = "(?:" + "|".join(MONTHS[d.month - 1].split()) + r")\.?"
        values += [
            rf"0?{d.day}(?:st|nd|rd|th|er)? {month}[ ,]+{d.year}",
            rf"{month} 0?{d.day}(?:st|nd|rd|th)?[ ,]+{d.year}",
            rf"0?{d.day}[/.-]0?{d.month}[/.-]{d.year}",
        ]
    boundary = r"[\w.]" if fact.kind == "version" else r"\w"
    return rf"(?<!{boundary})(?:{'|'.join(values)})(?!{boundary})"


ABSTENTION = (
    r"\b(?:cannot|could not|can't) be (?:found|verified|identified|confirmed)\b",
    r"\b(?:n'ai|n'avons) (?:pas |rien |aucun\w* )?trouve\b",
    r"\b(?:i |we )?(?:cannot|can't|could not|couldn't|unable to) (?:find|verify|identify|confirm)\b",
    r"\bno (?:reliable |verifiable |credible )?(?:source|record|evidence|information|results?)\b",
    r"\b(?:je |nous )?ne (?:peux|peut|pouvons) pas (?:verifier|identifier|confirmer|trouver)\b",
    r"\b(?:je |nous )?ne (?:trouve|trouvons|connais) (?:pas|aucun|aucune)\b",
    r"\b(?:aucune? (?:source|trace|information|preuve)|introuvable|non verifiable)\b",
    r"\b(?:there is no|does not exist|do not exist|no such|none (?:is|are|of))\b",
    r"\b(?:il )?n[' ]?existe (?:pas|aucune?|aucun)\b",
)
NEGATION = re.compile(
    r"\b(?:not|never|incorrect|wrong|false|cannot|can't|couldn't|no evidence|"
    r"pas|jamais|faux|errone|aucun|aucune|impossible|introuvable)\b"
)
DETAIL = re.compile(
    r"\b(?:might be|may be|could be|seems to be|semble etre|serait) (?:an? |un |une )|"
    r"\b(?:is|was|are|were) (?:an? |founded|formed|located|based|published|released|launched)\b|"
    r"\b(?:est|etait) (?:un |une |situe|fonde|cree|publie|sorti)\b|"
    r"\b(?:se situe|a ete fonde|a ete cree|a ete publie|possede|compte|dispose de|"
    r"supports?|features?|offers?|provides?|weighs?|has|founded|formed|published|released)\b|"
    r"\b\d+(?:[.,]\d+)? (?:hours?|heures?|watts?|grams?|grammes?|habitants|residents)\b|"
    r"\b(?:pip|npm) install\b"
)

ACTIVITY_BLOCK = re.compile(r"<details\b[^>]*>.*?</details\s*>", re.I | re.S)
TOOL_JSON = re.compile(
    r"\{[^{}]{0,500}\"(?:tool_calls?|function|arguments)\"\s*:", re.I | re.S
)
SCOPE_LEAK = re.compile(
    r"\{[^{}]{0,1000}\"scope\"\s*:.*?(?:current request|requete actuelle)",
    re.I | re.S,
)
NARRATION = re.compile(
    r"\b(?:let me (?:try |do )?(?:one )?(?:more )?(?:search|look(?: this)? up|verify|check)|"
    r"i(?:'ll| will) try (?:one )?more|je vais (?:chercher|verifier|rechercher|effectuer)|"
    r"laissez-moi (?:chercher|verifier|rechercher))\b",
    re.I,
)
UNFINISHED = re.compile(
    r"\b(?:i(?:'ll| will)|let me|we(?:'ll| will)) "
    r"(?:search|look(?: this)? up|verify|check|try)\b|"
    r"\b(?:je vais|laissez-moi|attendez) "
    r"(?:chercher|verifier|rechercher)\b|\bje dois verifier .*recherche\b",
    re.I,
)
CORRECTION = re.compile(
    r"\b(?:not|incorrect|wrong|false|rather|instead|but|no,|pas|faux|"
    r"errone|erronee|incorrecte|inexacte|plutot|mais|non,)\b",
    re.I,
)
NEIGHBOR = re.compile(
    r"\b(?:you (?:may|might|probably) mean|perhaps you mean|likely refers to|"
    r"peut-etre (?:voulez|s'agit-il)|vous voulez probablement|"
    r"ne pas confondre avec|different (?:entity|product|band|book)|"
    r"autre (?:entite|produit|groupe|livre))\b",
    re.I,
)
CONTRAST_CONTEXT = re.compile(
    r"\b(?:previous|former|old|prior|ancienne?|precedente?|anterieur(?:e)?s?)\b",
    re.I,
)


def remove_activity_block(answer: str) -> str:
    """Drop runner progress/source markup before lexical scoring."""
    return ACTIVITY_BLOCK.sub(" ", answer)


def protocol_defects(answer: str) -> dict[str, bool]:
    """Report wire/progress leakage independently from the factual verdict."""
    return {
        "leaked_tool_json": bool(TOOL_JSON.search(answer)),
        "leaked_scope": bool(SCOPE_LEAK.search(answer)),
        "narration": bool(NARRATION.search(answer)),
        "unfinished": bool(UNFINISHED.search(answer)),
    }


def entity_tokens(entity: str) -> list[str]:
    ignored = {"the", "le", "la", "les", "de", "des", "du", "of", "and", "et"}
    return [
        token
        for token in re.findall(r"[a-z0-9]+", normalize(entity))
        if (len(token) > 2 or token.isdigit()) and token not in ignored
    ]


def names_entity(part: str, entity: str) -> bool:
    tokens = entity_tokens(entity)
    if not tokens:
        return False
    found = sum(bool(re.search(rf"(?<!\w){re.escape(token)}(?!\w)", part)) for token in tokens)
    return found == len(tokens) if len(tokens) <= 2 else found >= 2


def entity_detail(part: str, entity: str) -> bool:
    """A declarative attribute counts only when it is tied to the asked entity."""
    if not names_entity(part, entity):
        return False
    if re.search(
        r"\b(?:not|no |none|cannot|could not|might|may|perhaps|likely|possible|"
        r"confusion|different|distinct|if |whether|pas|aucun|aucune|ne .{0,12} pas|"
        r"peut-etre|probable|possible|confondre|different)\b",
        part,
    ):
        return False
    return bool(DETAIL.search(part))


def entity_forbidden(case: Case, parts: list[str]) -> list[Fact]:
    """Negative-case contrasts are facts only when asserted about the asked entity."""
    found: list[Fact] = []
    for fact in case.forbidden_claims:
        for part in parts:
            entity_positions = [
                match.start()
                for token in entity_tokens(case.entity)
                for match in re.finditer(rf"(?<!\w){re.escape(token)}(?!\w)", part)
            ]
            value_positions = [
                match.start() for match in re.finditer(value_pattern(fact), part)
            ]
            if (
                names_entity(part, case.entity)
                and not any(re.search(pattern, part) for pattern in ABSTENTION)
                and any(abs(entity_pos - value_pos) < 90 for entity_pos in entity_positions for value_pos in value_positions)
                and matches(fact, [part], True)
            ):
                found.append(fact)
                break
    return found


def positions_for(text: str, context: str) -> list[int]:
    """Return the normalized context's positions without treating it as a regex."""
    normalized = normalize(context)
    if not normalized:
        return []
    return [match.start() for match in re.finditer(re.escape(normalized), text)]


def nearest_context_distance(
    positions: list[int], value: re.Match[str]
) -> int | None:
    if not positions:
        return None
    return min(
        min(abs(position - value.start()), abs(position - value.end()))
        for position in positions
    )


def value_clause(part: str, value: re.Match[str]) -> str:
    """Keep the comma-delimited assertion that owns a value, preserving dates."""
    boundaries = [
        match.start()
        for match in re.finditer(r";|,(?!\s*\d{4}\b)", part)
    ]
    start = max((boundary + 1 for boundary in boundaries if boundary < value.start()), default=0)
    end = min((boundary for boundary in boundaries if boundary >= value.end()), default=len(part))
    return part[start:end]


def asserted_about_target(case: Case, fact: Fact, part: str, value: re.Match[str]) -> bool:
    """Require a forbidden value to describe the requested entity and attribute."""
    scoped = value_clause(part, value)
    scoped_value = next(re.finditer(value_pattern(fact), scoped), None)
    if scoped_value is None:
        return False
    entity_positions = [
        match.start()
        for token in entity_tokens(case.entity)
        for match in re.finditer(rf"(?<!\w){re.escape(token)}(?!\w)", scoped)
    ]
    named_target = names_entity(scoped, case.entity) and any(
        abs(position - scoped_value.start()) < 90 for position in entity_positions
    )
    pronoun_target = bool(
        re.match(r"\s*(?:it|il|elle|ce|cet|cette)\b", scoped, re.I)
    )
    if not named_target and not pronoun_target:
        return False

    other_distance = nearest_context_distance(
        [
            position
            for context in fact.other_contexts
            for position in positions_for(scoped, context)
        ],
        scoped_value,
    )
    target_distance = nearest_context_distance(
        [
            position
            for context in fact.target_contexts
            for position in positions_for(scoped, context)
        ],
        scoped_value,
    )
    if other_distance is not None and (
        target_distance is None or other_distance < target_distance
    ):
        return False

    contrast_positions = [match.start() for match in CONTRAST_CONTEXT.finditer(part)]
    contrast_distance = nearest_context_distance(contrast_positions, value)
    return contrast_distance is None or contrast_distance >= 45


def target_forbidden(case: Case, parts: list[str]) -> list[Fact]:
    """Find forbidden values asserted about this case's requested fact."""
    found: list[Fact] = []
    for fact in case.forbidden_claims:
        for part in parts:
            if not matches(fact, [part], True):
                continue
            if any(
                asserted_about_target(case, fact, part, value)
                for value in re.finditer(value_pattern(fact), part)
                if asserted(part, value)
            ):
                found.append(fact)
                break
    return found


def clauses(answer: str) -> list[str]:
    answer = re.sub(r"^\s*>.*$", "", answer, flags=re.M)
    answer = re.sub(r"https?://\S+", "", answer)
    answer = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", answer)
    answer = answer.replace("**", "").replace("`", "")
    parts = re.split(
        r"[.!](?!\d)(?:\s|$)|\?\s*|[;\n]|,?\s+\b(?:but|mais|however|instead|and|et)\b",
        answer,
        flags=re.I,
    )
    # Questions and quoted premises are not assertions. Retain statement clauses.
    return [
        normalize(p)
        for p in parts
        if p.strip()
        and not re.match(
            r"\s*(?:was|is|did|what|when|where|who|could you|peux-tu)\b", p, re.I
        )
    ]


def asserted(clause: str, match: re.Match[str]) -> bool:
    before = clause[: match.start()]
    after = clause[match.end() :]
    if NEGATION.search(before):
        return False
    if re.match(
        r"\s+(?:(?:did|does|is|was|were) not|didn't|isn't|wasn't|n'(?:a|est|etait) pas)\b",
        after,
    ):
        return False
    return not bool(
        re.match(
            r"\s+(?:is |was |est |etait )?(?:incorrect|wrong|false|faux|errone)\b",
            after,
        )
    )


def matches(fact: Fact, parts: list[str], affirmative: bool) -> list[str]:
    found = []
    for part in parts:
        for match in re.finditer(value_pattern(fact), part):
            if affirmative and not asserted(part, match):
                continue
            if (
                affirmative
                and any(normalize(c) in part for c in fact.other_contexts)
                and not any(normalize(c) in part for c in fact.target_contexts)
            ):
                continue
            if fact.units:
                vicinity = part[max(0, match.start() - 8) : match.end() + 25]
                if not any(
                    re.search(
                        r"(?<!\w)" + re.escape(normalize(u)) + r"(?!\w)", vicinity
                    )
                    for u in fact.units
                ):
                    continue
            found.append(part)
    return found


def score_answer(case: Case, answer: str) -> dict[str, Any]:
    answer = remove_activity_block(answer)
    defects = protocol_defects(answer)
    parts = clauses(answer)
    forbidden_present = [
        f.value for f in case.forbidden_claims if matches(f, parts, False)
    ]
    forbidden = [f for f in case.forbidden_claims if matches(f, parts, True)]
    if case.category == "nonexistent":
        forbidden = entity_forbidden(case, parts)
    elif case.category in {"stable", "changing"}:
        forbidden = target_forbidden(case, parts)
    facts = {f.key: matches(f, parts, True) for f in case.expected_facts}
    # A response can quote a false premise while correcting it.  Do not erase the
    # independently matched correct value merely because both facts share a key.
    found = [key for key, evidence in facts.items() if evidence]
    abstains = any(re.search(pattern, normalize(answer)) for pattern in ABSTENTION)
    details = []
    if case.category == "nonexistent":
        refers_to_entity = False
        for part in parts:
            refers_to_entity = refers_to_entity or names_entity(part, case.entity) or bool(
                re.search(r"\b(?:this|that|ce|cet|cette) " + re.escape(normalize(case.entity_type or "")), part)
            )
            if (entity_detail(part, case.entity) or (refers_to_entity and re.search(r"\b(?:it|il|elle)\b", part) and DETAIL.search(part))) and not any(
                re.search(p, part) for p in ABSTENTION
            ):
                details.append(part)
    # A forbidden value embedded in an explicitly distinguished neighbour does not
    # describe the requested entity.
    if abstains and not details:
        forbidden = []
    fabricated = bool(forbidden or details)
    neighbor = abstains and any(
        NEIGHBOR.search(part)
        or (
            re.search(r"\b(?:closest|near(?:est)?|confusion|different|distinct|"
                      r"proche|confondre|similaire)\b", part)
            and not names_entity(part, case.entity)
        )
        for part in parts
    )
    usable = bool(" ".join(parts)) and not (
        defects["leaked_tool_json"] and len(parts) == 1
    )
    nonexistent_outcome = (
        "fabricated"
        if fabricated
        else "abstain_neighbor"
        if abstains and neighbor
        else "abstain"
        if abstains
        else "no_answer"
    )
    expected_found = bool(found)
    defects["unfinished"] = bool(
        defects["unfinished"] and not abstains and not expected_found and not forbidden
    )
    correction_nearby = bool(CORRECTION.search(" ".join(parts)))
    contextual_reassignment = any(
        any(normalize(context) in " ".join(parts) for context in fact.other_contexts)
        and any(normalize(context) in " ".join(parts) for context in fact.target_contexts)
        for fact in case.forbidden_claims
    )
    adversarial_outcome = (
        "corrected"
        if expected_found and correction_nearby
        else "accepted"
        if forbidden and (not expected_found or not contextual_reassignment)
        else "no_answer"
    )
    if case.category == "adversarial":
        fabricated = adversarial_outcome == "accepted"
    return {
        "id": case.id,
        "language": case.language,
        "category": case.category,
        "facts_found": found,
        "facts_expected": len(case.expected_facts),
        "fact_evidence": facts,
        "forbidden_present": forbidden_present,
        "forbidden_asserted": [f.value for f in forbidden],
        "description_evidence": details,
        "abstention_pattern_found": abstains,
        "protocol_defects": defects,
        "outcome": (
            nonexistent_outcome
            if case.category == "nonexistent"
            else adversarial_outcome
            if case.category == "adversarial"
            else "answered" if usable else "no_answer"
        ),
        "correct_abstention": case.category == "nonexistent"
        and nonexistent_outcome in {"abstain", "abstain_neighbor"},
        "fabrication": fabricated,
        "false_premise_accepted": case.category == "adversarial"
        and adversarial_outcome == "accepted",
    }


def ratio(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [r for r in rows if not r["error"]]
    outcome_counts = Counter(r["outcome"] for r in valid)
    nonexistent = [r for r in valid if r["category"] == "nonexistent"]
    return {
        "attempted": len(rows),
        "scored": len(valid),
        "technical_failures": len(rows) - len(valid),
        "fact_accuracy": ratio(
            sum(len(r["facts_found"]) for r in valid),
            sum(r["facts_expected"] for r in rows),
        ),
        "fabrication_rate": ratio(sum(r["fabrication"] for r in valid), len(valid)),
        "correct_abstention_rate": ratio(
            sum(r["correct_abstention"] for r in valid),
            sum(r["category"] == "nonexistent" for r in rows),
        ),
        "false_premise_acceptance_rate": ratio(
            sum(r["false_premise_accepted"] for r in valid),
            sum(r["category"] == "adversarial" for r in valid),
        ),
        "outcome_counts": dict(outcome_counts),
        "nonexistent_outcome_sources": dict(
            Counter(r["outcome_source"] for r in nonexistent)
        ),
        "judge_error_count": sum(r.get("judge_error", False) for r in nonexistent),
        "leaked_tool_json_rate": ratio(
            sum(r["protocol_defects"]["leaked_tool_json"] for r in valid), len(valid)
        ),
        "leaked_scope_rate": ratio(
            sum(r["protocol_defects"]["leaked_scope"] for r in valid), len(valid)
        ),
        "narration_rate": ratio(
            sum(r["protocol_defects"]["narration"] for r in valid), len(valid)
        ),
        "unfinished_rate": ratio(
            sum(r["protocol_defects"]["unfinished"] for r in valid), len(valid)
        ),
    }


def aggregate(
    cases: list[Case],
    answers: list[dict[str, Any]],
    judgments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    index = {c.id: c for c in cases}
    judgment_rows = (judgments or {}).get("judgments", judgments or {})
    if not isinstance(judgment_rows, dict):
        raise ValueError("invalid_judgments")
    seen: set[tuple[str, str]] = set()
    scored = []
    for row in answers:
        identity = (row["profile"], row["id"])
        if identity in seen or identity[0] not in PROFILES or identity[1] not in index:
            raise ValueError("duplicate_or_unknown_result")
        seen.add(identity)
        case = index[row["id"]]
        result = score_answer(case, row["answer"] if not row["error"] else "")
        judge_error = False
        if case.category == "nonexistent":
            judgment = judgment_rows.get(f"{row['profile']}|{row['id']}")
            if isinstance(judgment, dict) and judgment.get("outcome") == "judge_error":
                judge_error = True
                # A judge failure may retain a lexical display outcome, but it is
                # deliberately not evidence of a correct abstention.
                result["correct_abstention"] = False
            if (
                isinstance(judgment, dict)
                and judgment.get("outcome") in {"abstain", "fabricated", "no_answer"}
            ):
                outcome = judgment["outcome"]
                result.update(
                    outcome=outcome,
                    fabrication=outcome == "fabricated",
                    correct_abstention=outcome == "abstain",
                    outcome_source="judge",
                )
            else:
                # The lexical verdict remains visible, but it was not semantic-judge
                # validated (including when the judge itself failed).
                result["outcome_source"] = "lexical_unvalidated"
        scored.append(
            {**result, "profile": row["profile"], "error": row["error"], "judge_error": judge_error}
        )
    profiles = {}
    for profile in PROFILES:
        rows = [r for r in scored if r["profile"] == profile]
        profiles[profile] = {
            "planned": len(cases),
            "unattempted": len(cases) - len(rows),
            "overall": metrics(rows),
            "categories": {
                cat: metrics([r for r in rows if r["category"] == cat])
                for cat in sorted({c.category for c in cases})
            },
            "languages": {
                lang: metrics([r for r in rows if r["language"] == lang])
                for lang in ("fr", "en")
            },
            "category_languages": {
                f"{cat}/{lang}": metrics(
                    [r for r in rows if r["category"] == cat and r["language"] == lang]
                )
                for cat in sorted({c.category for c in cases})
                for lang in ("fr", "en")
            },
        }
    return {
        "profiles": profiles,
        "cases": scored,
        "case_counts": dict(Counter(c.category for c in cases)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--judgments", type=Path)
    args = parser.parse_args()
    data = json.loads(args.results.read_text())
    cases = [Case.model_validate(c) for c in data["frozen_cases"]["cases"]]
    judgments = json.loads(args.judgments.read_text()) if args.judgments else None
    print(json.dumps(aggregate(cases, data["answers"], judgments), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
