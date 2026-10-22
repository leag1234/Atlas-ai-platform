"""Configured request classification and routing settings."""

import os
import re
import unicodedata

from packages.image_limits import configured_policy, image_limit, refusal

_TEXT_PREFIX = {
    "en": "I do not write this kind of content:",
    "fr": "Je n’écris pas ce type de contenu :",
    "de": "Ich schreibe diese Art von Inhalt nicht:",
    "es": "No escribo este tipo de contenido:",
    "it": "Non scrivo questo tipo di contenuto:",
}


def _plain(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def extended_enabled() -> bool:
    policy = configured_policy()
    return bool(
        policy.content_level == "extended"
        and policy.extended_request_patterns
        and policy.extended_instruction
    )


def extended_request(text: str) -> bool:
    """Return whether configured request patterns match normalized input."""
    return any(
        re.search(pattern, _plain(text), re.IGNORECASE)
        for patterns in configured_policy().extended_request_patterns.values()
        for pattern in patterns
    )


def text_limit(text: str) -> str | None:
    """Hard limits apply to text exactly as to images, at every content level."""
    return image_limit(text)


def text_refusal(category: str, language: str) -> str:
    reason = refusal(category, language).split(":", 1)[-1].strip()
    return f"{_TEXT_PREFIX.get(language, _TEXT_PREFIX['en'])} {reason}"


def extended_profile() -> str | None:
    profiles = extended_profiles()
    return profiles[0] if profiles else None


def extended_profiles() -> tuple[str, ...]:
    """Return the configured profile order, including compatibility settings."""
    configured = os.environ.get("ATLAS_EXTENDED_PROFILES")
    if configured is None:
        configured = os.environ.get("ATLAS_ADULT_PROFILES")
    if configured is None:
        configured = os.environ.get("ATLAS_ADULT_PROFILE")
    if configured is not None:
        return tuple(
            profile.strip() for profile in configured.split(",") if profile.strip()
        )
    return tuple(configured_policy().extended_profiles)


def short_refusal(text: str) -> bool:
    answer = text.strip()
    if len(answer) >= 400:
        return False
    return bool(
        re.search(
            r"(?i)(?:je ne peux pas|i can(?:not|'t) help with|i cannot|"
            r"ich kann nicht|no puedo|non posso)",
            _plain(answer),
        )
    )


def extended_instruction() -> str:
    return configured_policy().extended_instruction if extended_enabled() else ""
