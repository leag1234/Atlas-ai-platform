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


def configured_policy() -> ImagePolicy:
    """Expose the same policy instance used by request validation."""
    return POLICY


def image_limit(text: str, policy: ImagePolicy | None = None) -> str | None:
    """Each category requires the conjunction of its owner-editable patterns."""
    normalized = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    ).replace("’", "'")
    for rule in _RULES:
        if all(re.search(pattern, normalized) for pattern in rule.all_of):
            return rule.category
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
