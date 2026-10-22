"""M13 input contract and minimal illicit-content filter."""

import re

from pydantic import Field, field_validator
from packages.images import Strict
from packages.image_limits import configured_policy, image_limit
from packages.content_policy import extended_request
from services.guardrails.input_filter import validate_input


class ImagePrompt(Strict):
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)
    prompt: str = Field(min_length=1, max_length=2000, pattern=r"\S")

    @field_validator("prompt")
    @classmethod
    def allowed(cls, value: str) -> str:
        validate_input(value)
        if category := image_limit(value):
            raise ValueError("image_content_refused:" + category)
        if configured_policy().content_level != "extended" and extended_request(value):
            raise ValueError("image_content_refused")
        return value


def image_request(text: str) -> bool:
    return bool(
        re.match(
            r"(?i)^\s*(?:génère|générer|genere|crée|cree|generate|create|draw)(?:[ -]moi)?\s+(?:(?:une?|an?|the)\s+)?(?:image|picture|illustration|photo)\b",
            text,
        )
    )


def positive_image_prompt(value: str) -> str:
    """Remove rendering directives that the image model can paint as lettering."""
    directives = re.compile(
        r"(?i)\b(?:text|labels?|captions?|diagrams?|words?|paragraphs?|lettering|numbered callouts)\b"
    )
    value = re.sub(
        r"(?i)\b(?:no|without|do not|don't|avoid|never)\b[^.;\n]*(?:[.;]|$)",
        lambda match: "" if directives.search(match.group()) else match.group(),
        value,
    )
    value = re.sub(r"(?i)\bdiagrams?\b", "illustration", value)
    value = re.sub(
        r"(?i)\b(?:text|labels?|captions?|words?|paragraphs?|lettering|numbered callouts)\b",
        "",
        value,
    )
    return re.sub(r"[ \t]{2,}", " ", value).strip()
