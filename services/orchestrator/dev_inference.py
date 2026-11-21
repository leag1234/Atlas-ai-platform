"""Authenticated inference: reserve before I/O, settle usage or retain the bound."""

from packages.limits import LimitError
from packages.validation import describe_validation
from packages.privacy import PrivacyRefusal
from packages.owned_server import STARTING, OwnedServerUnavailable
from packages.user_errors import failure_sentence

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
import json
import logging
from time import monotonic
from typing import Any, cast

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
import api_routing
import dev_gateway as gateway
import owned_start
from services.orchestrator import owned_wait
from services.orchestrator.dev_auth import QuotaError, Store
from services.orchestrator.call_repair import repaired_frames
from services.orchestrator.dev_chat import ChatInput, Output, RawChatInput
from services.orchestrator.dev_input import (
    SemanticFieldError,
    StorageRefusal,
    messages,
    responses,
)
from services.orchestrator.dev_messages import Wire as MessagesWire
from services.orchestrator.dev_responses import Wire


def error(code: str, status: int, detail: str = "", **values: Any) -> JSONResponse:
    return JSONResponse(
        {"error": {"type": code, "code": code, "message": detail or code, **values}},
        status_code=status,
        headers=(
            {"Retry-After": str(values["retry_after"])}
            if values.get("retry_after") is not None
            else None
        ),
    )


def stream_refusal(
    protocol: str,
    parsed: Any,
    raw: Any,
    model: str,
    code: str,
    message: str,
    values: dict[str, Any],
) -> str:
    """The SSE frames ending a stream that was refused after it opened (a strict
    request that waited for the owned server), in the client's protocol."""
    if protocol == "chat":
        body = {"error": {"type": code, "code": code, "message": message, **values}}
        return "data: " + json.dumps(body) + "\n\n" + "data: [DONE]\n\n"
    output = Output(uuid.uuid4().hex, parsed, model)
    request = raw if isinstance(raw, dict) else {}
    wire = (
        Wire(output, request)
        if protocol == "responses"
        else MessagesWire(output, request)
    )
    events = [*wire.start(), *wire.feed({"error": {"type": code, "message": message}})]
    return "".join(
        "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
        for event in events
    )


def owned_wait_stream(
    first: OwnedServerUnavailable,
    accepted: int,
    resume: Callable[[], Awaitable[Response]],
    refusal: Callable[[str, str, dict[str, Any]], str],
) -> StreamingResponse:
    """A streamed strict request waiting for the owned server to start: SSE comments
    keep the stream alive, then the request is served as usual (``resume``), or ends
    with the starter's named answer (refused, failed, still starting after the wait)."""

    async def frames() -> AsyncGenerator[str | bytes, None]:
        try:
            async for _ in owned_wait.waiting(first, accepted):
                yield ": owned server starting\n\n"
        except OwnedServerUnavailable as exc:
            values = {k: v for k, v in exc.fields().items() if k != "code"}
            yield refusal(exc.code, exc.message, values)
            return
        try:
            response = await resume()
        except Exception as exc:  # noqa: BLE001 - the stream is already open
            from services.orchestrator.daily_budget import BudgetRefusal

            if isinstance(exc, BudgetRefusal):
                yield refusal(
                    "budget_limit",
                    "budget_limit",
                    {
                        "used": str(exc.used),
                        "limit": str(exc.limit),
                        "category": exc.category,
                    },
                )
                return
            yield refusal("unavailable", failure_sentence("en"), {})
            return
        if isinstance(response, StreamingResponse):
            # Closed with this stream: a client that leaves ends the served request.
            served = cast(AsyncGenerator[Any, None], response.body_iterator)
            async with aclosing(served) as pieces:
                async for piece in pieces:
                    yield piece if isinstance(piece, (str, bytes)) else bytes(piece)
            return
        try:
            failure = json.loads(bytes(response.body)).get("error") or {}
        except (ValueError, AttributeError):
            failure = {}
        if not isinstance(failure, dict):
            failure = {}
        code = str(failure.get("type") or failure.get("code") or "unavailable")
        extra = {
            k: v for k, v in failure.items() if k not in {"type", "code", "message"}
        }
        yield refusal(code, str(failure.get("message") or code), extra)

    return StreamingResponse(
        frames(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


def corrected_events(
    plan: gateway.Plan,
) -> Callable[[str], AsyncGenerator[dict[str, Any], None]]:
    """Opens the corrected attempt of ``plan`` (call_repair asks for it with a note)."""

    def start(note: str) -> AsyncGenerator[dict[str, Any], None]:
        return gateway.events(gateway.corrected(plan, note))

    return start


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
    meter = gateway.Meter()
    plan = gateway.metered(plan, meter)
    current, charged, incoming, outgoing, estimated = plan, 0, 0, 0, 0
    settled, known, failure = False, False, "closed"
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
                async with aclosing(
                    repaired_frames(
                        gateway.events(current),
                        request.tools,
                        corrected_events(current),
                        plan.reserved - charged,
                    )
                ) as frames:
                    async for frame in frames:
                        chunk = candidate.feed(frame)
                        if "usage" in frame:
                            charged += frame["cost_micro_eur"]
                            estimated += int(frame.get("estimated_micro_eur", 0))
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
                    output.choices = candidate.choices
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
                    store.finish(
                        request_id, charged, incoming, outgoing, estimated=estimated
                    )
                    settled = True
                    for chunk in pending:
                        yield chunk
                    return
                if attempt or emitted:
                    store.finish(
                        request_id, charged, incoming, outgoing, estimated=estimated
                    )
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
        failure = exc.code
        yield {"error": {"type": exc.code, "message": exc.detail}}
    except (
        ValueError,
        RuntimeError,
        TimeoutError,
        OSError,
        KeyError,
        TypeError,
    ) as exc:
        failure = type(exc).__name__
        yield provider_failure(exc)
    finally:
        if not settled:
            # attempts == 0: the deadline passed before the first provider call.
            settle_unmeasured(store, request_id, meter, plan, failure, attempts > 0)
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
                    # Who answered, against what the route planned first: a
                    # fallback (and its price) is visible per request.
                    "planned": request_target(plan),
                    "served_by": getattr(output, "served_by", None),
                }
            )
        )


def cached_count(usage: dict[str, Any] | None) -> int:
    """Prompt tokens the provider served from its cache (0 when it reports none)."""
    details = (usage or {}).get("prompt_tokens_details")
    value = details.get("cached_tokens") if isinstance(details, dict) else None
    return value if isinstance(value, int) and value > 0 else 0


def settle_unmeasured(
    store: Store,
    request_id: str,
    meter: gateway.Meter,
    plan: gateway.Plan,
    failure: str,
    called: bool,
) -> None:
    """Settle a call that ended without a usage frame, on what really happened.

    Attempts with a usage frame keep their measured cost; an attempt the provider
    refused, or that failed before one byte of answer, costs nothing; any other
    attempt (cut stream, client gone) is estimated from the request body sent and
    the text streamed back, never from the max_tokens reservation. The estimated
    part stays visible in GET /usage (D5). Only a call that went to the provider
    without any recorded attempt keeps its reservation: nothing is known of it.
    """
    total = meter.summary()
    if not called:
        store.finish(request_id, 0)
    elif not meter.attempts:
        store.finish(request_id, None)
        total["charged"] = total["estimated"] = plan.reserved
    else:
        store.finish(
            request_id,
            total["charged"],
            total["input_tokens"],
            total["output_tokens"],
            estimated=total["estimated"],
            measured=total["estimated"] == 0,
        )
    # A warning, so the operator log shows every call settled without usage: the
    # cause class only, never the request or the answer.
    logging.getLogger("atlas.api").warning(
        json.dumps(
            {
                "event": "devapi_settled_without_usage",
                "request": request_id,
                "reason": failure,
                "attempts": len(meter.attempts),
                "refused": sum(1 for a in meter.attempts if a.refused),
                "charged_micro_eur": total["charged"],
                "estimated_micro_eur": total["estimated"],
                "reserved_micro_eur": plan.reserved,
            }
        )
    )


def request_target(plan: gateway.Plan) -> str | None:
    return plan.provider + "/" + plan.model if plan.provider else None


def _provider_error_status(error: dict[str, Any]) -> int:
    """The provider's own failure status reaches the client, never another model."""
    kind = error.get("type")
    if kind == "provider_quota":
        return 402
    if kind == "rate_limit":
        return 429
    if kind == "provider_timeout":
        return 504
    if kind == "provider_unavailable":
        return 503
    return 502


def provider_failure(exc: BaseException) -> dict[str, dict[str, Any]]:
    """D5: a provider refusal is reported with its cause, never as another model."""
    if str(exc) == "invalid_tool_arguments":
        return {
            "error": {
                "type": "invalid_tool_arguments",
                "message": "The model returned tool-call arguments that are not valid "
                "JSON, also after one corrected attempt. Send the request again.",
            }
        }
    status = getattr(exc, "status", None)
    retry_after = getattr(exc, "retry_after", None)
    if str(exc) == "provider_quota" or status == 402:
        return {
            "error": {
                "type": "provider_quota",
                "message": "The model provider refused the request: account credit exhausted.",
                # The status the provider really sent: a credit refusal may
                # arrive on another status than 402.
                "provider_status": status if isinstance(status, int) else 402,
            }
        }
    if str(exc) == "provider_timeout" or status == 504:
        return {
            "error": {
                "type": "provider_timeout",
                "message": "The model provider did not answer in time.",
                "provider_status": 504,
            }
        }
    if status == 429:
        error = {
            "type": "rate_limit",
            "message": "The model provider rate-limited the request.",
            "provider_status": 429,
        }
        if retry_after is not None:
            error["retry_after"] = retry_after
        return {"error": error}
    if status is not None and status >= 500:
        return {
            "error": {
                "type": "provider_unavailable",
                "message": "The model provider is unavailable.",
                "provider_status": status,
            }
        }
    return {
        "error": {
            "type": "provider_error",
            # A sentence for people; the code is the type (no bare code as text).
            "message": failure_sentence("en", "provider_error"),
            **({"provider_status": status} if status is not None else {}),
        }
    }


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
    called, failure = False, "closed"
    meter = gateway.Meter()
    plan = gateway.metered(plan, meter)
    try:
        if deadline <= monotonic():
            raise TimeoutError("timeout")
        called = True
        async with (
            asyncio.timeout(max(0, deadline - monotonic())),
            aclosing(
                repaired_frames(
                    gateway.events(plan),
                    output.request.tools,
                    # One corrected attempt when a tool call stays unusable.
                    corrected_events(plan),
                    plan.reserved,
                )
            ) as frames,
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
                        estimated=int(frame.get("estimated_micro_eur", 0)),
                    )
                    settled = True
                    if charged > plan.reserved:
                        raise LimitError(
                            "budget_exceeded", charged, plan.reserved, "microEUR"
                        )
                yield chunk
    except LimitError as exc:
        failure = exc.code
        yield {"error": {"type": exc.code, "message": exc.detail}}
    except (
        ValueError,
        RuntimeError,
        TimeoutError,
        OSError,
        KeyError,
        TypeError,
    ) as exc:
        failure = type(exc).__name__
        yield provider_failure(exc)
    finally:
        if not settled:
            # Nothing was sent when the deadline passed before the provider call.
            settle_unmeasured(store, request_id, meter, plan, failure, called)
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
                    # Who answered, against what the route planned first: a
                    # fallback (and its price) is visible per request.
                    "planned": request_target(plan),
                    "served_by": getattr(output, "served_by", None),
                }
            )
        )


async def hosted_drive(
    store: Store,
    developer: str,
    request_id: str,
    plan: gateway.Plan,
    output: Output,
    deadline: float,
    request: RawChatInput,
    public_model: str,
    key_id: str,
    strict: bool,
    fetch_options: dict[str, Any] | None = None,
    code_options: dict[str, Any] | None = None,
    no_fallback: bool = False,
) -> AsyncGenerator[dict[str, Any], None]:
    """Model turns with hosted searches in between, until the model answers or calls a client tool.

    Each turn is its own provider call with its own reservation. Searches stream to the client
    as hosted items when they run; the final turn's chunks follow with the summed usage.
    """
    from services.orchestrator.hosted_tools import (
        HOSTED_NAMES,
        MAX_ROUNDS,
        code_execution,
        execution_code,
        fetch_refusal,
        fetch_url,
        search_query,
        web_fetch,
        web_search,
    )

    options = fetch_options or {}
    code_options = code_options or {}
    fetched = 0
    executions = 0
    execution_limit = 4
    if isinstance(code_options.get("max_uses"), int):
        execution_limit = min(execution_limit, code_options["max_uses"])
    # Addresses the model may read: the conversation as sent, then search results.
    seen = json.dumps(
        request.model_dump(by_alias=True, exclude_none=True)["messages"],
        ensure_ascii=False,
    )
    original_request_id = request_id
    prompt_tokens = completion_tokens = cached_tokens = 0
    for turn in range(MAX_ROUNDS):
        candidate = Output(request_id, request, str(output.base["model"]))
        kept: list[dict[str, Any]] = []
        async with aclosing(
            drive(store, developer, request_id, plan, candidate, deadline)
        ) as frames:
            async for chunk in frames:
                if "error" in chunk:
                    yield chunk
                    return
                kept.append(chunk)
        if candidate.usage:
            prompt_tokens += int(candidate.usage.get("prompt_tokens", 0))
            completion_tokens += int(candidate.usage.get("completion_tokens", 0))
            cached_tokens += cached_count(candidate.usage)
        calls = list(candidate.calls.values())
        hosted_calls = [c for c in calls if c["function"]["name"] in HOSTED_NAMES]
        if (
            not hosted_calls
            or len(hosted_calls) != len(calls)
            or turn == MAX_ROUNDS - 1
        ):
            output.choices = candidate.choices
            output.served_by = candidate.served_by
            output.privacy = candidate.privacy
            output.usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                # Cached prompt tokens of every turn, as the provider counted them.
                "prompt_tokens_details": {"cached_tokens": cached_tokens},
            }
            for chunk in kept:
                if "usage" in chunk:
                    chunk = dict(chunk, usage=output.usage)
                yield chunk
            return
        results = []
        for call in hosted_calls:
            if call["function"]["name"] == "web_fetch":
                url = fetch_url(call["function"]["arguments"])
                refusal = fetch_refusal(url, seen, options, fetched)
                fetched += 1
                page = (
                    {"url": url, "error": refusal}
                    if refusal
                    else await web_fetch(
                        url,
                        max(1.0, min(30.0, deadline - monotonic())),
                        key_id,
                        options.get("max_content_tokens"),
                    )
                )
                yield {"hosted": {"type": "web_fetch_call", "id": call["id"], **page}}
                results.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(page, ensure_ascii=False),
                    }
                )
                continue
            if call["function"]["name"] == "code_execution":
                code = execution_code(call["function"]["arguments"])
                error = None
                if executions >= execution_limit:
                    error = "too_many_requests"
                else:
                    # Only API key stores count executions; anything else cannot run code.
                    count = getattr(store, "count_code_execution", None)
                    try:
                        if count is None:
                            raise ValueError("code_execution_not_counted")
                        count(
                            original_request_id,
                            int(
                                os.environ.get(
                                    "ATLAS_API_CODE_EXECUTION_DAILY_LIMIT", "100"
                                )
                            ),
                        )
                    except LimitError:
                        error = "code_execution_daily_limit"
                    except (ValueError, OSError):
                        error = "unavailable"
                if error is None:
                    executions += 1
                    result = await code_execution(
                        code, max(1.0, min(60.0, deadline - monotonic())), key_id
                    )
                    if "error" in result:
                        error = str(result["error"])
                else:
                    result = {"error": error}
                yield {
                    "hosted": {
                        "type": "code_interpreter_call",
                        "id": call["id"],
                        "code": code,
                        **result,
                    }
                }
                results.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
                continue
            query = search_query(call["function"]["arguments"])
            found = await web_search(
                query, max(1.0, min(20.0, deadline - monotonic())), key_id
            )
            output.citations.extend(found["results"])
            seen += " " + " ".join(row["url"] for row in found["results"])
            yield {
                "hosted": {
                    "type": "web_search_call",
                    "id": call["id"],
                    "query": query,
                    "results": found["results"],
                }
            }
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(found, ensure_ascii=False),
                }
            )
        assistant = {
            "role": "assistant",
            "content": candidate.text or None,
            "tool_calls": calls,
        }
        request = RawChatInput.model_validate(
            request.model_dump(by_alias=True, exclude_none=True)
            | {
                "messages": [
                    *request.model_dump(by_alias=True, exclude_none=True)["messages"],
                    assistant,
                    *results,
                ]
            }
        )
        try:
            plan = gateway.prepare_api(
                request,
                public_model,
                key_id,
                strict=strict,
                pinned=api_routing.pinned_target(public_model or "") is not None,
                fallback=not no_fallback,
            )
            request_id = store.reserve(
                developer,
                plan.reserved,
                monetary=plan.reserved > 0 and not plan.unpriced,
            )
        except (LimitError, QuotaError, ValueError) as exc:
            yield {
                "error": {
                    "type": "budget_limit"
                    if isinstance(exc, QuotaError)
                    else "invalid_request",
                    "message": str(exc)[:200],
                }
            }
            return


async def chat(
    request: Request,
    protocol: str = "chat",
    *,
    raw_model: str | None = None,
    public_model: str | None = None,
    owned_waited: bool = False,
) -> Response:
    # nginx allows 300 s on the API; long streamed agent turns need more than 120 s.
    started = monotonic()
    deadline = started + 300
    try:
        body = bytearray()
        async with asyncio.timeout(120):
            async for piece in request.stream():
                body.extend(piece)
                if len(body) > 8388608:
                    raise LimitError("body_limit", len(body), 8388608, "bytes")
        raw = json.loads(body)
        if isinstance(raw, dict):
            # "fallback": false pins an alias request to its first target.
            request.state.no_fallback = raw.pop("fallback", None) is False
        if raw_model is not None and protocol == "chat" and isinstance(raw, dict):
            from packages.api_content import (
                FileContentError,
                drop_client_extras,
                inline_files,
                normalize_chat,
                unsupported_option,
            )

            drop_client_extras(raw)
            if (option := unsupported_option(raw)) is not None:
                return error("unsupported_parameter: " + option, 400)
            try:
                normalize_chat(raw)
                inline_files(raw.get("messages") or [])
            except FileContentError as exc:
                return error(str(exc), 400)
        parsed: ChatInput | RawChatInput
        hosted: list[str] = []
        fetch_options: dict[str, Any] | None = None
        code_options: dict[str, Any] | None = None
        if (
            raw_model is not None
            and protocol == "chat"
            and isinstance(raw, dict)
            and "web_search_options" in raw
        ):
            # Chat Completions asks for web search with web_search_options: the same
            # hosted search as the Responses web_search tool.
            from services.orchestrator.hosted_tools import SEARCH_FUNCTION

            raw.pop("web_search_options")
            raw["tools"] = [*(raw.get("tools") or []), SEARCH_FUNCTION]
            hosted = ["web_search"]
        try:
            if protocol in ("messages", "responses") and raw_model is not None:
                from packages.api_content import inline_files, normalize_chat
                from services.orchestrator.dev_input import (
                    messages_body,
                    responses_body,
                    text_only,
                )

                converter = responses_body if protocol == "responses" else messages_body
                converted = converter(dict(raw, model="atlas-code"))
                # Same checks as the text-only schema, then images and files kept.
                ChatInput.model_validate(text_only(converted))
                normalize_chat(converted)
                inline_files(converted["messages"])
                hosted = converted.pop("_hosted", [])
                fetch_options = converted.pop("_fetch", None)
                code_options = converted.pop("_code", None)
                parsed = RawChatInput.model_validate(dict(converted, model=raw_model))
            else:
                parsed = (
                    responses(raw)
                    if protocol == "responses"
                    else messages(raw)
                    if protocol == "messages"
                    else RawChatInput.model_validate(raw)
                    if raw_model is not None
                    else ChatInput.model_validate(raw)
                )
        except KeyError:
            # Missing client fields are not missing provider configuration.
            return error("invalid_request", 400)
        if public_model is not None and getattr(request.state, "context_trim", False):
            from services.orchestrator.context_trim import trim_messages

            parsed = parsed.model_copy(
                update={"messages": trim_messages(list(parsed.messages))}
            )
        if public_model is not None:
            from services.orchestrator.loop_nudge import LOOP_STOP, nudge_messages

            nudged, repeats = nudge_messages(list(parsed.messages))
            if repeats:
                logging.getLogger("atlas.api").warning(
                    json.dumps({"event": "devapi_loop_nudge", "repeats": repeats})
                )
                if repeats >= LOOP_STOP:
                    # Measured 2026-10-31: agents kept repeating after the note;
                    # stop the session instead of spending the client's budget.
                    return JSONResponse(
                        {
                            "error": {
                                "type": "tool_loop_detected",
                                "message": (
                                    f"The same tool call returned the same result "
                                    f"{repeats} times in a row; Atlas stopped this "
                                    "session to protect your budget. Change approach "
                                    "or start a new session."
                                ),
                            }
                        },
                        status_code=409,
                    )
                parsed = parsed.model_copy(update={"messages": nudged})
        level = getattr(request.state, "privacy", "standard")
        strict = level == "strict"
        if hosted and level == "enhanced":
            # Enhanced privacy: a search query or fetched URL leaves Atlas in clear
            # until the local detector exists; code execution stays on Atlas machines.
            for feature in ("web_search", "web_fetch"):
                if feature in hosted:
                    refused = PrivacyRefusal(feature, "enhanced")
                    return JSONResponse(
                        {"error": refused.body()}, status_code=refused.status
                    )
        if hosted and strict:
            # Strict privacy: no request leaves the owned infrastructure. Code
            # execution stays on Atlas machines, but web tools leave it.
            if "web_search" in hosted:
                return error(
                    "web_search_unavailable_in_strict_privacy",
                    400,
                    "Web search is not available in strict privacy mode.",
                )
            if "web_fetch" in hosted:
                return error(
                    "web_fetch_unavailable_in_strict_privacy",
                    400,
                    "Web fetch is not available in strict privacy mode.",
                )
        pinned = (
            public_model is not None
            and api_routing.pinned_target(public_model) is not None
        )
        no_fallback = bool(getattr(request.state, "no_fallback", False))
        if (
            public_model is not None
            and parsed.response_format.type == "json_schema"
            and parsed.response_format.json_schema is not None
            and parsed.response_format.json_schema.strict
            and not api_routing.supports_structured_output(
                public_model, parsed.reasoning_effort
            )
        ):
            # Never free text when strict json_schema was asked: refuse before
            # any provider call, naming the public model.
            return JSONResponse(
                {
                    "error": {
                        "type": "unsupported_parameter",
                        "param": "response_format",
                        "message": "json_schema strict is not supported by "
                        + public_model,
                    }
                },
                status_code=400,
            )
        plan = (
            gateway.prepare_api(
                parsed,
                public_model,
                request.state.api_key.id,
                strict=strict,
                pinned=pinned,
                fallback=not no_fallback,
            )
            if public_model is not None
            else gateway.prepare_raw(parsed)
            if raw_model is not None
            else gateway.prepare(cast(ChatInput, parsed))
        )
        if strict and plan.privacy == "owned":
            # The owned server may be powered off between uses: when it does not
            # answer, its starter is asked to power it on, before anything is
            # reserved or streamed. A start under way is waited for (owned_wait.py):
            # a streamed request keeps its stream alive meanwhile and is then
            # served as usual; a client that accepts no wait (X-Atlas-Owned-Wait:
            # 0) gets 503 with Retry-After. A refusal of the starter is named.
            try:
                await owned_start.ensure()
            except OwnedServerUnavailable as exc:
                accepted = 0 if owned_waited else owned_wait.request_wait(request)
                if exc.code != STARTING or accepted <= 0:
                    raise
                if parsed.stream:
                    request._body = bytes(body)

                    async def resume() -> Response:
                        return await chat(
                            request,
                            protocol,
                            raw_model=raw_model,
                            public_model=public_model,
                            owned_waited=True,
                        )

                    return owned_wait_stream(
                        exc,
                        accepted,
                        resume,
                        lambda code, message, values: stream_refusal(
                            protocol,
                            parsed,
                            raw,
                            public_model or raw_model or "atlas-code",
                            code,
                            message,
                            values,
                        ),
                    )
                # Not streamed: a bounded silent wait inside the API deadline.
                async for _ in owned_wait.waiting(exc, accepted):
                    pass
        store, developer = request.state.store, request.state.developer
        request_id = store.reserve(
            developer, plan.reserved, monetary=plan.reserved > 0 and not plan.unpriced
        )
    except OwnedServerUnavailable as exc:
        values = {k: v for k, v in exc.fields().items() if k != "code"}
        return error(exc.code, exc.status, exc.message, **values)
    except LimitError as exc:
        return JSONResponse(
            {
                "error": {
                    "type": exc.code,
                    "message": exc.detail,
                    "measured": exc.measured,
                    "limit": exc.limit,
                    "unit": exc.unit,
                }
            },
            status_code=400,
        )
    except StorageRefusal as exc:
        return error("storage_not_supported", 400, str(exc))
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
    except PrivacyRefusal as exc:
        # A permanent policy answer (403), never an outage: clients do not retry it.
        return JSONResponse({"error": exc.body()}, status_code=exc.status)
    except ValueError as exc:
        code = (
            str(exc)
            if str(exc) in {"cost_budget", "context_exceeded", "invalid_reservation"}
            else "invalid_request"
        )
        return error(code, 400)
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

    no_fallback = bool(getattr(request.state, "no_fallback", False))

    def run() -> AsyncGenerator[dict[str, Any], None]:
        if not hosted or public_model is None:
            return drive(store, developer, request_id, plan, output, deadline)
        return hosted_drive(
            store,
            developer,
            request_id,
            plan,
            output,
            deadline,
            cast(RawChatInput, parsed),
            public_model,
            request.state.api_key.id,
            strict,
            fetch_options,
            code_options,
            no_fallback,
        )

    async def chunks() -> AsyncGenerator[str, None]:
        # Chat streams hold one chunk back so the last JSON chunk before [DONE]
        # can name the model that served the answer (served_by).
        last: dict[str, Any] | None = None
        outcome = "complete"
        error_type: str | None = None
        frames_yielded = 0
        bytes_yielded = 0
        max_gap_s = 0.0
        previous_yield = monotonic()

        def record_yield(frame: str) -> str:
            nonlocal frames_yielded, bytes_yielded, max_gap_s, previous_yield
            now = monotonic()
            max_gap_s = max(max_gap_s, now - previous_yield)
            previous_yield = now
            frames_yielded += 1
            bytes_yielded += len(frame)
            return frame

        try:
            if wire:
                for event in wire.start():
                    yield record_yield(
                        "event: "
                        + event["type"]
                        + "\ndata: "
                        + json.dumps(event)
                        + "\n\n"
                    )
            async with aclosing(run()) as frames:
                async for chunk in frames:
                    if wire:
                        try:
                            events = wire.feed(chunk)
                        except (ValueError, TypeError, KeyError):
                            events = wire.feed({"error": {"message": "provider_error"}})
                        if any("error" in event for event in events):
                            outcome = "error_frame"
                            error_type = str(
                                next(
                                    event["error"].get("type", "provider_error")
                                    for event in events
                                    if "error" in event
                                )
                            )
                        for event in events:
                            yield record_yield(
                                "event: "
                                + event["type"]
                                + "\ndata: "
                                + json.dumps(event)
                                + "\n\n"
                            )
                    elif "hosted" in chunk:
                        # Searches run between turns; Chat Completions shows only the answer.
                        continue
                    elif (
                        chunk.get("choices") != []
                        or parsed.stream_options.include_usage
                    ):
                        if "error" in chunk:
                            outcome = "error_frame"
                            error_type = str(
                                chunk["error"].get("type", "provider_error")
                            )
                            chunk["error"].get("retry_after")
                        if parsed.stream_options.include_usage and "error" not in chunk:
                            chunk.setdefault("usage", None)
                        if output.citations:
                            # The final delta of a searched answer carries its citations.
                            for choice in chunk.get("choices") or []:
                                if choice.get("finish_reason") is not None:
                                    from services.orchestrator.dev_chat import (
                                        url_citations,
                                    )

                                    found = url_citations(output.text, output.citations)
                                    if found:
                                        choice["delta"] = dict(
                                            choice.get("delta") or {}, annotations=found
                                        )
                        if last is not None:
                            yield record_yield("data: " + json.dumps(last) + "\n\n")
                        last = chunk
            if wire is not None and not getattr(
                getattr(wire, "core", wire), "terminal", True
            ):
                # The provider stream ended without a final frame: end with a failure
                # event instead of closing silently.
                outcome = "unterminated"
                logging.getLogger("atlas.api").warning(
                    json.dumps({"event": "devapi_stream_unterminated"})
                )
                for event in wire.feed(
                    {
                        "error": {
                            "type": "server_error",
                            "message": "provider_stream_ended",
                        }
                    }
                ):
                    yield record_yield(
                        "event: "
                        + event["type"]
                        + "\ndata: "
                        + json.dumps(event)
                        + "\n\n"
                    )
            if wire is None:
                if last is not None:
                    if output.served_by and "error" not in last:
                        last["served_by"] = output.served_by
                    yield record_yield("data: " + json.dumps(last) + "\n\n")
                yield record_yield("data: [DONE]\n\n")
        except (GeneratorExit, asyncio.CancelledError):
            outcome = "client_disconnect"
            raise
        except BaseException as exc:
            outcome = "exception"
            error_type = type(exc).__name__
            raise
        finally:
            if outcome == "error_frame":
                error_type = error_type or "provider_error"
            elif outcome == "unterminated":
                error_type = "provider_stream_ended"
            logging.getLogger("atlas.api").warning(
                json.dumps(
                    {
                        "event": "devapi_stream_end",
                        "outcome": outcome,
                        **({"error_type": error_type} if error_type else {}),
                        "protocol": protocol,
                        "model": public_model or raw_model or "atlas-code",
                        "elapsed_s": round(monotonic() - started, 1),
                        "frames": frames_yielded,
                        "bytes": bytes_yielded,
                        "max_gap_s": round(max_gap_s, 1),
                    }
                )
            )

    if parsed.stream:
        headers = {"X-Accel-Buffering": "no"}
        if plan.provider:
            headers["X-Atlas-Served-By"] = plan.provider + "/" + plan.model
        return StreamingResponse(
            chunks(),
            media_type="text/event-stream",
            headers=headers,
        )
    async with aclosing(run()) as frames:
        async for frame in frames:
            if "error" in frame:
                status = _provider_error_status(frame["error"])
                return error(
                    frame["error"]["type"],
                    status,
                    frame["error"]["message"],
                    **(
                        {"provider_status": frame["error"]["provider_status"]}
                        if frame["error"].get("provider_status") is not None
                        else {}
                    ),
                    **(
                        {"retry_after": frame["error"]["retry_after"]}
                        if frame["error"].get("retry_after") is not None
                        else {}
                    ),
                )
            if wire:
                try:
                    wire.feed(frame)
                except (ValueError, TypeError, KeyError):
                    return error("provider_error", 502)
    result = wire.result() if wire else output.result()
    headers = {"X-Atlas-Served-By": output.served_by} if output.served_by else {}
    return JSONResponse(result, headers=headers)
