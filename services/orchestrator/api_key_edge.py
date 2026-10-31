"""REQ-KEY-03/04: isolated Bearer edge; browser authentication remains unchanged."""

import asyncio
import base64
from contextvars import ContextVar
from decimal import Decimal
import json
import logging
from collections.abc import Awaitable, Callable

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from packages.profiles import PROFILES
from services.orchestrator.api_key_messages import install as install_messages
from services.orchestrator.api_keys import (
    Principal,
    Store,
    KeyQuotaError,
    configured_store,
)
from services.orchestrator.code_ui import project_mode
from services.orchestrator.daily_budget import (
    BudgetRefusal,
    CostReservation,
    DailyLedger,
)
from services.orchestrator.project_ui import broker_identity
from services.retrieval.project_identity import ProjectError

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
key_call: ContextVar[bool] = ContextVar("atlas_key_call", default=False)


def error(status: int, code: str, **values: object) -> JSONResponse:
    return JSONResponse(
        {"error": {"type": code, "code": code, "message": code, **values}},
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            **({"WWW-Authenticate": "Bearer"} if status == 401 else {}),
        },
    )


async def check_member(principal: Principal) -> str | None:
    if not project_mode():
        return None
    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
    ) as client:
        try:
            resolved = await broker_identity(
                client, principal.subject, principal.org_id, None
            )
        except ProjectError as exc:
            if exc.status in {401, 403, 404}:
                raise PermissionError("inactive_member") from None
            raise
    # This token comes directly from our authenticated broker, never from the caller.
    token = resolved["identity"]
    payload = token.split(".")[0]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if (
        claims["member"] != principal.member_id
        or claims["subject"] != principal.subject
        or claims["org"] != principal.org_id
    ):
        raise PermissionError("member_mismatch")
    return str(token)


@app.middleware("http")
async def authenticate(
    request: Request, next_call: Callable[[Request], Awaitable[Response]]
) -> Response:
    store: Store | None = None
    principal: Principal | None = None
    try:
        bearer = request.headers.get("authorization", "")
        if "authorization" not in request.headers:
            bearer = "Bearer " + request.headers.get("x-api-key", "")
        if (
            any(
                len(request.headers.getlist(header)) > 1
                for header in ("authorization", "x-api-key")
            )
            or not bearer.startswith("Bearer atl_")
            or "cookie" in request.headers
            or "origin" in request.headers
        ):
            logging.getLogger("atlas.api_keys").info(
                json.dumps({"event": "failure", "reason": "invalid_key_request"})
            )
            return error(401, "invalid_key_request")
        store = configured_store()
        principal = store.resolve(
            bearer[7:], request.client.host if request.client else ""
        )
        if store.policy(principal.org_id)["mode"] == "off":
            raise PermissionError("policy_denied")
        request.state.scoped_identity = await check_member(principal)
        request.state.api_key = principal
        request.state.key_store = store
        if any(
            name in request.headers for name in ("x-atlas-project", "atlas-project")
        ):
            store.audit(
                "failure", principal.id, reason="scope", missing_scope="project"
            )
            return error(403, "insufficient_scope", missing_scope="project")
        # A processing option, not an identity assertion: read it before the strip below.
        request.state.context_trim = (
            request.headers.get("x-atlas-context-trim", "").lower() == "on"
        )
        request.state.privacy = (
            "strict"
            if request.headers.get("x-atlas-privacy", "").lower() == "strict"
            or principal.privacy == "strict"
            else "standard"
        )
        # Discard every client identity assertion, including the user budget selector.
        request.scope["headers"] = [
            (name, value)
            for name, value in request.scope["headers"]
            if not name.lower().startswith(
                (b"x-atlas-", b"x-openwebui-", b"x-forwarded-")
            )
        ] + [(b"x-openwebui-user-id", principal.budget_user.encode())]
        if hasattr(request, "_headers"):
            del request._headers
        response = await next_call(request)
        response.headers["Cache-Control"] = "no-store"
        return response
    except PermissionError:
        if store is not None and principal is not None:
            store.audit("failure", principal.id, reason="member_authentication")
        return error(401, "unauthorized")
    except ProjectError as exc:
        return error(exc.status, str(exc.body["error"]))
    except (ValueError, KeyError, OSError, aiohttp.ClientError, TimeoutError):
        return error(503, "key_service_unavailable")


def require(request: Request, scope: str) -> Response | None:
    principal: Principal = request.state.api_key
    if scope in principal.scopes:
        return None
    request.state.key_store.audit(
        "failure", principal.id, reason="scope", missing_scope=scope
    )
    return error(403, "insufficient_scope", missing_scope=scope)


@app.get("/models")
def models(request: Request) -> dict[str, object]:
    from api_routing import EFFORTS, PUBLIC_MODELS

    scopes = request.state.api_key.scopes
    # Assistant names keep the full Atlas assistant (tools, search, memory).
    names = (["atlas", *PROFILES] if "assistant" in scopes else []) + (
        list(PUBLIC_MODELS) if "model" in scopes else []
    )
    return {
        "object": "list",
        "data": [
            {
                "id": name,
                "object": "model",
                "created": 0,
                "owned_by": "atlas",
                **(
                    {"reasoning_efforts": list(EFFORTS)}
                    if name in PUBLIC_MODELS
                    else {}
                ),
            }
            for name in names
        ],
    }


@app.get("/usage")
def usage(request: Request) -> dict[str, object]:
    return dict(request.state.key_store.usage(request.state.api_key.id))


@app.post("/chat/completions")
async def chat(request: Request) -> Response:
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 32 * 1024 * 1024:
                    return error(
                        413, "body_limit", measured=len(body), limit=32 * 1024 * 1024
                    )
        raw = json.loads(body)
    except (ValueError, TimeoutError):
        return error(400, "invalid_request")
    if not isinstance(raw, dict):
        return error(400, "invalid_request")
    if (
        any(
            raw.get(name)
            for name in (
                "org_id",
                "project_id",
                "atlas_project",
                "conversation_id",
                "file_version_ids",
                "work_session_id",
                "code_mode",
                "code_session_id",
                "location_ref",
                "cancel_id",
            )
        )
        or raw.get("project_action", "chat") != "chat"
    ):
        request.state.key_store.audit(
            "failure", request.state.api_key.id, reason="scope", missing_scope="project"
        )
        return error(403, "insufficient_scope", missing_scope="project")
    from api_routing import select_public_model
    from services.orchestrator.dev_inference import chat as raw_chat

    # Without a model, an assistant-scoped key keeps the assistant (as before).
    default = "atlas" if "assistant" in request.state.api_key.scopes else "atlas-code"
    name = raw.get("model", default)
    if not isinstance(name, str):
        return error(400, "invalid_model")
    if name == "atlas" or name in PROFILES:
        # The Atlas assistant itself is not a routed usage: keep its own path.
        missing = require(request, "assistant")
        if missing is not None:
            return missing
        if name == "atlas":
            raw["model"] = PROFILES[0]
        raw["atlas_ui"] = False
        request._body = json.dumps(raw).encode()
        from services.orchestrator.chat_api import chat as assistant_chat

        return await assistant_chat(request)
    try:
        public_model = select_public_model(name)
    except ValueError:
        return error(400, "invalid_model")
    missing = require(request, "model")
    if missing is not None:
        return missing
    raw["model"] = "raw/public-routing"
    request._body = json.dumps(raw).encode()
    request.state.store = ModelAccounting(
        request.state.key_store, request.state.api_key
    )
    request.state.developer = request.state.api_key.id
    try:
        return await raw_chat(
            request, raw_model="raw/public-routing", public_model=public_model
        )
    except BudgetRefusal as exc:
        return error(
            429,
            "budget_limit",
            used=str(exc.used),
            limit=str(exc.limit),
            category=exc.category,
        )


@app.post("/responses")
async def responses(request: Request) -> Response:
    from services.orchestrator.api_key_responses import respond

    return await respond(request)


def raw_model_ids() -> list[str]:
    from dev_gateway import raw_models

    return list(raw_models())


class ModelAccounting:
    """Layer member cost accounting over the raw model's existing reservation flow."""

    def __init__(self, store: Store, principal: Principal) -> None:
        self.store, self.principal = store, principal
        self.ledger = DailyLedger()
        self.reservations: dict[str, CostReservation] = {}

    def usage(self, developer: str) -> dict[str, object]:
        return self.store.usage(developer)

    def reserve(self, developer: str, amount: int, *, monetary: bool = True) -> str:
        reservation = self.ledger.reserve_cost(
            self.principal.budget_user,
            Decimal(amount) / 1000000,
            "en",
            user_limits=not self.principal.budget_user.startswith("api-key:"),
        )
        try:
            identifier = self.store.reserve(developer, amount, monetary=monetary)
        except (KeyQuotaError, PermissionError, ValueError):
            self.ledger.settle_cost(reservation, Decimal(0))
            raise
        self.reservations[identifier] = reservation
        return identifier

    def finish(
        self, request_id: str, charged: int | None, incoming: int = 0, outgoing: int = 0
    ) -> None:
        self.store.finish(request_id, charged, incoming, outgoing)
        reservation = self.reservations.pop(request_id)
        self.ledger.settle_cost(
            reservation,
            reservation.amount if charged is None else Decimal(charged) / 1000000,
        )


def principal(request: Request) -> Principal | None:
    value = getattr(request.state, "api_key", None)
    return value if isinstance(value, Principal) else None


install_messages(app)
