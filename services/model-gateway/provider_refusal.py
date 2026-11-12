"""Machine-readable class of a provider HTTP refusal.

A log line may carry the provider's error identifiers (type, code) and the class
computed here. It never carries the provider's message, the request or a
credential. The classes follow the documented status codes and error fields of
the configured providers; the sources are listed in docs/providers.md.
"""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
import json
import re
from typing import Any

import aiohttp

CLASSES = ("credit", "rate_limit", "capacity", "auth", "other")

# Substrings looked up in the sanitised type and code, in this order: an account
# without credit and a saturated service tier may both answer with HTTP 429, so
# the identifiers are read before the status.
_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "credit",
        (
            "insufficient_quota",
            "insufficient_balance",
            "insufficient_credit",
            "insufficient_fund",
            "payment",
            "billing",
            "credit",
            "spending_limit",
            "spend_limit",
        ),
    ),
    ("capacity", ("capacity", "overload")),
    (
        "rate_limit",
        (
            "rate_limit",
            "ratelimit",
            "too_many",
            "toomany",
            "throttl",
            "tokens_per",
            "requests_per",
            "concurren",
        ),
    ),
    (
        "auth",
        (
            "authentication",
            "unauthorized",
            "unauthorised",
            "invalid_api_key",
            "invalid_key",
            "permission",
            "forbidden",
            "access_denied",
        ),
    ),
)

# Documented meanings when the identifiers say nothing: 402 payment required,
# 423 model locked by a missing payment method or a consumption limit, 401 and
# 403 credentials or permissions, 429 requests or tokens per period, 503
# temporarily overloaded.
_STATUS = {
    402: "credit",
    423: "credit",
    401: "auth",
    403: "auth",
    429: "rate_limit",
    503: "capacity",
}


@dataclass(frozen=True)
class Refusal:
    """What a log line may say about one refused provider call."""

    kind: str = "other"
    error_type: str = ""
    error_code: str = ""
    status: int | None = None

    def fields(self) -> str:
        return (
            f"class={self.kind} type={self.error_type or '-'} "
            f"code={self.error_code or '-'}"
        )


async def error_envelope(
    response: aiohttp.ClientResponse, limit: int = 16384
) -> dict[str, Any] | None:
    """The bounded JSON object of an error response; None when it is unusable.

    Error bodies are untrusted and may be HTML, oversized or stalled. The body
    of a response can be read only once: callers share the returned object.
    """
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for piece in response.content.iter_chunked(4096):
                if len(body) + len(piece) > limit:
                    return None
                body.extend(piece)
        value = json.loads(body)
    except (ValueError, RecursionError, aiohttp.ClientError, TimeoutError):
        return None
    return value if isinstance(value, dict) else None


def _identifier(value: object, secrets: tuple[str, ...]) -> str:
    """Lowercase letters, digits and underscores, at most 60 characters, or nothing."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value) if 0 <= value < 1_000_000_000 else ""
    # Anything that is not a short identifier may be free text: it is dropped.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", value):
        return ""
    lowered = value.lower()
    for secret in secrets:
        hidden = secret.lower()
        if hidden and (hidden in lowered or (len(lowered) >= 8 and lowered in hidden)):
            return ""
    return re.sub(r"[^a-z0-9_]", "_", lowered)


def refusal_class(
    status: int | None, error_type: str = "", error_code: str = ""
) -> str:
    if status == 402:
        return "credit"
    for kind, words in _WORDS:
        if any(word in field for field in (error_type, error_code) for word in words):
            return kind
    return _STATUS.get(status, "other") if status is not None else "other"


def classify_refusal(
    status: int | None,
    envelope: dict[str, Any] | None = None,
    secrets: Iterable[str] = (),
) -> Refusal:
    hidden = tuple(secrets)
    error_type = error_code = ""
    if envelope is not None:
        # Nested {"error": {...}} or {"detail": {...}}, or a flat error object.
        for source in (envelope.get("error"), envelope.get("detail"), envelope):
            if isinstance(source, dict):
                error_type = error_type or _identifier(source.get("type"), hidden)
                error_code = error_code or _identifier(source.get("code"), hidden)
    return Refusal(
        refusal_class(status, error_type, error_code), error_type, error_code, status
    )
