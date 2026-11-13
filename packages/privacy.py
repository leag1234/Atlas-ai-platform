"""Effective privacy level of the current request and the strict egress check.

Strict privacy: request content is processed only on owned or controlled machines
(an Atlas-operated server, rented dedicated servers included, or the customer's own).
Anything else is refused with the named code ``strict_privacy_unavailable``; it is never
served silently elsewhere, and there is no web search.

One level per request, carried in a context variable so that every call made while the
request runs (child tasks included) sees it without each call site passing it along:

- the edge (API key, project turn, Work turn) raises it with ``elevate``; nothing lowers it;
- calls to the model gateway carry it in the ``X-Atlas-Privacy`` header, and the gateway
  restores it for the duration of the request;
- every egress that can carry request content to a shared provider or a third-party
  service calls ``require_shared`` before sending. The gateway does it once, where a
  provider credential is resolved (``provider_settings``); outside the gateway, each
  third-party client (OCR, web search and fetch, image search, maps, connectors) does it.

A refusal crossing the gateway boundary is marked with the ``X-Atlas-Privacy-Refusal``
response header (value: the refused capability), so clients name it without reading the
body.
"""

from collections.abc import Iterable, Mapping
from contextvars import ContextVar, Token
from typing import Any, Literal

Level = Literal["standard", "strict"]
STANDARD: Level = "standard"
STRICT: Level = "strict"
CODE = "strict_privacy_unavailable"
HEADER = "X-Atlas-Privacy"
REFUSAL_HEADER = "X-Atlas-Privacy-Refusal"
# The status of the existing public strict refusals (a status change is an owner decision).
STATUS = 503

# Tools whose only implementation sends request content to a shared or third-party
# service: never offered in a strict turn (their clients also refuse at egress).
STRICT_EXCLUDED_TOOLS = frozenset(
    {
        "web_search",
        "web_fetch",
        "web_images",
        "mcp_call",
        "switch_connector_group",
    }
)

_level: ContextVar[Level] = ContextVar("atlas_privacy_level", default=STANDARD)


class StrictPrivacyUnavailable(ValueError):
    """No owned target exists for a capability a strict request needs."""

    code = CODE
    status = STATUS

    def __init__(self, capability: str = "inference") -> None:
        super().__init__(CODE)
        self.capability = capability

    @property
    def message(self) -> str:
        return describe(self.capability)


def describe(capability: str) -> str:
    return (
        f"No owned server is available for {capability.replace('_', ' ')} "
        "in strict privacy mode."
    )


def current() -> Level:
    return _level.get()


def is_strict() -> bool:
    return _level.get() == STRICT


def elevate(level: object) -> Token[Level] | None:
    """Raise the current request to ``level``; a lower level never lowers it."""
    if level == STRICT and _level.get() != STRICT:
        return _level.set(STRICT)
    return None


def reset(token: Token[Level] | None) -> None:
    if token is not None:
        _level.reset(token)


def require_shared(capability: str) -> None:
    """Call before sending request content to a shared provider or third party."""
    if is_strict():
        raise StrictPrivacyUnavailable(capability)


def headers() -> dict[str, str]:
    """Headers of an internal call to the model gateway or retrieval service."""
    return {HEADER: STRICT} if is_strict() else {}


def requested(headers: Mapping[str, str] | Any, body: object = None) -> Level:
    """Level asked by an internal caller (header or body field); unknown means standard."""
    value = headers.get(HEADER) if headers is not None else None
    if str(value or "").lower() == STRICT:
        return STRICT
    if isinstance(body, dict) and body.get("privacy") == STRICT:
        return STRICT
    return STANDARD


def tool_name(tool: object) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict):
            return str(function.get("name", ""))
        return str(tool.get("name", ""))
    return ""


def allowed_tools(tools: Iterable[Any]) -> list[Any]:
    """The tool declarations a turn may offer at the current level."""
    if not is_strict():
        return list(tools)
    return [
        tool
        for tool in tools
        if tool_name(tool) not in STRICT_EXCLUDED_TOOLS
        and not tool_name(tool).startswith("mcp_")
    ]
