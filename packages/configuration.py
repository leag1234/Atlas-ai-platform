"""Fail before startup side effects when required configuration is absent."""

import os
import re

CHAT_REQUIRED_ENV = (
    "ESCALATION_MODEL",
    "SCW_GENERATIVE_BASE_URL",
    "SCW_GENERATIVE_API_KEY",
)
GPU_REQUIRED_ENV = (
    "SCW_DEFAULT_PROJECT_ID",
    "SCW_DEFAULT_ZONE",
    "LOCAL_MODEL",
    "GPU_CLIENT_IP",
    "GPU_MAX_EUR_H",
)


IMAGE_REQUIRED_ENV = (
    *CHAT_REQUIRED_ENV,
    "SCW_DEFAULT_PROJECT_ID",
    "SCW_DEFAULT_ZONE",
    "LOCAL_MODEL",
    "GPU_MAX_EUR_H",
)


class MissingConfiguration(RuntimeError):
    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__(
            "missing_configuration: Missing required environment variables: "
            + ", ".join(missing)
        )


def require_env(names: tuple[str, ...]) -> None:
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    if missing:
        raise MissingConfiguration(tuple(missing))


def is_configuration_failure(exc: BaseException) -> bool:
    """Recognize setup failures without reflecting their diagnostics to guests."""
    message = str(exc) + " " + str(getattr(exc, "detail", ""))
    return (
        isinstance(exc, MissingConfiguration)
        or getattr(exc, "code", "") == "configuration_missing"
        or (
            "missing_configuration" in message
            or "Missing required environment variables" in message
            or "invalid_configuration:" in message
        )
    )


def guest_configuration_failure(exc: BaseException) -> bool:
    """Only the guest instance hides setup diagnostics; the owner instance keeps them (M20)."""
    return os.environ.get("ATLAS_STABLE") == "1" and is_configuration_failure(exc)


def configuration_names(exc: BaseException) -> tuple[str, ...]:
    """Return diagnostic variable names only; values are never retained or logged."""
    if isinstance(exc, MissingConfiguration):
        return exc.missing
    diagnostic = str(exc) + " " + str(getattr(exc, "detail", ""))
    return tuple(
        dict.fromkeys(
            re.findall(r"\b(?:SCW_[A-Z0-9_]*|LOCAL_MODEL|GPU_[A-Z0-9_]*)\b", diagnostic)
        )
    )


def temporarily_unavailable(language: str) -> str:
    labels = {
        "fr": "Cette fonctionnalité est temporairement indisponible.",
        "en": "This feature is temporarily unavailable.",
        "de": "Diese Funktion ist vorübergehend nicht verfügbar.",
        "es": "Esta función no está disponible temporalmente.",
        "it": "Questa funzione non è temporaneamente disponibile.",
    }
    return labels.get(language, labels["en"])
