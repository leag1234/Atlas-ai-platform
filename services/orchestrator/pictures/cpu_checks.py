"""Whether each CPU picture shows the counts and unusual features asked for.

The image model often misses a counted or unusual detail (staging: "a sheep
with 5 legs" gave two sheep with four legs). The rewrite lists those details
as short statements in the user's language (rewrite.checks); one vision call
per shown picture, on the gateway's "locate" role (the one that places
diagram labels, locate.py), says which statements the picture does not
show. The answer then names them plainly (note): never silent, never a
refusal, never a promise. When the check cannot run, the answer says the
details could not be checked.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from services.orchestrator.pictures.texts import text

MAX_TOKENS = 300
CHECK_SECONDS = 40.0
CHECK_BUDGET = Decimal("0.01")
SYSTEM = (
    "You check a picture against statements about it. For each statement, look "
    "carefully at the picture; when the statement gives a number, count. Answer "
    "true only when the picture clearly shows what the statement says, false when "
    "it does not or when you cannot tell. "
    'Reply with JSON only: {"met": [true or false, ...]}, one value per '
    "statement, in the order given."
)
QUOTES = {
    "en": ("“", "”"),
    "fr": ("« ", " »"),
    "de": ("„", "“"),
    "es": ("«", "»"),
    "it": ("«", "»"),
}


def messages(checks: list[str], image_url: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Statements: " + json.dumps(checks, ensure_ascii=False),
                },
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]


def parse(reply: str, count: int) -> list[bool] | None:
    """One boolean per statement, or None when the reply cannot be read."""
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(reply[start : end + 1])
    except ValueError:
        return None
    met = value.get("met") if isinstance(value, dict) else None
    if not isinstance(met, list) or len(met) != count:
        return None
    if not all(isinstance(item, bool) for item in met):
        return None
    return list(met)


async def unmet(
    checks: list[str],
    image: str,
    profile: str,
    spend: Any,
    call: Any,
    errors: list[str],
) -> list[str] | None:
    """The statements one picture (base64 PNG) does not show; None when the
    check could not run (the answer then says the details are unchecked)."""
    from services.orchestrator.pictures.turn import (
        DRAFT_SHARE,
        Refused,
        _budget,
        _code,
        _refusal,
    )

    payload = {
        "messages": messages(checks, "data:image/png;base64," + image),
        "tools": [],
        "response_format": {"type": "json_object"},
        "profile": profile,
        "vision_role": "locate",
        "max_tokens": MAX_TOKENS,
        "timeout": CHECK_SECONDS,
        "budget_eur": _budget(min(CHECK_BUDGET, spend.remaining())),
        "single_attempt": True,
        "local_enabled": False,
    }
    try:
        reply = await call(payload, CHECK_SECONDS + 5)
    except Exception as error:  # noqa: BLE001 - D5 refusal, else unchecked
        if _refusal(error) and _code(error) not in DRAFT_SHARE:
            raise Refused(error) from error
        if _code(error) in DRAFT_SHARE:
            spend.reserved_unknown += CHECK_BUDGET
        errors.append("picture_cpu_check_" + _code(error))
        return None
    spend.settle(reply)
    met = parse(str(reply.get("text") or ""), len(checks))
    if met is None:
        errors.append("picture_cpu_check_invalid")
        return None
    return [check for check, shown in zip(checks, met) if not shown]


def _items(language: str, checks: list[str]) -> str:
    left, right = QUOTES.get(language, QUOTES["en"])
    return ", ".join(left + check + right for check in checks)


def note(language: str, checks: list[str], results: list[list[str] | None]) -> str:
    """What the answer says about the checks of its pictures ("" when every
    checked picture shows everything)."""
    if not checks or not results:
        return ""
    checked = [(n, r) for n, r in enumerate(results, 1) if r is not None]
    if not checked:
        return text(language, "cpu_check_unknown", items=_items(language, checks))
    missed = [(n, r) for n, r in checked if r]
    if not missed:
        return ""
    if len(missed) == len(results) and all(r == missed[0][1] for _, r in missed):
        key = "cpu_check_one" if len(results) == 1 else "cpu_check_all"
        return text(language, key, items=_items(language, missed[0][1]))
    return " ".join(
        text(language, "cpu_check_some", n=n, items=_items(language, r))
        for n, r in missed
    )
