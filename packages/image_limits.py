"""Shared, deterministic image limits, checked before any generation transport."""

import json
from pathlib import Path
import re
import unicodedata

from pydantic import BaseModel, ConfigDict
from packages.image_policy import POLICY, ImagePolicy


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    category: str
    all_of: list[str]
    messages: dict[str, str]


_RULES = tuple(
    Rule.model_validate(rule)
    for rule in json.loads(Path(__file__).with_suffix(".json").read_text())
)
# Named word lists shared by the rules: a rule pattern writes <<people>> for the
# alternatives of the "people" list (owner-editable, like the rules).
_WORDS: dict[str, list[str]] = json.loads(
    Path(__file__).with_name("image_limit_words.json").read_text()
)
_MARKER = re.compile(r"<<(\w+)>>")


def _expand(pattern: str, words: dict[str, list[str]], depth: int = 0) -> str:
    if depth > 4:
        raise ValueError("image limit word lists nest too deeply")
    return _MARKER.sub(
        lambda found: _expand("|".join(words[found[1]]), words, depth + 1), pattern
    )


Compiled = tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]


def compile_rules(words: dict[str, list[str]] | None = None) -> Compiled:
    """The rules with their word lists; ``words`` replaces some lists (tests)."""
    lists = {**_WORDS, **(words or {})}
    return tuple(
        (
            rule.category,
            tuple(re.compile(_expand(pattern, lists)) for pattern in rule.all_of),
        )
        for rule in _RULES
    )


_COMPILED = compile_rules()


def configured_policy() -> ImagePolicy:
    """Expose the same policy instance used by request validation."""
    return POLICY


def image_limit(
    text: str, policy: ImagePolicy | None = None, rules: Compiled | None = None
) -> str | None:
    """Each category requires the conjunction of its owner-editable patterns."""
    normalized = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    ).replace("’", "'")
    for category, patterns in rules or _COMPILED:
        if all(pattern.search(normalized) for pattern in patterns):
            return category
    if (policy or POLICY).image_safety_level == "strict":
        for category, pattern in _STRICT_PATTERNS.items():
            if re.search(pattern, normalized, re.IGNORECASE):
                return category
    return None


_STRICT_PATTERNS = {
    "strict_nudity": r"\b(?:nud(?:e|es|ity|ite|ita|o|i|a)|naked|nu|nue|nus|nues|nackt\w*|desnud\w*|nudit\w*|entbloss\w*|unclothed|bare breasts?|seins? nus?|genital\w*)\b",
    "strict_gore": r"\b(?:gore|gory|graphic violence|violence graphique|violenza grafica|violencia grafica|grafische gewalt|dismember\w*|eviscerat\w*|decapitat\w*|behead\w*|entrailles|eventr\w*|demembr\w*|sanglant\w*|blutig\w*|zerstuckel\w*|enthaupt\w*|desmembr\w*|sanguinolent\w*|smembr\w*|squart\w*)\b",
}
_STRICT_MESSAGES = {
    "strict_nudity": {
        "en": "Strict image policy prohibits nudity, including artistic and medical nudity.",
        "fr": "La politique stricte interdit la nudité, y compris artistique et médicale.",
        "de": "Die strenge Bildrichtlinie verbietet Nacktheit, auch in Kunst und Medizin.",
        "es": "La política estricta prohíbe la desnudez, también artística y médica.",
        "it": "La politica rigorosa vieta la nudità, anche artistica e medica.",
    },
    "strict_gore": {
        "en": "Strict image policy prohibits gore and graphic violence.",
        "fr": "La politique stricte interdit le gore et la violence graphique.",
        "de": "Die strenge Bildrichtlinie verbietet blutige und grafische Gewalt.",
        "es": "La política estricta prohíbe el gore y la violencia gráfica.",
        "it": "La politica rigorosa vieta il gore e la violenza grafica.",
    },
}


def refusal(category: str, language: str) -> str:
    if category in _STRICT_MESSAGES:
        return _STRICT_MESSAGES[category].get(
            language, _STRICT_MESSAGES[category]["en"]
        )
    rule = next(rule for rule in _RULES if rule.category == category)
    return rule.messages.get(language, rule.messages["en"])
