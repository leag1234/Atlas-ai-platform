"""Privacy level of the current request (Standard, Enhanced, Strict) and the egress check.

Levels, from the least to the most protective:

- ``standard``: content goes to the EU providers of the active routes.
- ``enhanced``: identifiers detected by Atlas are to be replaced by tokens before content
  leaves Atlas (local pseudonymisation). The pseudonymiser is not installed yet
  (``pseudonymiser_available``), so Enhanced never pretends: content that would leave in
  clear is not sent. Model calls use owned targets only, or are refused with
  ``enhanced_privacy_unavailable``; features whose content cannot be pseudonymised (audio,
  images, scanned pages, embeddings, web and place search) are refused with
  ``privacy_feature_refused``. Connectors stay allowed (the user chose that recipient), and
  so does the currency rate feed (it carries no request content). The level is offered
  only where the operator enables it (``ATLAS_PRIVACY_ENHANCED=1``).
- ``strict``: content is processed only on owned or controlled machines (an
  Atlas-operated server, rented dedicated servers included, or the customer's own).
  Anything else is refused with ``strict_privacy_unavailable``; there is no web search.

One effective level per request: the highest of the organisation minimum
(``ATLAS_PRIVACY_MINIMUM``), the API key default, the project, the conversation and the
request itself. Nothing lowers it. It is carried in a context variable so that every call
made while the request runs (child tasks included) sees it without each call site passing
it along:

- the edge (API key, chat conversation, project turn, Work turn, dictation) raises it with
  ``elevate``; ``current`` never returns less than the organisation minimum;
- calls to the model gateway carry it in the ``X-Atlas-Privacy`` header, and the gateway
  restores it for the duration of the request;
- every egress that can carry request content to a shared provider or a third-party
  service calls ``require_shared`` before sending. The gateway does it once, where a
  provider credential is resolved (``provider_settings``); outside the gateway, each
  third-party client (OCR, web search and fetch, image search, maps, connectors) does it.

Refusals are permanent policy answers, never outages: HTTP 403 with a named code. A
refusal crossing the gateway boundary is marked with the ``X-Atlas-Privacy-Refusal``
response header (value: the refused capability), so clients name it without reading the
body.
"""

import os
from collections.abc import Iterable, Mapping
from contextvars import ContextVar, Token
from typing import Any, Literal

Level = Literal["standard", "enhanced", "strict"]
STANDARD: Level = "standard"
ENHANCED: Level = "enhanced"
STRICT: Level = "strict"
LEVELS: tuple[Level, ...] = (STANDARD, ENHANCED, STRICT)
RANK: dict[str, int] = {level: index for index, level in enumerate(LEVELS)}

CODE = "strict_privacy_unavailable"
ENHANCED_CODE = "enhanced_privacy_unavailable"
FEATURE_CODE = "privacy_feature_refused"
INVALID_CODE = "invalid_privacy_level"
CODES = frozenset({CODE, ENHANCED_CODE, FEATURE_CODE})
HEADER = "X-Atlas-Privacy"
APPLIED_HEADER = "X-Atlas-Privacy-Applied"
REFUSAL_HEADER = "X-Atlas-Privacy-Refusal"
# A refusal is a permanent policy answer: 403, so that clients do not retry it as an
# outage (owner decision 2026-11-14; it was 503 before).
STATUS = 403

# Capabilities whose content may leave Atlas at the Enhanced level without
# pseudonymisation: connectors (a recipient the user chose, shown in the interface) and
# the currency rate feed (the request carries no content of the conversation).
ENHANCED_SHARED = frozenset({"connectors", "currency_rates"})
# Text capabilities the pseudonymiser will cover; until it exists they use owned
# targets only at the Enhanced level.
PSEUDONYMISED = frozenset({"inference", "evaluation", "pseudonymisation"})

# Tools whose only implementation sends request content to a shared or third-party
# service: never offered in a strict turn (their clients also refuse at egress).
STRICT_EXCLUDED_TOOLS = frozenset(
    {
        "web_search",
        "web_fetch",
        "web_images",
        "currency_convert",
        "mcp_call",
        "switch_connector_group",
    }
)
TOOL_CAPABILITIES = {
    "web_search": "web_search",
    "web_fetch": "web_fetch",
    "web_images": "web_search",
    "currency_convert": "currency_rates",
    "mcp_call": "connectors",
    "switch_connector_group": "connectors",
}

_level: ContextVar[Level] = ContextVar("atlas_privacy_level", default=STANDARD)


class InvalidPrivacyLevel(ValueError):
    """A privacy value that is not one of the three levels (HTTP 400)."""

    code = INVALID_CODE
    status = 400

    def __init__(self) -> None:
        super().__init__(INVALID_CODE)

    @property
    def message(self) -> str:
        return "privacy must be one of: " + ", ".join(LEVELS) + "."


def refusal_code(capability: str, level: str) -> str:
    if level == ENHANCED:
        return ENHANCED_CODE if capability in PSEUDONYMISED else FEATURE_CODE
    return CODE


class PrivacyRefusal(ValueError):
    """A capability is not allowed at the request's privacy level (HTTP 403, named)."""

    status = STATUS

    def __init__(self, capability: str = "inference", level: str | None = None) -> None:
        level = level if level in (ENHANCED, STRICT) else _refusing_level()
        self.capability, self.level = capability, level
        self.code = refusal_code(capability, level)
        super().__init__(self.code)

    @property
    def message(self) -> str:
        return describe(self.capability, self.level)

    def body(self) -> dict[str, str]:
        """Public error fields: never any content, only the code, feature and level."""
        return {
            "type": self.code,
            "code": self.code,
            "message": self.message,
            "feature": self.capability,
            "level": self.level,
        }


# The name of the strict-only refusal before the three levels; every existing handler
# catches the whole family through it.
StrictPrivacyUnavailable = PrivacyRefusal


def _refusing_level() -> str:
    level = current()
    return level if level != STANDARD else STRICT


def describe(capability: str, level: str = STRICT) -> str:
    name = capability.replace("_", " ")
    if level == ENHANCED:
        if capability in PSEUDONYMISED:
            return (
                "Enhanced privacy needs Atlas's local pseudonymisation, which is not "
                "installed on this server yet, and no owned server is available for "
                f"{name}. Nothing was sent."
            )
        return (
            f"{name[:1].upper() + name[1:]} is not available in enhanced privacy mode: "
            "this content cannot be pseudonymised before it leaves Atlas. Nothing was sent."
        )
    return f"No owned server is available for {name} in strict privacy mode."


def parse(value: object) -> Level | None:
    """A public privacy value: None when absent, else one of the levels (or 400)."""
    if value is None or value == "":
        return None
    if isinstance(value, str) and value.strip().lower() in RANK:
        return value.strip().lower()  # type: ignore[return-value]
    raise InvalidPrivacyLevel()


def highest(*levels: object) -> Level:
    """The most protective of the given levels; None counts as standard."""
    result: Level = STANDARD
    for value in levels:
        level = value if isinstance(value, str) and value in RANK else None
        if level is not None and RANK[level] > RANK[result]:
            result = level  # type: ignore[assignment]
    return result


def minimum() -> Level:
    """The organisation minimum; a value that cannot be read counts as strict."""
    raw = os.environ.get("ATLAS_PRIVACY_MINIMUM", "").strip().lower()
    if not raw:
        return STANDARD
    return raw if raw in RANK else STRICT  # type: ignore[return-value]


def enhanced_offered() -> bool:
    """Whether users may choose Enhanced (operator switch; off until the pseudonymiser)."""
    return os.environ.get("ATLAS_PRIVACY_ENHANCED") == "1" or minimum() == ENHANCED


def pseudonymiser_available() -> bool:
    """Whether the local pseudonymiser runs for this request. Not built yet: the next
    slice of the privacy levels work (detector measurement, then the gateway service)."""
    return False


def owned_available() -> bool:
    """Whether an owned inference server is configured for this deployment."""
    return bool(os.environ.get("ATLAS_OWNED_VLLM_BASE_URL"))


def offered(level: str) -> bool:
    """Whether a request may choose ``level`` here (Enhanced only where enabled)."""
    return level != ENHANCED or enhanced_offered()


def current() -> Level:
    level = _level.get()
    floor = minimum()
    return floor if RANK[floor] > RANK[level] else level


def is_strict() -> bool:
    return current() == STRICT


def owned_only() -> bool:
    """Model calls of this request may use owned targets only."""
    level = current()
    return level == STRICT or (level == ENHANCED and not pseudonymiser_available())


def allows(capability: str) -> bool:
    """Whether request content may go to a shared provider or third party for
    ``capability`` at the current level."""
    level = current()
    if level == STANDARD:
        return True
    if level == ENHANCED:
        return capability in ENHANCED_SHARED or (
            capability in PSEUDONYMISED and pseudonymiser_available()
        )
    return False


def refusal(capability: str) -> PrivacyRefusal:
    return PrivacyRefusal(capability, _refusing_level())


def elevate(level: object) -> Token[Level] | None:
    """Raise the current request to ``level``; a lower level never lowers it."""
    if isinstance(level, str) and level in RANK and RANK[level] > RANK[_level.get()]:
        return _level.set(level)  # type: ignore[arg-type]
    return None


def reset(token: Token[Level] | None) -> None:
    if token is not None:
        _level.reset(token)


def require_shared(capability: str) -> None:
    """Call before sending request content to a shared provider or third party."""
    if not allows(capability):
        raise refusal(capability)


def headers() -> dict[str, str]:
    """Headers of an internal call to the model gateway or retrieval service."""
    level = current()
    return {HEADER: level} if level != STANDARD else {}


def requested(headers: Mapping[str, str] | Any, body: object = None) -> Level:
    """Level asked by an internal caller (header or body field); unknown means standard."""
    value = headers.get(HEADER) if headers is not None else None
    asked = str(value or "").strip().lower()
    field = body.get("privacy") if isinstance(body, dict) else None
    return highest(asked, field)


def tool_name(tool: object) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict):
            return str(function.get("name", ""))
        return str(tool.get("name", ""))
    return ""


def tool_capability(name: str) -> str | None:
    if name.startswith("mcp_"):
        return "connectors"
    return TOOL_CAPABILITIES.get(name)


def allowed_tools(tools: Iterable[Any]) -> list[Any]:
    """The tool declarations a turn may offer at the current level."""
    if current() == STANDARD:
        return list(tools)
    return [
        tool
        for tool in tools
        if (capability := tool_capability(tool_name(tool))) is None
        or allows(capability)
    ]
