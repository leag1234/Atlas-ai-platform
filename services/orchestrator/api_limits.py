"""The caller's limits on the API chat routes: hard content limits and the output limit.

Hard content limits (packages.content_policy.text_limit, the rules shared with images)
read the caller's own words in the last user message, as the chat interface reads the
person's, and answer a short refusal before any model call, on every API chat route
(Chat Completions, Responses, Messages) and for every model name.

The assistant (``atlas`` and its profiles) runs several internal calls (routing, tools,
research) with its own output budget; the caller's max_tokens caps its final answer,
which then ends with the length outcome of the direct models.
"""

import codecs
from collections.abc import AsyncIterable, AsyncIterator
import json
from typing import Any
import uuid

from fastapi.responses import JSONResponse, Response, StreamingResponse

# The repository's token estimate (packages.api_content.estimated_tokens): UTF-8 bytes / 4.
TOKEN_BYTES = 4
TEXT_PARTS = ("text", "input_text")


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") in TEXT_PARTS
            and isinstance(part.get("text"), str)
        )
    return ""


def caller_text(raw: dict[str, Any], protocol: str) -> str:
    """The text of the caller's last user message.

    Never a system prompt, an earlier turn or a tool result: a turn that only returns
    tool results carries no new words of the caller.
    """
    source = raw.get("input") if protocol == "responses" else raw.get("messages")
    if protocol == "responses" and isinstance(source, str):
        return source
    if not isinstance(source, list):
        return ""
    for message in reversed(source):
        if (
            isinstance(message, dict)
            and message.get("role") == "user"
            and message.get("type", "message") == "message"
        ):
            return _text(message.get("content"))
    return ""


def content_limit(raw: dict[str, Any], protocol: str) -> str | None:
    """The refusal text when the caller's own words reach a hard limit, else None."""
    from packages.content_policy import text_limit, text_refusal
    from packages.language import question_language
    from services.orchestrator.link_requests import own_words

    text = caller_text(raw, protocol)
    if not text.strip():
        return None
    category = text_limit(own_words(text))
    if category is None:
        return None
    return text_refusal(category, question_language(text, "en"))


def _sse(event: dict[str, Any], named: bool) -> str:
    head = "event: " + str(event["type"]) + "\n" if named else ""
    return head + "data: " + json.dumps(event) + "\n\n"


def refusal(raw: dict[str, Any], protocol: str, model: str) -> Response | None:
    """The short refusal of a request that reaches a hard limit, from no model.

    Chat Completions ends it with finish_reason "content_filter", Messages with
    stop_reason "refusal", Responses as incomplete with reason "content_filter".
    Nothing is charged: no model was called.
    """
    message = content_limit(raw, protocol)
    if message is None:
        return None
    from services.orchestrator.dev_chat import Output, RawChatInput
    from services.orchestrator.dev_messages import Wire as MessagesWire
    from services.orchestrator.dev_responses import Wire as ResponsesWire

    shape = RawChatInput.model_validate(
        {"model": "raw/assistant", "messages": [{"role": "user", "content": "-"}]}
    )
    output = Output(uuid.uuid4().hex, shape, model)
    frames: list[dict[str, Any]] = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": message},
                    "finish_reason": None,
                }
            ]
        },
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "content_filter"}]},
        {"choices": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0}},
    ]
    headers = {"Cache-Control": "no-store"}
    if protocol == "chat":
        chunks = [output.feed(frame) for frame in frames]
        if not raw.get("stream"):
            return JSONResponse(output.result(), headers=headers)
        options = raw.get("stream_options")
        if not (isinstance(options, dict) and options.get("include_usage")):
            chunks = chunks[:-1]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)

        async def chat_stream() -> AsyncIterator[str]:
            yield body + "data: [DONE]\n\n"

        return StreamingResponse(
            chat_stream(), media_type="text/event-stream", headers=headers
        )
    wire = (
        ResponsesWire(output, raw)
        if protocol == "responses"
        else MessagesWire(output, raw)
    )
    events = list(wire.start())
    for frame in frames:
        events += wire.feed(output.feed(frame))
    if not raw.get("stream"):
        return JSONResponse(wire.result(), headers=headers)
    text = "".join(_sse(event, True) for event in events)

    async def stream() -> AsyncIterator[str]:
        yield text

    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


class OutputCap:
    """The caller's output limit on the assistant's final answer, in estimated tokens."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.room = limit * TOKEN_BYTES
        self.cut = False

    def take(self, text: str) -> str:
        """The part of ``text`` that still fits; once cut, nothing more is shown."""
        if self.cut:
            return ""
        data = text.encode()
        if len(data) <= self.room:
            self.room -= len(data)
            return text
        kept = data[: self.room].decode(errors="ignore")
        self.room, self.cut = 0, True
        return kept


def chat_limit(raw: dict[str, Any]) -> int | None:
    """Remove the caller's output limit from an assistant Chat Completions body.

    max_completion_tokens wins over the legacy max_tokens. The assistant's internal
    calls keep their own budget; the limit is applied to the answer (capped_chat).
    Raises ValueError for a limit that is not a positive integer.
    """
    current = raw.pop("max_completion_tokens", None)
    legacy = raw.pop("max_tokens", None)
    limit = current if current is not None else legacy
    if limit is None:
        return None
    if type(limit) is not int or limit < 1:
        raise ValueError(
            "max_completion_tokens" if current is not None else "max_tokens"
        )
    return limit


def _cap_chunk(chunk: dict[str, Any], model: str, cap: OutputCap | None) -> None:
    if "model" in chunk:
        chunk["model"] = model
    metadata = chunk.get("atlas")
    if (
        cap is not None
        and isinstance(metadata, dict)
        and "max_output_tokens" in metadata
    ):
        metadata["max_output_tokens"] = cap.limit
    for choice in chunk.get("choices") or []:
        if not isinstance(choice, dict) or cap is None:
            continue
        delta = choice.get("delta") or choice.get("message")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            kept = cap.take(delta["content"])
            if kept or "message" in choice:
                delta["content"] = kept
            else:
                delta.pop("content")
        if cap.cut and choice.get("finish_reason"):
            choice["finish_reason"] = "length"


async def _capped_stream(
    source: AsyncIterable[Any], model: str, cap: OutputCap | None
) -> AsyncIterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    async for piece in source:
        buffer += piece if isinstance(piece, str) else decoder.decode(bytes(piece))
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            if event.startswith("data: {"):
                chunk = json.loads(event[6:])
                _cap_chunk(chunk, model, cap)
                event = "data: " + json.dumps(chunk)
            yield event + "\n\n"
    if buffer:
        yield buffer


def capped_chat(reply: Response, model: str, limit: int | None) -> Response:
    """The assistant's Chat Completions answer under the caller's model name and limit."""
    if reply.status_code != 200:
        return reply
    cap = OutputCap(limit) if limit is not None else None
    headers = {
        name: value
        for name, value in reply.headers.items()
        if name not in {"content-length", "content-type"}
    }
    if isinstance(reply, StreamingResponse):
        return StreamingResponse(
            _capped_stream(reply.body_iterator, model, cap),
            status_code=reply.status_code,
            media_type="text/event-stream",
            headers=headers,
            background=reply.background,
        )
    body = json.loads(bytes(reply.body))
    if not isinstance(body, dict):
        return reply
    _cap_chunk(body, model, cap)
    return JSONResponse(body, status_code=reply.status_code, headers=headers)


async def cap_frames(
    source: AsyncIterator[dict[str, Any]], cap: OutputCap | None
) -> AsyncIterator[dict[str, Any]]:
    """Provider-style frames of the assistant, cut at the caller's output limit.

    After the cut the rest of the answer is read but not shown, so the usage frame
    still reports what the answer cost.
    """
    async for frame in source:
        if cap is None:
            yield frame
            continue
        choices = []
        for choice in frame["choices"]:
            if cap.cut:
                continue
            content = choice["delta"].get("content", "")
            kept = cap.take(content) if content else ""
            if cap.cut:
                choices.append(
                    {
                        "index": 0,
                        "delta": {"content": kept} if kept else {},
                        "finish_reason": "length",
                    }
                )
            elif kept or choice["finish_reason"]:
                choices.append(dict(choice, delta={"content": kept} if kept else {}))
        if choices:
            yield {"choices": choices}
        if "usage" in frame:
            yield {"choices": [], "usage": frame["usage"]}
