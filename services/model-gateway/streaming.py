"""Bounded SSE decoder; deltas precede the validated terminal result (M11)."""

from packages.limits import LimitError, MalformedToolCall

import json
from typing import Literal

from pydantic import BaseModel, Field

from agent_provider import Completion


class FunctionDelta(BaseModel):
    name: str = Field(default="", max_length=80)
    arguments: str = Field(default="", max_length=16000)


class CallDelta(BaseModel):
    index: int = Field(ge=0, lt=10, strict=True)
    id: str = Field(default="", max_length=200)
    type: Literal["function"] = "function"
    function: FunctionDelta = Field(default_factory=FunctionDelta)


class Delta(BaseModel):
    content: str | None = None
    reasoning: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[CallDelta] = Field(default_factory=list, max_length=10)


class StreamChoice(BaseModel):
    index: Literal[0]
    delta: Delta
    finish_reason: Literal["stop", "tool_calls", "length"] | None = None


class Frame(BaseModel):
    id: str | None = None
    error: None = None
    choices: list[StreamChoice] = Field(max_length=1)
    usage: dict[str, object] | None = None


class StreamDecoder:
    def __init__(self, allow_length: bool = False) -> None:
        self.allow_length = allow_length
        self.provider_id: str | None = None
        self.reasoning = ""
        self.buffer = bytearray()
        self.total = 0
        self.text = ""
        self.calls: dict[int, dict[str, str]] = {}
        self.usage: dict[str, object] | None = None
        self.reason: str | None = None
        self.result: dict[str, object] | None = None

    def feed(self, piece: bytes) -> list[dict[str, object]]:
        self.total += len(piece)
        maximum = 8000000 if self.allow_length else 800000
        if self.total > maximum:
            raise LimitError(
                "stream_limit_or_trailing_data", self.total, maximum, "bytes"
            )
        if self.result is not None and piece.strip():
            raise ValueError("stream_limit_or_trailing_data")
        self.buffer.extend(piece)
        events: list[dict[str, object]] = []
        while b"\n" in self.buffer:
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            line = line.rstrip(b"\r")
            if not line or line.startswith(b":"):
                continue
            if not line.startswith(b"data:"):
                raise ValueError("invalid_sse_field")
            data = line[5:].strip()
            if self.result is not None:
                raise ValueError("trailing_frame")
            if data == b"[DONE]":
                self._done()
                continue
            frame = Frame.model_validate(json.loads(data))
            if frame.id:
                if self.provider_id and frame.id != self.provider_id:
                    raise ValueError("provider_id_changed")
                self.provider_id = frame.id
            if frame.usage is not None:
                if self.usage is not None:
                    raise ValueError("duplicate_usage")
                self.usage = frame.usage
            for choice in frame.choices:
                if self.reason is not None:
                    raise ValueError("delta_after_finish")
                delta = choice.delta
                for key, value in (
                    ("content", delta.content),
                    ("reasoning_content", delta.reasoning_content or delta.reasoning),
                ):
                    if value:
                        events.append({"delta": {key: value}})
                        if key == "content":
                            self.text += value
                        else:
                            self.reasoning += value
                for call in delta.tool_calls:
                    target = self.calls.setdefault(
                        call.index, {"id": "", "name": "", "arguments": ""}
                    )
                    target["id"] += call.id
                    target["name"] += call.function.name
                    target["arguments"] += call.function.arguments
                    for key, maximum in (
                        ("id", 200),
                        ("name", 80),
                        ("arguments", 16000),
                    ):
                        if len(target[key]) > maximum:
                            if key == "name":
                                measured = len(target[key])
                                self.calls.clear()
                                raise MalformedToolCall(measured)
                            raise LimitError(
                                "tool_limit",
                                len(target[key]),
                                maximum,
                                key + " characters",
                            )
                self.reason = choice.finish_reason
        return events

    def _done(self) -> None:
        calls = [self.calls[i] for i in sorted(self.calls)]
        completion = Completion.model_validate(
            {
                "usage": self.usage,
                "choices": [
                    {
                        "finish_reason": self.reason,
                        "message": {
                            "content": self.text,
                            "tool_calls": [
                                {
                                    "id": c["id"],
                                    "type": "function",
                                    "function": {
                                        "name": c["name"],
                                        "arguments": c["arguments"],
                                    },
                                }
                                for c in calls
                            ],
                        },
                    }
                ],
            }
        )
        if not self.allow_length and completion.usage.completion_tokens > 2048:
            raise LimitError(
                "output_budget", completion.usage.completion_tokens, 2048, "tokens"
            )
        if self.reason == "length" and not self.allow_length:
            raise ValueError("incomplete_stream")
        if not self.text and not calls and not self.allow_length:
            raise ValueError("empty_stream")
        if len({c["id"] for c in calls}) != len(calls):
            raise ValueError("duplicate_call")
        if bool(calls) != (self.reason == "tool_calls"):
            raise ValueError("inconsistent_finish")
        self.result = {
            "text": self.text,
            "calls": calls,
            "usage": completion.usage.model_dump(),
        }
        if self.provider_id:
            self.result["provider_id"] = self.provider_id
        if self.allow_length:
            details = (self.usage or {}).get("completion_tokens_details")
            reasoning_tokens = (
                details.get("reasoning_tokens") if isinstance(details, dict) else None
            )
            if reasoning_tokens is not None and (
                type(reasoning_tokens) is not int
                or not 0 <= reasoning_tokens <= completion.usage.completion_tokens
            ):
                raise ValueError("invalid_reasoning_usage")
            self.result.update(
                finish_reason=self.reason,
                reasoning=self.reasoning,
                reasoning_tokens=reasoning_tokens,
            )

    def finish(self) -> None:
        if self.buffer.strip() or self.result is None:
            raise ValueError("incomplete_stream")
