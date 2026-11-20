"""The Atlas assistant on the Responses and Messages formats (API parity).

The request is converted to the assistant's Chat Completions form, the assistant answers as a
stream, and that stream goes through the same assembly and wire as the direct models. The
assistant runs its own tools (web search, documents): client tools are refused by name rather
than ignored, and the hosted web search tool is accepted since the assistant searches anyway.

A failure frame of the assistant's stream (a named refusal such as the owned server's budget
cap in strict privacy, a budget or deadline stop) ends the answer as the format's error: an
error event in a stream, the named HTTP error otherwise. It was dropped, so the client read
an empty completed answer (staging 2026-11-20, strict request with the owned server refused).
"""

import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from packages.profiles import default_profile
from services.orchestrator.dev_chat import Output, RawChatInput

CLIENT_TOOLS_MESSAGE = (
    "The Atlas assistant runs its own tools (web search, documents). For your own tools, "
    "use atlas-dev or atlas-chat."
)


def assistant_body(raw: dict[str, Any], protocol: str) -> dict[str, Any]:
    """The assistant's Chat Completions request for a Responses or Messages body."""
    from packages.api_content import normalize_chat
    from services.orchestrator.dev_input import messages_body, responses_body

    converter = responses_body if protocol == "responses" else messages_body
    converted = converter(dict(raw, model="atlas-code"))
    hosted = converted.pop("_hosted", [])
    client_tools = [
        tool
        for tool in converted.get("tools", [])
        if not (hosted and tool["function"]["name"] == "web_search")
    ]
    if client_tools:
        raise PermissionError(CLIENT_TOOLS_MESSAGE)
    normalize_chat(converted)
    name = str(raw["model"])
    # No max_tokens: the assistant's internal calls (routing, tools, research) keep
    # their own output budget; the caller's limit caps the answer (serve).
    return {
        "model": default_profile() if name == "atlas" else name,
        "messages": converted["messages"],
        "stream": True,
        "atlas_ui": False,
    }


# Named refusals of a failure frame (with the owned server's answers) and their HTTP
# status when the answer is not streamed; any other failure is a 502, or 504 for the
# deadline.
_REFUSAL_STATUS = {"cost_budget": 429, "budget_limit": 429, "quota_exceeded": 429}
_FAILURE_STATUS = {"timeout": 504}


def failure_response(failure: dict[str, Any]) -> JSONResponse:
    """The named error of a failure frame, for an answer that is not streamed."""
    from packages.owned_server import STATUS as OWNED_STATUS
    from services.orchestrator.api_key_edge import error

    code = str(failure.get("type") or failure.get("code") or "provider_error")
    named = code in OWNED_STATUS or code in _REFUSAL_STATUS
    status = (
        OWNED_STATUS.get(code)
        or _REFUSAL_STATUS.get(code)
        or _FAILURE_STATUS.get(code, 502)
    )
    values = {k: v for k, v in failure.items() if k not in {"type", "code"}}
    # A named refusal keeps its sentence and measured values; any other failure stays
    # generic, as on the streamed wire (no diagnostic text for the logs).
    values = values if named else {}
    values["message"] = str(values.get("message") or code)
    reply = error(status, code, **values)
    retry = failure.get("retry_after")
    if isinstance(retry, int) and not isinstance(retry, bool) and retry > 0:
        reply.headers["Retry-After"] = str(retry)
    return reply


def output_limit(raw: dict[str, Any], protocol: str) -> int | None:
    """The caller's output limit (Messages max_tokens, Responses max_output_tokens)."""
    value = raw.get("max_output_tokens" if protocol == "responses" else "max_tokens")
    return value if type(value) is int and value > 0 else None


async def frames(response: Response) -> AsyncIterator[dict[str, Any]]:
    """Provider-style frames from the assistant's Chat Completions stream."""
    buffer = ""
    assert isinstance(response, StreamingResponse)
    async for piece in response.body_iterator:
        buffer += piece if isinstance(piece, str) else bytes(piece).decode()
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line.startswith("data: {"):
                continue
            chunk = json.loads(line[6:])
            if isinstance(chunk.get("error"), dict):
                yield {"choices": [], "error": chunk["error"]}
                return
            frame: dict[str, Any] = {"choices": []}
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                clean = {"content": delta["content"]} if delta.get("content") else {}
                if clean or choice.get("finish_reason"):
                    frame["choices"].append(
                        {
                            "index": 0,
                            "delta": clean,
                            "finish_reason": choice.get("finish_reason"),
                        }
                    )
            if isinstance(chunk.get("usage"), dict):
                frame["usage"] = {
                    "prompt_tokens": int(chunk["usage"].get("prompt_tokens", 0)),
                    "completion_tokens": int(
                        chunk["usage"].get("completion_tokens", 0)
                    ),
                }
            if frame["choices"] or "usage" in frame:
                yield frame


async def stop_at(
    source: AsyncIterator[dict[str, Any]], stops: list[str]
) -> AsyncIterator[dict[str, Any]]:
    """End the assistant's text at the first requested stop sequence, as a model would.

    The assistant is an agent, so the sequences apply to its final text. A tail that could
    start a sequence is held back; after a match the rest is read but not shown, so the
    usage frame still reports what the answer cost.
    """
    if not stops:
        async for frame in source:
            yield frame
        return
    keep = max(len(stop) for stop in stops) - 1
    pending, matched = "", None
    async for frame in source:
        if "error" in frame:
            yield frame
            continue
        choices = []
        for choice in frame["choices"]:
            if matched is not None:
                continue
            pending += choice["delta"].get("content", "")
            hits = [(pending.find(stop), stop) for stop in stops if stop in pending]
            if hits:
                at, matched = min(hits)
                text, pending = pending[:at], ""
            elif choice["finish_reason"]:
                text, pending = pending, ""
            else:
                text = pending[: len(pending) - keep] if keep else pending
                pending = pending[len(text) :]
            if text or choice["finish_reason"] or matched is not None:
                choices.append(
                    {
                        "index": 0,
                        "delta": {"content": text} if text else {},
                        "finish_reason": "stop"
                        if matched is not None
                        else choice["finish_reason"],
                        **({"stop_reason": matched} if matched is not None else {}),
                    }
                )
        if choices:
            yield {"choices": choices}
        if "usage" in frame:
            yield {"choices": [], "usage": frame["usage"]}


async def serve(request: Request, raw: dict[str, Any], protocol: str) -> Response:
    from services.orchestrator.api_key_edge import require
    from services.orchestrator.chat_api import chat as assistant_chat
    from services.orchestrator.dev_messages import Wire as MessagesWire
    from services.orchestrator.dev_responses import Wire as ResponsesWire

    missing = require(request, "assistant")
    if missing is not None:
        return missing
    body = assistant_body(raw, protocol)
    limit = output_limit(raw, protocol)
    request._body = json.dumps(body).encode()
    reply = await assistant_chat(request)
    if not isinstance(reply, StreamingResponse) or reply.status_code != 200:
        return reply
    # Responses has no stop parameter; Messages sequences were validated with the body.
    stops = list(raw.get("stop_sequences") or []) if protocol == "messages" else []
    shape = RawChatInput.model_validate(
        {
            "model": "raw/assistant",
            "messages": [{"role": "user", "content": "-"}],
            **({"stop": stops} if stops else {}),
        }
    )
    output = Output("assistant", shape, str(raw["model"]))
    wire = (
        ResponsesWire(output, raw)
        if protocol == "responses"
        else MessagesWire(output, raw)
    )

    failures: list[dict[str, Any]] = []

    async def events() -> AsyncGenerator[dict[str, Any], None]:
        finished = False
        from services.orchestrator.api_limits import OutputCap, cap_frames

        cap = OutputCap(limit) if limit is not None else None
        async for frame in cap_frames(stop_at(frames(reply), stops), cap):
            if "error" in frame:
                failures.append(frame["error"])
                failure = {
                    "type": str(frame["error"].get("type") or "provider_error"),
                    "message": str(frame["error"].get("message") or ""),
                }
                for event in wire.feed({"error": failure}):
                    yield event
                return
            if "usage" in frame and not finished:
                # The usage frame closes the answer: end the text before it.
                for event in wire.feed(
                    output.feed(
                        {
                            "choices": [
                                {"index": 0, "delta": {}, "finish_reason": "stop"}
                            ]
                        }
                    )
                ):
                    yield event
                finished = True
            if frame["choices"] and any(c["finish_reason"] for c in frame["choices"]):
                finished = True
            for event in wire.feed(output.feed(frame)):
                yield event

    if raw.get("stream"):

        async def stream() -> AsyncGenerator[str, None]:
            for event in wire.start():
                yield (
                    "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                )
            async for event in events():
                yield (
                    "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                )

        return StreamingResponse(stream(), media_type="text/event-stream")
    async for _ in events():
        pass
    if failures:
        return failure_response(failures[0])
    return JSONResponse(wire.result())
