"""Authenticated inference: reserve before I/O, settle usage or retain the bound."""

from packages.limits import LimitError
from packages.validation import describe_validation

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
import json
import logging
from time import monotonic
from typing import Any, cast

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
import dev_gateway as gateway
from services.orchestrator.dev_auth import QuotaError, Store
from services.orchestrator.dev_chat import ChatInput, Output, RawChatInput
from services.orchestrator.dev_input import SemanticFieldError, responses, messages
from services.orchestrator.dev_messages import Wire as MessagesWire
from services.orchestrator.dev_responses import Wire


def error(code: str, status: int, detail: str = "") -> JSONResponse:
    return JSONResponse(
        {"error": {"type": code, "message": detail or code}}, status_code=status
    )


async def validated_drive(
    store: Store,
    developer: str,
    request_id: str,
    plan: gateway.Plan,
    output: Output,
    deadline: float,
) -> AsyncGenerator[dict[str, Any], None]:
    request = output.request
    streaming = isinstance(request, ChatInput) and request.stream
    current, charged, incoming, outgoing = plan, 0, 0, 0
    settled, known = False, False
    attempts, started = 0, monotonic()
    try:
        async with asyncio.timeout(max(0, deadline - monotonic())):
            for attempt in range(2):
                if deadline <= monotonic():
                    raise TimeoutError("timeout")
                attempts += 1
                known = False
                pending: list[dict[str, Any]] = []
                candidate = Output(request_id, request)
                emitted = False
                async with aclosing(gateway.events(current)) as frames:
                    async for frame in frames:
                        chunk = candidate.feed(frame)
                        if "usage" in frame:
                            charged += frame["cost_micro_eur"]
                            incoming += frame["usage"]["prompt_tokens"]
                            outgoing += frame["usage"]["completion_tokens"]
                            known = True
                            if charged > plan.reserved:
                                raise LimitError(
                                    "budget_exceeded",
                                    charged,
                                    plan.reserved,
                                    "microEUR",
                                )
                        if streaming and chunk.get("choices"):
                            # Terminal success must wait for independent validation.
                            choice = chunk["choices"][0]
                            reason = choice["finish_reason"]
                            if reason is not None:
                                pending.append(
                                    dict(chunk, choices=[dict(choice, delta={})])
                                )
                                choice["finish_reason"] = None
                            if choice["delta"]:
                                output.text, output.calls = (
                                    candidate.text,
                                    candidate.calls,
                                )
                                emitted |= bool(
                                    choice["delta"].get("content")
                                    or choice["delta"].get("tool_calls")
                                )
                                yield chunk
                        else:
                            pending.append(chunk)
                problem = candidate.validation_error()
                if problem is None:
                    output.text, output.calls = candidate.text, candidate.calls
                    output.reason = candidate.reason
                    output.usage = {
                        "prompt_tokens": incoming,
                        "completion_tokens": outgoing,
                        "total_tokens": incoming + outgoing,
                    }
                    if candidate.usage is not None:
                        details = candidate.usage.get("prompt_tokens_details")
                        if details is not None:
                            output.usage["prompt_tokens_details"] = details
                    output.served_by = candidate.served_by
                    for chunk in pending:
                        if "usage" in chunk:
                            chunk["usage"] = output.usage
                    store.finish(request_id, charged, incoming, outgoing)
                    settled = True
                    for chunk in pending:
                        yield chunk
                    return
                if attempt or emitted:
                    store.finish(request_id, charged, incoming, outgoing)
                    settled = True
                    yield {
                        "error": {
                            "type": "invalid_response_format",
                            "message": "Output validation failed: " + problem,
                        }
                    }
                    return
                current = gateway.retry(plan, problem, plan.reserved - charged)
    except LimitError as exc:
        yield {"error": {"type": exc.code, "message": exc.detail}}
    except (ValueError, RuntimeError, TimeoutError, OSError, KeyError, TypeError):
        yield {"error": {"type": "provider_error", "message": "provider_error"}}
    finally:
        if not settled:
            store.finish(request_id, charged if known else None, incoming, outgoing)
        logging.getLogger("uvicorn.error").info(
            json.dumps(
                {
                    "event": "devapi_request",
                    "unpriced": plan.unpriced,
                    "request": request_id,
                    "developer": developer,
                    "duration_ms": round((monotonic() - started) * 1000),
                    "attempts": attempts,
                    "charged_micro_eur": charged if known else None,
                    "reserved_micro_eur": plan.reserved,
                    "usage": output.usage,
                }
            )
        )


async def drive(
    store: Store,
    developer: str,
    request_id: str,
    plan: gateway.Plan,
    output: Output,
    deadline: float,
) -> AsyncGenerator[dict[str, Any], None]:
    if output.request.validate_output:
        async with aclosing(
            validated_drive(store, developer, request_id, plan, output, deadline)
        ) as frames:
            async for chunk in frames:
                yield chunk
        return
    settled, charged, started = False, None, monotonic()
    try:
        if deadline <= monotonic():
            raise TimeoutError("timeout")
        async with (
            asyncio.timeout(max(0, deadline - monotonic())),
            aclosing(gateway.events(plan)) as frames,
        ):
            async for frame in frames:
                chunk = output.feed(frame)
                if "usage" in frame:
                    charged = frame["cost_micro_eur"]
                    store.finish(
                        request_id,
                        charged,
                        frame["usage"]["prompt_tokens"],
                        frame["usage"]["completion_tokens"],
                    )
                    settled = True
                    if charged > plan.reserved:
                        raise LimitError(
                            "budget_exceeded", charged, plan.reserved, "microEUR"
                        )
                yield chunk
    except LimitError as exc:
        yield {"error": {"type": exc.code, "message": exc.detail}}
    except (ValueError, RuntimeError, TimeoutError, OSError, KeyError, TypeError):
        yield {"error": {"type": "provider_error", "message": "provider_error"}}
    finally:
        if not settled:
            store.finish(request_id, None)
        logging.getLogger("uvicorn.error").info(
            json.dumps(
                {
                    "event": "devapi_request",
                    "unpriced": plan.unpriced,
                    "request": request_id,
                    "developer": developer,
                    "duration_ms": round((monotonic() - started) * 1000),
                    "charged_micro_eur": charged,
                    "reserved_micro_eur": plan.reserved,
                    "usage": output.usage,
                }
            )
        )


async def chat(
    request: Request,
    protocol: str = "chat",
    *,
    raw_model: str | None = None,
    public_model: str | None = None,
) -> Response:
    deadline = monotonic() + 120
    try:
        body = bytearray()
        async with asyncio.timeout(120):
            async for piece in request.stream():
                body.extend(piece)
                if len(body) > 1048576:
                    raise LimitError("body_limit", len(body), 1048576, "bytes")
        raw = json.loads(body)
        try:
            parsed = (
                responses(
                    dict(raw, model="atlas-code") if raw_model is not None else raw
                )
                if protocol == "responses"
                else messages(dict(raw, model="atlas-code") if raw_model else raw)
                if protocol == "messages"
                else RawChatInput.model_validate(raw)
                if raw_model is not None
                else ChatInput.model_validate(raw)
            )
        except KeyError:
            # Missing client fields are not missing provider configuration.
            return error("invalid_request", 400)
        if protocol in ("messages", "responses") and raw_model is not None:
            parsed = RawChatInput.model_validate(
                parsed.model_dump(by_alias=True, exclude_none=True)
                | {"model": raw_model}
            )
        plan = (
            gateway.prepare_api(parsed, public_model, request.state.api_key.id)
            if public_model is not None
            else gateway.prepare_raw(parsed)
            if raw_model is not None
            else gateway.prepare(cast(ChatInput, parsed))
        )
        store, developer = request.state.store, request.state.developer
        request_id = store.reserve(
            developer, plan.reserved, monetary=plan.reserved > 0 and not plan.unpriced
        )
    except LimitError as exc:
        return error(exc.code, 400, exc.detail)
    except SemanticFieldError as exc:
        return error("invalid_request", 400, str(exc))
    except ValidationError as exc:
        return error("invalid_request", 400, describe_validation(exc))
    except QuotaError as exc:
        from services.orchestrator.api_keys import KeyQuotaError
        from services.orchestrator.api_key_edge import error as key_error

        if isinstance(exc, KeyQuotaError):
            return key_error(429, "quota_exceeded", **exc.values)
        return error("quota_exceeded", 429)
    except PermissionError:
        return error("unauthorized", 401)
    except (
        ValueError,
        TypeError,
        AttributeError,
        RecursionError,
        ValidationError,
    ) as exc:
        code = (
            str(exc)
            if str(exc) in {"cost_budget", "context_exceeded", "invalid_reservation"}
            else "invalid_request"
        )
        return error(code, 400)
    except (TimeoutError, KeyError):
        return error("unavailable", 503)
    output = Output(request_id, parsed, public_model or raw_model or "atlas-code")
    wire = (
        Wire(output, raw)
        if protocol == "responses"
        else MessagesWire(output, raw)
        if protocol == "messages"
        else None
    )

    async def chunks() -> AsyncGenerator[str, None]:
        if wire:
            for event in wire.start():
                yield (
                    "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                )
        async with aclosing(
            drive(store, developer, request_id, plan, output, deadline)
        ) as frames:
            async for chunk in frames:
                if wire:
                    try:
                        events = wire.feed(chunk)
                    except (ValueError, TypeError, KeyError):
                        events = wire.feed({"error": {"message": "provider_error"}})
                    for event in events:
                        yield (
                            "event: "
                            + event["type"]
                            + "\ndata: "
                            + json.dumps(event)
                            + "\n\n"
                        )
                elif chunk.get("choices") != [] or parsed.stream_options.include_usage:
                    if parsed.stream_options.include_usage and "error" not in chunk:
                        chunk.setdefault("usage", None)
                    yield "data: " + json.dumps(chunk) + "\n\n"
        if wire is None:
            yield "data: [DONE]\n\n"

    if parsed.stream:
        headers = {"X-Accel-Buffering": "no"}
        if plan.provider:
            headers["X-Atlas-Served-By"] = plan.provider + "/" + plan.model
        return StreamingResponse(
            chunks(),
            media_type="text/event-stream",
            headers=headers,
        )
    async with aclosing(
        drive(store, developer, request_id, plan, output, deadline)
    ) as frames:
        async for frame in frames:
            if "error" in frame:
                return error(frame["error"]["type"], 502, frame["error"]["message"])
            if wire:
                try:
                    wire.feed(frame)
                except (ValueError, TypeError, KeyError):
                    return error("provider_error", 502)
    result = wire.result() if wire else output.result()
    headers = {"X-Atlas-Served-By": output.served_by} if output.served_by else {}
    return JSONResponse(result, headers=headers)
