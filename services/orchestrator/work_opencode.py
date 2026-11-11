"""opencode as the Work engine for code requests (step 1, behind a setting).

With ATLAS_WORK_ENGINE=opencode, a project Work turn on a code request runs
opencode in server mode inside the session's own container instead of Atlas's
loop. Everything else (projects, files, approvals, budgets, ledger, UI events)
stays Atlas's: this driver maps opencode server events to Atlas Work events and
enforces the turn's own time and cost ceilings by aborting the opencode session.
"""

import asyncio
import base64
import json
import os
import time
from decimal import Decimal
from typing import Any, Awaitable, Callable

import aiohttp

SERVER_USER = "opencode"
SERVER_PORT = 4096
# One server-sent event is one line. A turn with many tool calls sends message updates far
# above aiohttp's default line limit (128 KiB), and the stream died with LineTooLong
# (staging 2026-11-11, 40 bash calls).
EVENT_LINE_BYTES = 16 * 1024 * 1024
IMAGE = "atlas-opencode-engine:local"

# opencode permission names this engine may auto-allow. Everything else,
# starting with bash, becomes an Atlas approval card.
AUTO_ALLOWED = frozenset({"read", "grep", "glob", "list", "todowrite"})

# Permissions that are always rejected outright.
ALWAYS_REJECT = frozenset({"external_directory"})


def engine_selected() -> bool:
    return os.environ.get("ATLAS_WORK_ENGINE") == "opencode"


def server_url(base: str) -> str:
    return base.rstrip("/") + "/api/atlas/v1"


def basic_auth_header(password: str) -> str:
    return "Basic " + base64.b64encode((SERVER_USER + ":" + password).encode()).decode()


def provider_configuration(
    api_base: str, api_key: str, provider: str, model_id: str
) -> dict[str, Any]:
    """opencode provider config: the Atlas API adapter as an OpenAI provider."""
    return {
        "provider": {
            provider: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "atlas",
                "options": {"baseURL": api_base, "apiKey": api_key},
                "models": {model_id: {}},
            }
        },
        "model": provider + "/" + model_id,
    }


def _short(value: Any, limit: int = 200) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def map_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Map one opencode /event payload to an Atlas Work event, or None."""
    kind = str(payload.get("type", ""))
    info = payload.get("properties") or {}
    if kind == "message.part.updated":
        part = info.get("part") or {}
        part_type = part.get("type")
        if part_type == "text":
            text = str(part.get("text", ""))
            if not text.strip():
                return None
            return {
                "kind": "text",
                "text": text,
                "session_id": info.get("sessionID"),
            }
        if part_type == "tool":
            state = part.get("state") or {}
            return {
                "kind": "tool",
                "tool": part.get("tool"),
                "status": state.get("status"),
                "input": _short(state.get("input")),
                "args": state.get("input")
                if isinstance(state.get("input"), dict)
                else {},
                "output": _short(state.get("output")),
                "session_id": info.get("sessionID"),
            }
        return None
    if kind == "permission.asked":
        patterns = info.get("patterns")
        if isinstance(patterns, list) and patterns:
            pattern = ", ".join(str(p) for p in patterns)
        else:
            pattern = str(info.get("pattern") or info.get("title") or "")
        return {
            "kind": "permission",
            "permission_id": info.get("id"),
            "session_id": info.get("sessionID"),
            "tool": info.get("type") or info.get("permission"),
            "pattern": _short(pattern),
        }
    if kind == "session.idle":
        return {"kind": "turn_end", "session_id": info.get("sessionID")}
    if kind == "session.error":
        return {"kind": "error", "detail": info.get("error") or info}
    return None


def step_usage(payload: dict[str, Any]) -> tuple[int, int, Decimal] | None:
    """Extract (input_tokens, output_tokens, cost) from a step-finish payload."""
    part = (payload.get("properties") or {}).get("part") or {}
    if part.get("type") != "step-finish":
        return None
    tokens = part.get("tokens") or {}
    cost = part.get("cost")
    if not tokens and cost is None:
        return None
    return (
        int(tokens.get("input", 0) or 0),
        int(tokens.get("output", 0) or 0),
        Decimal(str(cost)) if cost is not None else Decimal(0),
    )


def permission_response(permission: str, allowed: bool) -> str | None:
    """Decide an opencode permission without an Atlas card when possible.

    Returns the opencode response to send ("once"/"reject"), or None when the
    permission needs an Atlas approval (the caller's approve callback).
    ``allowed`` is whether web research is enabled for this turn.
    """
    name = str(permission or "")
    if name in ALWAYS_REJECT:
        return "reject"
    if name == "webfetch" and not allowed:
        return "reject"
    if name in AUTO_ALLOWED:
        return "once"
    return None


class OpencodeDriver:
    """Drive one opencode server turn and map its events to Atlas Work events."""

    def __init__(
        self,
        base_url: str,
        password: str,
        emit: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        approve: Callable[[dict[str, Any]], Awaitable[bool]] | None = None,
        cost_limit: float = 0.0,
        time_limit: float = 0.0,
        research: bool = False,
        provider: str = "atlas",
        model_id: str = "",
        token_prices: tuple[Decimal, Decimal] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = basic_auth_header(password)
        self.emit = emit
        self.approve = approve
        self.cost_limit = cost_limit
        self.time_limit = time_limit
        self.research = research
        self.provider = provider
        self.model_id = model_id
        # EUR per million input and output tokens. opencode reports cost 0 for a
        # provider without a price table, so the turn ceiling uses these rates.
        self.token_prices = token_prices
        self.cost = Decimal(0)
        self.input_tokens = 0
        self.output_tokens = 0

    async def _request(
        self, session: aiohttp.ClientSession, method: str, path: str, **kwargs: Any
    ) -> Any:
        async with session.request(
            method,
            self.base_url + path,
            headers={"Authorization": self.auth},
            **kwargs,
        ) as response:
            response.raise_for_status()
            return await response.json(content_type=None)

    async def _abort(self, session: aiohttp.ClientSession, session_id: str) -> None:
        try:
            await self._request(session, "POST", "/session/" + session_id + "/abort")
        except Exception:
            pass

    async def _answer_permission(
        self,
        session: aiohttp.ClientSession,
        mapped: dict[str, Any],
    ) -> None:
        permission = str(mapped.get("tool") or "")
        response = permission_response(permission, self.research)
        if response is None:
            if self.approve is None:
                response = "reject"
            else:
                allowed = await self.approve(mapped)
                response = "once" if allowed else "reject"
        await self._request(
            session,
            "POST",
            "/session/"
            + str(mapped.get("session_id"))
            + "/permissions/"
            + str(mapped.get("permission_id")),
            json={"response": response},
        )

    def _step_cost(self, usage: tuple[int, int, Decimal]) -> Decimal:
        if usage[2] > 0 or self.token_prices is None:
            return usage[2]
        million = Decimal(1_000_000)
        return (
            Decimal(usage[0]) * self.token_prices[0]
            + Decimal(usage[1]) * self.token_prices[1]
        ) / million

    def _payload_session_id(self, payload: dict[str, Any]) -> Any:
        info = payload.get("properties") or {}
        session_id = info.get("sessionID")
        if session_id is None:
            part = info.get("part") or {}
            session_id = part.get("sessionID")
        return session_id

    async def run(self, prompt: str) -> dict[str, Any]:
        """Run one turn; return outcome, cost and the mapped events."""
        events: list[dict[str, Any]] = []
        outcome = "complete"
        started = time.monotonic()
        async with aiohttp.ClientSession() as http:
            created = await self._request(
                http, "POST", "/session", json={"title": "atlas-work"}
            )
            session_id = created["id"]
            stream = await http.get(
                self.base_url + "/event",
                headers={"Authorization": self.auth},
            )
            try:
                await self._request(
                    http,
                    "POST",
                    "/session/" + session_id + "/prompt_async",
                    json={
                        "model": {
                            "providerID": self.provider,
                            "modelID": self.model_id,
                        },
                        "parts": [{"type": "text", "text": prompt}],
                    },
                )
                async with stream:
                    while True:
                        remaining = (
                            self.time_limit - (time.monotonic() - started)
                            if self.time_limit
                            else None
                        )
                        if remaining is not None and remaining <= 0:
                            outcome = "aborted_time"
                            await self._abort(http, session_id)
                            break
                        try:
                            if remaining is None:
                                line = await stream.content.readline(
                                    max_line_length=EVENT_LINE_BYTES
                                )
                            else:
                                line = await asyncio.wait_for(
                                    stream.content.readline(
                                        max_line_length=EVENT_LINE_BYTES
                                    ),
                                    timeout=remaining,
                                )
                        except asyncio.TimeoutError:
                            outcome = "aborted_time"
                            await self._abort(http, session_id)
                            break
                        if not line:
                            # The server closed the stream before the turn ended.
                            outcome = "error"
                            break
                        decoded = line.decode("utf-8", "replace").strip()
                        if not decoded.startswith("data:"):
                            continue
                        payload = json.loads(decoded[5:])
                        payload_session = self._payload_session_id(payload)
                        if payload_session is not None and str(payload_session) != str(
                            session_id
                        ):
                            continue
                        usage = step_usage(payload)
                        if usage is not None:
                            self.input_tokens += usage[0]
                            self.output_tokens += usage[1]
                            self.cost += self._step_cost(usage)
                        if self.cost_limit and self.cost >= Decimal(
                            str(self.cost_limit)
                        ):
                            outcome = "aborted_budget"
                            await self._abort(http, session_id)
                            break
                        mapped = map_event(payload)
                        if mapped is None:
                            continue
                        events.append(mapped)
                        await self.emit(mapped)
                        if mapped["kind"] == "permission":
                            await self._answer_permission(http, mapped)
                        elif mapped["kind"] == "turn_end":
                            break
                        elif mapped["kind"] == "error":
                            outcome = "error"
                            break
            finally:
                stream.close()
        return {
            "outcome": outcome,
            "cost": float(self.cost),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "events": events,
        }
