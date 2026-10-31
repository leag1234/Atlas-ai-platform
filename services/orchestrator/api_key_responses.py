"""REQ-KEY-03/04: public Responses using the shared raw inference adapter."""

import asyncio
import json

from fastapi import Request
from fastapi.responses import Response

from services.orchestrator.api_key_edge import ModelAccounting, error, require
from services.orchestrator.daily_budget import BudgetRefusal
from services.orchestrator.dev_inference import chat
from api_routing import select_public_model

KEY_DEFAULT_OUTPUT_TOKENS = 16384


async def respond(request: Request) -> Response:
    missing = require(request, "model")
    if missing is not None:
        return missing
    try:
        body = bytearray()
        async with asyncio.timeout(60):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 8388608:
                    return error(413, "body_limit", measured=len(body), limit=8388608)
        raw = json.loads(body)
        if not isinstance(raw, dict) or not isinstance(raw.get("model"), str):
            return error(400, "invalid_request")
    except (ValueError, TimeoutError, RecursionError):
        return error(400, "invalid_request")

    try:
        public_model = select_public_model(raw["model"])
    except ValueError:
        return error(400, "invalid_model")
    selected = "raw/public-routing"
    raw["model"] = selected
    # Codex sends no max_output_tokens; one long tool call (a whole file) can exceed
    # 4096 tokens and end the turn as incomplete (reported 2026-10-31).
    raw.setdefault("max_output_tokens", KEY_DEFAULT_OUTPUT_TOKENS)
    request._body = json.dumps(raw).encode()
    request.state.store = ModelAccounting(
        request.state.key_store, request.state.api_key
    )
    request.state.developer = request.state.api_key.id
    try:
        return await chat(
            request,
            "responses",
            raw_model=selected,
            public_model=public_model,
        )
    except BudgetRefusal as exc:
        return error(
            429,
            "budget_limit",
            used=str(exc.used),
            limit=str(exc.limit),
            category=exc.category,
        )
