"""REQ-KEY-03/04: public Messages routes over the existing developer adapters."""

import asyncio
from collections.abc import Awaitable, Callable
import json
from typing import cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.responses import StreamingResponse

from services.orchestrator.daily_budget import BudgetRefusal
from services.orchestrator.dev_input import messages as parse_messages

PATHS = ("/messages", "/v1/messages")


def is_messages(request: Request) -> bool:
    path = request.scope["path"].removeprefix(request.scope.get("root_path", ""))
    return path.rstrip("/") in (*PATHS, *(p + "/count_tokens" for p in PATHS))


def error(status: int, message: str, **values: object) -> JSONResponse:
    kind = {
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        413: "request_too_large",
        429: "rate_limit_error",
    }.get(status, "api_error" if status >= 500 else "invalid_request_error")
    return JSONResponse(
        {"type": "error", "error": {"type": kind, "message": message, **values}},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


async def errors(
    request: Request, next_call: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await next_call(request)
    if not is_messages(request) or response.status_code < 400:
        return response
    # call_next returns Starlette's streaming wrapper even for JSON errors.
    stream = cast(StreamingResponse, response)
    body = b"".join(
        [
            part.encode() if isinstance(part, str) else bytes(part)
            async for part in stream.body_iterator
        ]
    )
    value = json.loads(body)
    detail = value.get("error", {})
    mapped = error(
        response.status_code,
        str(detail.get("message", value.get("detail", "request_failed"))),
        **{k: v for k, v in detail.items() if k not in {"type", "message"}},
    )
    for name, content in response.headers.items():
        if name not in {"content-length", "content-type"}:
            mapped.headers[name] = content
    return mapped


async def messages(request: Request) -> Response:
    from api_routing import select_public_model
    from services.orchestrator.api_key_edge import ModelAccounting, require
    from services.orchestrator.dev_inference import chat

    missing = require(request, "model")
    if missing is not None:
        return missing
    try:
        body = bytearray()
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1048576:
                    return error(413, "body_limit", measured=len(body), limit=1048576)
        raw = json.loads(body)
        if not isinstance(raw, dict) or not isinstance(raw.get("model"), str):
            return error(400, "invalid_request")
        if (
            any(
                raw.get(field)
                for field in (
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
                "failure",
                request.state.api_key.id,
                reason="scope",
                missing_scope="project",
            )
            return error(403, "insufficient_scope", missing_scope="project")
        try:
            model = select_public_model(raw["model"])
        except (KeyError, ValueError):
            return error(400, "invalid_model")
        if request.url.path.endswith("/count_tokens"):
            parsed = parse_messages(
                dict(raw, model="atlas-code", max_tokens=raw.get("max_tokens", 1))
            )
            inputs = parsed.model_dump(
                include={"messages", "tools"}, by_alias=True, exclude_none=True
            )
            return JSONResponse(
                {
                    "input_tokens": len(json.dumps(inputs, ensure_ascii=False).encode())
                    + 512
                }
            )
        raw["model"] = "raw/public-routing"
        request._body = json.dumps(raw).encode()
        request.state.store = ModelAccounting(
            request.state.key_store, request.state.api_key
        )
        request.state.developer = request.state.api_key.id
        return await chat(
            request,
            "messages",
            raw_model="raw/public-routing",
            public_model=model,
        )
    except BudgetRefusal as exc:
        return error(
            429,
            "budget_limit",
            used=str(exc.used),
            limit=str(exc.limit),
            category=exc.category,
        )
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
        TimeoutError,
    ):
        return error(400, "invalid_request")


def install(app: FastAPI) -> None:
    for path in PATHS:
        app.add_api_route(path, messages, methods=["POST"])
        app.add_api_route(path + "/count_tokens", messages, methods=["POST"])
    app.middleware("http")(errors)
