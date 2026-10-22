"""Local developer API boundary; authenticated identity never comes from a payload."""

from collections.abc import Awaitable, Callable
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from services.orchestrator.dev_auth import Store

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def authenticate(
    request: Request, next_call: Callable[[Request], Awaitable[Response]]
) -> Response:
    store = Store(Path(os.environ.get("ATLAS_DEVAPI_DB", "BRAIN/devapi/usage.sqlite")))
    try:
        bearer = request.headers.get("authorization", "")
        key = request.headers.get("x-api-key", "")
        if any(
            len(request.headers.getlist(h)) > 1 for h in ("authorization", "x-api-key")
        ):
            raise PermissionError("unauthorized")
        if bearer:
            if not bearer.startswith("Bearer ") or (key and key != bearer[7:]):
                raise PermissionError("unauthorized")
            key = bearer[7:]
        request.state.developer = store.authenticate(key)
        request.state.store = store
    except PermissionError:
        return JSONResponse(
            {"error": {"type": "authentication_error", "message": "Unauthorized"}},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        )
    response = await next_call(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/v1/models")
async def models() -> dict[str, object]:
    return {
        "object": "list",
        "data": [{"id": "atlas-code", "object": "model", "owned_by": "atlas"}],
    }


@app.get("/v1/usage")
async def usage(request: Request) -> JSONResponse:
    return JSONResponse(request.state.store.usage(request.state.developer))


@app.post("/v1/chat/completions")
async def completions(request: Request) -> Response:
    from services.orchestrator.dev_inference import chat

    return await chat(request)


@app.post("/v1/responses")
async def responses(request: Request) -> Response:
    from services.orchestrator.dev_inference import chat

    return await chat(request, "responses")


@app.post("/v1/messages")
async def messages(request: Request) -> Response:
    from services.orchestrator.dev_inference import chat

    return await chat(request, "messages")
