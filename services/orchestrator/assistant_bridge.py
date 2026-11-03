"""The Atlas assistant on the Responses and Messages formats (API parity).

The request is converted to the assistant's Chat Completions form, the assistant answers as a
stream, and that stream goes through the same assembly and wire as the direct models. The
assistant runs its own tools (web search, documents): client tools are refused by name rather
than ignored, and the hosted web search tool is accepted since the assistant searches anyway.
"""

import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from packages.profiles import PROFILES
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
    return {
        "model": PROFILES[0] if name == "atlas" else name,
        "messages": converted["messages"],
        "stream": True,
        "max_tokens": min(int(converted.get("max_tokens") or 6000), 16000),
        "atlas_ui": False,
    }


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


async def serve(request: Request, raw: dict[str, Any], protocol: str) -> Response:
    from services.orchestrator.api_key_edge import require
    from services.orchestrator.chat_api import chat as assistant_chat
    from services.orchestrator.dev_messages import Wire as MessagesWire
    from services.orchestrator.dev_responses import Wire as ResponsesWire

    missing = require(request, "assistant")
    if missing is not None:
        return missing
    body = assistant_body(raw, protocol)
    request._body = json.dumps(body).encode()
    reply = await assistant_chat(request)
    if not isinstance(reply, StreamingResponse) or reply.status_code != 200:
        return reply
    shape = RawChatInput.model_validate(
        {"model": "raw/assistant", "messages": [{"role": "user", "content": "-"}]}
    )
    output = Output("assistant", shape, str(raw["model"]))
    wire = (
        ResponsesWire(output, raw)
        if protocol == "responses"
        else MessagesWire(output, raw)
    )

    async def events() -> AsyncGenerator[dict[str, Any], None]:
        finished = False
        async for frame in frames(reply):
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
    return JSONResponse(wire.result())
