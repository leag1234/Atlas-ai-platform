"""Loopback OpenAI-compatible adapter with one journal row on every outcome."""

from packages.profiles import PROFILES
from packages.limits import LimitError
from packages.configuration import (
    configuration_names,
    guest_configuration_failure,
    temporarily_unavailable,
)
import logging
import re

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from packages.validation import describe_validation
from packages.image_upload import UploadError
from services.orchestrator.image_store import prepare_uploads
import html

from services.orchestrator.chat_pipeline import process, source
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.source_truth import visible_sources
from services.orchestrator.stream_client import sink_context, metadata_only_context
from services.orchestrator.deadline import request_deadline
from services.orchestrator.chat_stream import response as stream_response
from services.orchestrator.model import GatewayError
from services.orchestrator.interactions import Interaction, write_interaction
from services.orchestrator.daily_budget import (
    BudgetRefusal,
    DailyLedger,
    maintenance_message,
    user_from_headers,
)
from services.orchestrator.cancellation import cancel

from services.orchestrator.project_ui import (
    ProjectGate,
    browser_origin,
    public_error,
    router as project_router,
)
from services.retrieval.project_identity import ProjectError
from services.orchestrator.code_ui import project_mode
from services.orchestrator.mcp_confirmation import router as mcp_router
from services.orchestrator.connectors import router as connector_router
from services.orchestrator.code_api import router as code_router

from services.orchestrator.image_store import router as image_router

from services.orchestrator.app import app as harness_app
from services.orchestrator.user_memory_ui import router as user_memory_router

from services.orchestrator.api_key_edge import (
    app as key_app,
    principal as key_principal,
    key_call,
)
from services.orchestrator.api_keys import KeyQuotaError
from services.orchestrator.api_key_ui import router as key_ui_router


app = FastAPI(title="ATLAS chat")
app.add_middleware(ProjectGate)
app.mount("/harness", harness_app)

app.include_router(image_router)

app.include_router(mcp_router)
app.include_router(connector_router)

app.include_router(code_router)


@app.post("/api/atlas/nearby-location")
async def nearby_location(request: Request) -> Response:
    from services.orchestrator.location import Location, location_tickets

    if request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
        return JSONResponse({"error": "origin_rejected"}, status_code=403)
    try:
        body = bytearray()
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 2048:
                    return JSONResponse(
                        {"error": "location_size_limit"}, status_code=413
                    )
        location = Location.model_validate_json(body)
        token = location_tickets.issue(location)
    except (ValueError, TimeoutError):
        return JSONResponse(
            {"error": "location_invalid_or_unavailable"}, status_code=400
        )
    return JSONResponse({"location_ref": token}, headers={"Cache-Control": "no-store"})


@app.delete("/api/atlas/nearby-location/{token}")
async def remove_nearby_location(token: UUID, request: Request) -> Response:
    from services.orchestrator.location import location_tickets

    if request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
        return Response(status_code=403)
    location_tickets.take(str(token))
    return Response(status_code=204)


app.include_router(key_ui_router)
app.include_router(user_memory_router)
app.mount("/api/atlas/v1", key_app)
app.include_router(project_router)


_UPSTREAM_URL = re.compile(r"https?://[^\s'\"]+", re.I)


def public_error_detail(detail: str) -> str:
    """Keep upstream topology out of API errors and therefore host UI toasts."""
    return _UPSTREAM_URL.sub("upstream service", detail)


@app.post("/atlas/cancel/{token}")
async def cancel_chat(token: UUID, request: Request) -> Response:
    """Cancel one browser-owned stream without relying on proxy disconnects."""
    if request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
        return JSONResponse({"error": "origin_rejected"}, status_code=403)
    return Response(status_code=202 if cancel(str(token)) else 404)


@app.post("/v1/audio/{operation}")
async def trial_audio(operation: str) -> JSONResponse:
    return JSONResponse(
        {
            "error": "speech_unavailable",
            "message": "Speech is not enabled on this installation.",
        },
        status_code=503,
    )


@app.get("/v1/models")
def models() -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": 0, "owned_by": "atlas"}
            for name in PROFILES
        ],
    }


@app.get("/sources/{key}")
async def sources(key: str) -> Response:
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        return JSONResponse({"error": "invalid_chunk_id"}, status_code=400)
    try:
        passage = await source(key)
    except (ValueError, RuntimeError, OSError):
        return JSONResponse({"error": "source_unavailable"}, status_code=404)
    return HTMLResponse(
        "<meta charset=utf-8><title>Source ATLAS</title><h1>"
        + html.escape(str(passage["source"]))
        + "</h1><pre style='white-space:pre-wrap'>"
        + html.escape(str(passage["text"]))
        + "</pre>",
        headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"
        },
    )


async def process_request(payload: ChatRequest, item: Interaction) -> None:
    token = key_call.set(payload._api_key_request)
    downstream = sink_context.get()

    async def collect(event: dict[str, object]) -> None:
        if "phase" in event and event["phase"] != "generating":
            item.steps.append(dict(event))
        if downstream:
            await downstream(event)

    metadata_token = metadata_only_context.set(not payload.stream)
    sink_token = sink_context.set(collect)
    try:
        await process(payload, item)
    finally:
        sink_context.reset(sink_token)
        metadata_only_context.reset(metadata_token)
        key_call.reset(token)


async def execute(
    request: Request, payload: ChatRequest, item: Interaction, remaining: float
) -> None:
    async with request_deadline(remaining):
        task = asyncio.create_task(process_request(payload, item))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.1)
                if await request.is_disconnected():
                    raise asyncio.CancelledError
            await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Response:
    authenticated_key = key_principal(request)
    if authenticated_key is not None:
        pass  # Only the mounted Bearer edge can populate this server-side principal.
    elif project_mode() and (
        request.headers.get("origin") is not None
        or request.headers.get("sec-fetch-site") is not None
        or request.headers.get("authorization", "").startswith("Bearer atl_")
    ):
        # Browser calls and Atlas API keys keep the strict project origin check
        # (keys must go through the key edge); the chat UI backend calls this
        # endpoint server to server, without browser fetch metadata.
        try:
            browser_origin(request)
        except ProjectError as exc:
            return public_error(exc)
    elif request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
        return JSONResponse({"error": "origin_rejected"}, status_code=403)
    item, started = Interaction(), time.monotonic()
    status, code, payload = 200, "", None
    streaming = False
    detail = ""
    try:
        body = bytearray()
        async with asyncio.timeout(5):
            async for piece in request.stream():
                body.extend(piece)
                if len(body) > 32 * 1024 * 1024:
                    status, code = 413, "request_size_exceeded"
                    detail = f"Request body: {len(body)} bytes, limit {32 * 1024 * 1024} bytes"
                    break
        if not code:
            try:
                raw = json.loads(body)
                if (
                    os.environ.get("ATLAS_TRIAL") == "1"
                    and isinstance(raw, dict)
                    and raw.get("code_mode") is True
                ):
                    return JSONResponse(
                        {
                            "error": "code_mode_unavailable",
                            "message": "Code mode is unavailable in the Docker trial. See the full stack installation guide.",
                        },
                        status_code=503,
                    )
                if (
                    isinstance(raw, dict)
                    and isinstance(raw.get("messages"), list)
                    and raw["messages"]
                ):
                    last = raw["messages"][-1]
                    if isinstance(last, dict):
                        content = last.get("content", "")
                        if isinstance(content, str):
                            item.question = content[:32000]
                        elif isinstance(content, list):
                            item.question = "\n".join(
                                p["text"]
                                for p in content
                                if isinstance(p, dict)
                                and p.get("type") == "text"
                                and isinstance(p.get("text"), str)
                            )[:32000]
                from services.orchestrator.project_transcript import authenticate_chat

                scoped_subject, scoped_key = await authenticate_chat(request, raw)
                raw, item.uploads, item.uploaded_images = await asyncio.to_thread(
                    prepare_uploads, raw, user_from_headers(request.headers)
                )
                if isinstance(raw, dict) and "location_ref" in raw:
                    from services.orchestrator.location import location_tickets

                    reference = raw.pop("location_ref")
                    if isinstance(reference, str):
                        raw["location"] = location_tickets.take(reference)
                payload = ChatRequest.model_validate(raw)
                payload._api_key_request = authenticated_key is not None
                payload._project_subject = scoped_subject
                payload._transcript_key = scoped_key
                from services.orchestrator.connectors import authenticate

                if not payload._api_key_request:
                    await authenticate(request, payload)
                if not payload.org_id:
                    from services.orchestrator.skill_catalog import ordinary_catalog

                    payload._skill_catalog = await ordinary_catalog(request)
            except UploadError as exc:
                status, code, detail = 413, exc.code, str(exc)
            except ValidationError as exc:
                status, code = 400, "invalid_request"
                detail = describe_validation(exc)
            except json.JSONDecodeError:
                status = 413 if len(body) > 160000 else 400
                code = "invalid_request"
                detail = f"Malformed JSON: {len(body)} bytes; malformed-body diagnostic limit 160000 bytes"
            except (ValueError, OSError):
                status, code, detail = (
                    400,
                    "invalid_request",
                    "Invalid JSON or image data",
                )
        if payload is not None:
            from packages.language import conversation_language

            lang = conversation_language(
                payload.conversation_messages, payload.ui_locale
            )
            if (
                os.environ.get("ATLAS_KILL_SWITCH_FILE")
                and Path(os.environ["ATLAS_KILL_SWITCH_FILE"]).exists()
            ):
                raise BudgetRefusal("maintenance", 0, 0, lang)
            user, ledger = user_from_headers(request.headers), DailyLedger()
            reserve = Decimal("0.30" if payload.has_attachments else "0.10")
            if payload.code_mode and project_mode():
                return public_error(ProjectError(409, "feature_not_available"))
            if payload.code_mode or payload.project_action == "work":
                from services.orchestrator.code_mode import settings

                reserve = settings().cost
            reservation = ledger.reserve_cost(user, reserve, lang)
            key_reservation = None
            if authenticated_key is not None:
                try:
                    key_reservation = request.state.key_store.reserve(
                        authenticated_key.id, int(reserve * 1000000)
                    )
                except (KeyQuotaError, PermissionError, ValueError):
                    ledger.settle_cost(reservation, Decimal(0))
                    raise

            def settle(result: Interaction) -> None:
                ledger.settle_cost(reservation, Decimal(str(result.cout_eur)))
                if key_reservation is not None:
                    request.state.key_store.finish(
                        key_reservation,
                        int(Decimal(str(result.cout_eur)) * 1000000)
                        if result.state == "done"
                        else None,
                        result.tokens["in"],
                        result.tokens["out"],
                    )

            payload.__dict__["daily_ledger"] = ledger
            payload.__dict__["daily_user"] = user
            item.question = payload.user_text
            if payload.stream:
                streaming = True

                async def budgeted(p: ChatRequest, result: Interaction) -> None:
                    try:
                        await process_request(p, result)
                    finally:
                        settle(result)

                return stream_response(payload, item, started, budgeted)
            try:
                await execute(
                    request,
                    payload,
                    item,
                    max(0, payload.timeout_seconds - (time.monotonic() - started)),
                )
            finally:
                settle(item)
            if item.state != "done":
                status = (
                    429
                    if any(e in {"cost", "cost_budget"} for e in item.erreurs)
                    else 502
                    if "provider_error" in item.erreurs
                    else 504
                )
                code = "cost_budget" if status == 429 else "request_stopped"
                detail = item.reponse
    except KeyQuotaError as exc:
        from services.orchestrator.api_key_edge import error

        return error(429, "quota_exceeded", **exc.values)
    except PermissionError:
        from services.orchestrator.api_key_edge import error

        return error(401, "unauthorized")
    except ProjectError as exc:
        return public_error(exc)
    except asyncio.CancelledError:
        status, code = 499, "cancelled"
    except GatewayError as exc:
        item.provider_retries += exc.retries
        status, code = exc.status, exc.code
        if guest_configuration_failure(exc):
            logging.getLogger(__name__).error(
                "Configuration failure; variables=%s", configuration_names(exc)
            )
            detail = temporarily_unavailable(payload.lang if payload else "en")
        else:
            detail = exc.detail
    except BudgetRefusal as exc:
        status, code, detail = (
            429,
            "budget_limit",
            (
                maintenance_message(exc.lang)
                if exc.category == "maintenance"
                else exc.message
            ),
        )
        if payload is not None and payload.stream:

            async def refusal() -> AsyncIterator[str]:
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "object": "chat.completion.chunk",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": detail},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                    )
                    + "\n\n"
                )
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                refusal(), status_code=429, media_type="text/event-stream"
            )
    except LimitError as exc:
        status, code, detail = (
            (429 if exc.unit in {"microEUR", "micro EUR"} else 502),
            exc.code,
            exc.detail,
        )
    except TimeoutError:
        status, code = 504, "timeout"
        limit = (
            5.0 if payload is None else payload.timeout_seconds + item.startup_seconds
        )
        detail = f"Timeout: {time.monotonic() - started:.3f} seconds elapsed, limit {limit:.3f} seconds"
    except Exception as exc:
        # Never reflect provider/transport exception text into logs or the client.
        status, code = 502, "provider_error"
        if guest_configuration_failure(exc):
            logging.getLogger(__name__).error(
                "Configuration failure; variables=%s", configuration_names(exc)
            )
            detail = temporarily_unavailable(payload.lang if payload else "en")
    finally:
        if code:
            item.erreurs.append(code)
            item.state, item.reponse = "error", detail
            item.rejection = {"code": code, "message": detail}
        if not streaming:
            item.latence_ms["total"] = (time.monotonic() - started) * 1000
            write_interaction(
                item,
                Path(os.environ.get("ATLAS_INTERACTION_DIR", "BRAIN/interactions")),
            )
    if code:
        detail = public_error_detail(detail)
        return JSONResponse(
            {
                "error": {"message": detail or code, "type": code, "code": code},
                "atlas": {
                    "context": item.context_samples[-1]
                    if item.context_samples
                    else None,
                    "context_samples": item.context_samples,
                    "context_manifest": item.context_manifest,
                    "cost_eur": item.cout_eur,
                    "cost_kind": "reserved_upper_bound",
                    "provider_retries": item.provider_retries,
                },
            },
            status_code=status,
        )
    usage = {
        "prompt_tokens": item.tokens["in"],
        "completion_tokens": item.tokens["out"],
        "total_tokens": sum(item.tokens.values()),
    }
    assert payload is not None
    base = {"id": item.request_id, "created": int(time.time()), "model": payload.model}
    message = {"role": "assistant", "content": item.reponse}
    if item.reasoning:
        message["reasoning_content"] = item.reasoning
    return JSONResponse(
        {
            **base,
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": usage,
            "atlas": {
                "steps": item.steps,
                "sources": visible_sources(item),
                "deadline_seconds": payload.timeout_seconds,
                "attachment_ceiling_eur": float(reserve),
                "hierarchical": any(
                    d.get("hierarchical_synthesis") for d in item.documents
                ),
                "context": item.context_samples[-1] if item.context_samples else None,
                "context_samples": item.context_samples,
                "context_manifest": item.context_manifest,
                "images": item.images,
                "files": item.files,
                "skills_loaded": item.skills_loaded,
                "terminal_commands": item.terminal_commands,
                "terminal_failed_commands": item.terminal_failed_commands,
                "uploaded_images": item.uploaded_images,
                **(
                    {"connector_approvals": payload._connector_approvals}
                    if payload._connector_approvals
                    else {}
                ),
                "reasoning_effort": payload.reasoning_effort,
                "max_output_tokens": payload.max_tokens,
                "cost_eur": item.cout_eur,
                "cost_kind": "reserved_upper_bound" if item.erreurs else "measured",
                "provider_model": item.provider_model,
                "reasoning_retried": item.reasoning_retried,
                "status": json.loads(Path("prompts/activity-labels.json").read_text())[
                    payload.status_language
                ]["reasoning_fallback"]
                if item.reasoning_retried
                else "",
                "reasoning": {
                    "collapsed": True,
                    "trace_tokens": item.trace_tokens,
                    "answer_tokens": item.answer_tokens,
                    "estimated": item.token_split_estimated,
                },
            },
        }
    )
