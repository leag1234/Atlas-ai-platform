"""REQ-KEY-03/04: isolated Bearer edge; browser authentication remains unchanged."""

import asyncio
import base64
from contextvars import ContextVar
from decimal import Decimal
import json
import os
from uuid import UUID
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


def project_organization(org_id: str) -> bool:
    """Project organizations have UUID ids; keys of the pre-project instance organization
    (for example "stable") are not tied to project membership."""
    try:
        UUID(org_id)
    except ValueError:
        return False
    return True


async def check_member(principal: Principal) -> str | None:
    # Measured 2026-11-01: turning project mode on rejected every existing key of the
    # guest instance (org "stable") with 422, because they were checked as project members.
    if not project_mode() or not project_organization(principal.org_id):
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


def model_names(request: Request) -> list[str]:
    from api_routing import PUBLIC_MODELS

    scopes = request.state.api_key.scopes
    # Assistant names keep the full Atlas assistant (tools, search, memory).
    names = []
    if "assistant" in scopes:
        names.extend(["atlas", *PROFILES])
    if "model" in scopes:
        names.extend([*PUBLIC_MODELS, EMBEDDING_MODEL])
        if tts_configured():
            names.append(TTS_MODEL)
    return names


def messages_client(request: Request) -> bool:
    from packages.api_content import messages_protocol

    # Messages-format clients get model objects in that format.
    return messages_protocol(request.headers)


def openai_model(name: str) -> dict[str, object]:
    from api_routing import EFFORTS, PUBLIC_MODELS

    return {
        "id": name,
        "object": "model",
        "created": 0,
        "owned_by": "atlas",
        **({"reasoning_efforts": list(EFFORTS)} if name in PUBLIC_MODELS else {}),
    }


def messages_model(name: str) -> dict[str, object]:
    return {
        "type": "model",
        "id": name,
        "display_name": name,
        "created_at": "1970-01-01T00:00:00Z",
    }


@app.get("/models", response_model=None)
def models(request: Request) -> Response | dict[str, object]:
    names = model_names(request)
    if not messages_client(request):
        return {"object": "list", "data": [openai_model(name) for name in names]}
    # Messages-format lists are paginated with limit, after_id and before_id.
    query = request.query_params
    try:
        limit = int(query.get("limit", "20"))
    except ValueError:
        limit = 0
    if not 1 <= limit <= 1000:
        return messages_error(400, "invalid_request_error", "limit: 1 to 1000")
    start, stop = 0, len(names)
    if "after_id" in query and query["after_id"] in names:
        start = names.index(query["after_id"]) + 1
    if "before_id" in query and query["before_id"] in names:
        stop = names.index(query["before_id"])
        start = max(start, stop - limit)
    page = names[start:stop][:limit]
    return {
        "data": [messages_model(name) for name in page],
        "has_more": start + len(page) < stop,
        "first_id": page[0] if page else None,
        "last_id": page[-1] if page else None,
    }


@app.get("/models/{model_id}", response_model=None)
def model(model_id: str, request: Request) -> Response | dict[str, object]:
    if model_id not in model_names(request):
        message = f"The model {model_id!r} does not exist"
        if messages_client(request):
            return messages_error(404, "not_found_error", message)
        return JSONResponse(
            {
                "error": {
                    "message": message,
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "model_not_found",
                }
            },
            status_code=404,
        )
    if messages_client(request):
        return messages_model(model_id)
    return openai_model(model_id)


def messages_error(status: int, kind: str, message: str) -> Response:
    return JSONResponse(
        {"type": "error", "error": {"type": kind, "message": message}},
        status_code=status,
    )


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

    # Without a model every key gets the Atlas assistant: the default never depends on the key.
    default = "atlas"
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


EMBEDDING_MODEL = "atlas-embed"
TTS_MODEL = "atlas-tts"
TTS_MAX_INPUT_CHARS = 12000
TTS_MAX_BODY_BYTES = 64 * 1024
TTS_MICRO_EUR_PER_CHARACTER = 1


def tts_url() -> str:
    """Base URL of the local speech service; empty when speech is not enabled here."""
    return os.environ.get("ATLAS_TTS_URL", "").rstrip("/")


def tts_configured() -> bool:
    return bool(tts_url())


@app.post("/embeddings")
async def embeddings(request: Request) -> Response:
    """OpenAI-format embeddings (API parity, phase 2); any model name is served by atlas-embed."""
    import embeddings as gateway_embeddings

    missing = require(request, "model")
    if missing is not None:
        return missing
    # Read with a bound, as the chat route does: a body is never held unbounded in memory.
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > gateway_embeddings.MAX_BODY_BYTES:
                    return error(
                        413,
                        "body_limit",
                        measured=len(body),
                        limit=gateway_embeddings.MAX_BODY_BYTES,
                    )
        raw = json.loads(body)
    except (ValueError, TimeoutError):
        return error(400, "invalid_request")
    if not isinstance(raw, dict):
        return error(400, "invalid_request")
    unknown = set(raw) - {"model", "input", "encoding_format", "dimensions", "user"}
    if unknown:
        return error(
            400,
            "invalid_request",
            message="unsupported field: " + ", ".join(sorted(unknown))[:200],
        )
    texts = raw.get("input")
    if isinstance(texts, str):
        texts = [texts]
    if (
        not isinstance(texts, list)
        or not texts
        or not all(isinstance(text, str) and text for text in texts)
    ):
        return error(
            400,
            "invalid_request",
            message="input: a non-empty string or list of non-empty strings "
            "(token arrays are not supported)",
        )
    if len(texts) > gateway_embeddings.MAX_INPUTS:
        return error(
            400,
            "input_limit",
            measured=len(texts),
            limit=gateway_embeddings.MAX_INPUTS,
            unit="inputs",
        )
    total = sum(len(text.encode()) for text in texts)
    if total > gateway_embeddings.MAX_TOTAL_BYTES:
        return error(
            400,
            "input_limit",
            measured=total,
            limit=gateway_embeddings.MAX_TOTAL_BYTES,
            unit="bytes",
        )
    longest = max(len(text.encode()) for text in texts)
    if longest > gateway_embeddings.MAX_INPUT_BYTES:
        return error(
            400,
            "context_exceeded",
            measured=longest,
            limit=gateway_embeddings.MAX_INPUT_BYTES,
            unit="bytes",
        )
    encoding = raw.get("encoding_format", "float")
    dimensions = raw.get("dimensions")
    if encoding not in ("float", "base64") or (
        dimensions is not None
        and (type(dimensions) is not int or not 32 <= dimensions <= 4096)
    ):
        return error(
            400,
            "invalid_request",
            message="encoding_format: float or base64; dimensions: 32 to 4096",
        )
    if request.state.privacy == "strict":
        return error(
            503,
            "strict_privacy_unavailable",
            message="No owned embedding server is available for strict privacy mode.",
        )
    store = ModelAccounting(request.state.key_store, request.state.api_key)
    developer = request.state.api_key.id
    try:
        request_id = store.reserve(developer, gateway_embeddings.estimated_micro(texts))
    except BudgetRefusal as exc:
        return error(
            429,
            "budget_limit",
            used=str(exc.used),
            limit=str(exc.limit),
            category=exc.category,
        )
    except KeyQuotaError as exc:
        return error(429, "quota_exceeded", **exc.values)
    except (PermissionError, ValueError):
        return error(429, "quota_exceeded")
    try:
        result = await gateway_embeddings.embed(
            texts, dimensions=dimensions, encoding=encoding, timeout=120
        )
    except (RuntimeError, OSError, TimeoutError, aiohttp.ClientError):
        store.finish(request_id, 0)
        return error(502, "provider_error")
    store.finish(request_id, result["cost_micro"], result["tokens"], 0)
    logging.getLogger("atlas.api").info(
        json.dumps(
            {
                "event": "api_embeddings",
                "key": developer,
                "inputs": len(texts),
                "tokens": result["tokens"],
            }
        )
    )
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {"object": "embedding", "index": index, "embedding": vector}
                for index, vector in enumerate(result["vectors"])
            ],
            "model": EMBEDDING_MODEL,
            "usage": {
                "prompt_tokens": result["tokens"],
                "total_tokens": result["tokens"],
            },
        }
    )


def not_stored(kind: str) -> JSONResponse:
    from services.orchestrator.dev_input import STORAGE_MESSAGE

    return JSONResponse(
        {
            "error": {
                "message": STORAGE_MESSAGE,
                "type": "invalid_request_error",
                "param": None,
                "code": "storage_not_supported",
                "object": kind,
            }
        },
        status_code=404,
    )


@app.post("/audio/speech")
async def speech(request: Request) -> Response:
    """OpenAI-format speech synthesis, served by the local speech service."""
    missing = require(request, "model")
    if missing is not None:
        return missing
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > TTS_MAX_BODY_BYTES:
                    return error(
                        413, "body_limit", measured=len(body), limit=TTS_MAX_BODY_BYTES
                    )
        raw = json.loads(body)
    except (ValueError, TimeoutError):
        return error(400, "invalid_request")
    if not isinstance(raw, dict):
        return error(400, "invalid_request")
    unknown = set(raw) - {
        "model",
        "input",
        "voice",
        "response_format",
        "speed",
        "instructions",
    }
    if unknown:
        return error(
            400,
            "invalid_request",
            message="unsupported field: " + ", ".join(sorted(unknown))[:200],
        )
    text = raw.get("input")
    if not isinstance(text, str) or not text:
        return error(400, "invalid_request", message="input: a non-empty string")
    if len(text) > TTS_MAX_INPUT_CHARS:
        return error(
            400,
            "input_limit",
            measured=len(text),
            limit=TTS_MAX_INPUT_CHARS,
            unit="characters",
        )
    response_format = raw.get("response_format", "mp3")
    if response_format not in {"mp3", "wav"}:
        return error(400, "invalid_request", message="response_format: mp3 or wav")
    speed = raw.get("speed", 1.0)
    if speed != 1.0:
        return error(400, "invalid_request", message="speed: only 1.0 is supported")
    if "instructions" in raw:
        return error(
            400,
            "invalid_request",
            message="instructions are not supported",
        )
    if not tts_configured():
        return error(503, "tts_unavailable")
    store = ModelAccounting(request.state.key_store, request.state.api_key)
    developer = request.state.api_key.id
    try:
        request_id = store.reserve(developer, len(text) * TTS_MICRO_EUR_PER_CHARACTER)
    except BudgetRefusal as exc:
        return error(
            429,
            "budget_limit",
            used=str(exc.used),
            limit=str(exc.limit),
            category=exc.category,
        )
    except KeyQuotaError as exc:
        return error(429, "quota_exceeded", **exc.values)
    except (PermissionError, ValueError):
        return error(429, "quota_exceeded")
    try:
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=120)
        ) as client:
            async with client.post(
                tts_url() + "/v1/audio/speech",
                json={
                    "model": TTS_MODEL,
                    "input": text,
                    "response_format": response_format,
                },
            ) as upstream:
                if 400 <= upstream.status < 500:
                    # The service refused the text itself (nothing speakable): the
                    # caller's input, not an outage.
                    store.finish(request_id, 0)
                    return error(
                        400, "invalid_request", message="input has no speakable text"
                    )
                if upstream.status != 200:
                    store.finish(request_id, 0)
                    return error(503, "tts_unavailable")
                audio = await upstream.read()
                voice = upstream.headers.get("X-Atlas-TTS-Voice", "")
    except (OSError, TimeoutError, aiohttp.ClientError):
        store.finish(request_id, 0)
        return error(503, "tts_unavailable")
    store.finish(request_id, len(text) * TTS_MICRO_EUR_PER_CHARACTER, 0, 0)
    logging.getLogger("atlas.api").info(
        json.dumps({"event": "api_speech", "key": developer, "characters": len(text)})
    )
    return Response(
        audio,
        media_type="audio/wav" if response_format == "wav" else "audio/mpeg",
        headers={"X-Atlas-TTS-Voice": voice},
    )


# Retrieval, deletion and cancellation need stored objects: nothing is stored.
@app.api_route("/responses/{response_id}", methods=["GET", "DELETE"])
async def stored_response(response_id: str) -> Response:
    return not_stored("response")


@app.api_route("/responses/{response_id}/{action}", methods=["GET", "POST"])
async def stored_response_action(response_id: str, action: str) -> Response:
    return not_stored("response")


@app.api_route("/conversations", methods=["GET", "POST"])
@app.api_route("/conversations/{path:path}", methods=["GET", "POST", "DELETE"])
async def stored_conversation(path: str = "") -> Response:
    return not_stored("conversation")


@app.get("/chat/completions")
@app.api_route("/chat/completions/{path:path}", methods=["GET", "POST", "DELETE"])
async def stored_completion(path: str = "") -> Response:
    return not_stored("chat.completion")


@app.post("/responses/input_tokens")
async def response_input_tokens(request: Request) -> Response:
    from packages.api_content import estimated_tokens
    from services.orchestrator.dev_input import responses as parse_responses

    missing = require(request, "model")
    if missing is not None:
        return missing
    try:
        raw = json.loads(await request.body())
        parsed = parse_responses(dict(raw, model="atlas-code", stream=False))
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return error(400, "invalid_request")
    inputs = parsed.model_dump(
        include={"messages", "tools"}, by_alias=True, exclude_none=True
    )
    return JSONResponse(
        {"object": "response.input_tokens", "input_tokens": estimated_tokens(inputs)}
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
