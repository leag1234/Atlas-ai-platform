"""REQ-KEY-03/04: public Responses using the shared raw inference adapter."""

import asyncio
import json
import os

from fastapi import Request
from fastapi.responses import Response
from serverless import ServerlessPolicy

from services.orchestrator.api_key_edge import ModelAccounting, error, require
from services.orchestrator.daily_budget import BudgetRefusal
from services.orchestrator.dev_inference import chat
from dev_gateway import raw_models


async def respond(request: Request) -> Response:
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
    except (ValueError, TimeoutError, RecursionError):
        return error(400, "invalid_request")

    models = raw_models()
    default = os.environ.get("ATLAS_API_RESPONSES_MODEL", "atlas-code")
    code = "raw/" + ServerlessPolicy(allow_unpriced=True).models["code"]
    default = code if default == "atlas-code" else default
    selected = code if raw["model"] == "atlas-code" else raw["model"]
    if selected not in models:
        selected = default
    if selected not in models:
        return error(503, "invalid_default_model")
    request._body = json.dumps(raw).encode()
    request.state.store = ModelAccounting(
        request.state.key_store, request.state.api_key
    )
    request.state.developer = request.state.api_key.id
    try:
        return await chat(request, "responses", raw_model=selected)
    except BudgetRefusal as exc:
        return error(
            429,
            "budget_limit",
            used=str(exc.used),
            limit=str(exc.limit),
            category=exc.category,
        )
