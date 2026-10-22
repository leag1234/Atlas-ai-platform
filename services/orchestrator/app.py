"""Loopback HTTP entry point for the bounded, read-only M3 harness."""

import os
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query as Parameter
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from services.orchestrator.cache import Cache
from services.orchestrator.loop import Limits, Query, run
from services.orchestrator.model import GatewayModel
from services.orchestrator.tools import Runtime
from packages.ports import atlas_ports

app = FastAPI(title="ATLAS-0 harness", version="0.1.0")


@app.exception_handler(RequestValidationError)
async def invalid_request(
    request: Request, error: RequestValidationError
) -> JSONResponse:
    # Avoid echoing rejected input, including unencodable Unicode surrogates.
    return JSONResponse({"detail": "invalid_request"}, status_code=422)


def cache() -> Cache:
    return Cache(Path(os.environ.get("ATLAS_WEB_CACHE", "BRAIN/web-cache.sqlite")))


@app.post("/query")
async def query(request: Query) -> dict[str, object]:
    started = time.monotonic()
    try:
        model = await GatewayModel.connect(
            os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway"))
        )
    except (ValueError, RuntimeError, OSError):
        raise HTTPException(503, "gateway_unavailable") from None
    result = await run(
        request,
        model,
        Runtime(cache(), Decimal(0), request.question),
        Path("prompts/agent.txt").read_text(),
        Limits(wall_clock=max(0, 120 - (time.monotonic() - started))),
    )
    return dict(jsonable_encoder(asdict(result)))


@app.get("/continuation/{handle}")
async def continuation(
    handle: str, offset: int = Parameter(default=0, ge=0, le=2_000_000)
) -> dict[str, object]:
    if len(handle) != 64 or any(c not in "0123456789abcdef" for c in handle):
        raise HTTPException(400, "invalid_handle")
    stored = cache().get(handle)
    if stored is None or "text" not in stored:
        raise HTTPException(404, "expired_handle")
    text = str(stored["text"])
    part = text[offset : offset + 8000]
    return {
        "trust": "untrusted",
        "url": stored["url"],
        "text": part,
        "next_offset": offset + len(part) if offset + len(part) < len(text) else None,
    }
