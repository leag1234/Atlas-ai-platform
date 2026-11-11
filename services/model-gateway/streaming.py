"""Bounded SSE decoder; deltas precede the validated terminal result (M11)."""

from packages.limits import LimitError, MalformedToolCall

import json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from agent_provider import Completion, Usage
from packages.validation import malformed_tool_name
from stream_usage import validate_usage


class FunctionDelta(BaseModel):
    name: str = Field(default="", max_length=80)
    arguments: str = Field(default="", max_length=1048576)


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

    @staticmethod
    def thinking_text(value: object) -> str:
        """Reasoning text of typed thinking parts ({"type": "thinking", "thinking": [...]})."""
        if not isinstance(value, list):
            return ""
        text: list[str] = []
        for part in value:
            if isinstance(part, dict) and part.get("type") == "thinking":
                inner = part.get("thinking")
                for piece in inner if isinstance(inner, list) else []:
                    if isinstance(piece, dict) and isinstance(piece.get("text"), str):
                        text.append(piece["text"])
        return "".join(text)

    @field_validator("content", mode="before")
    @classmethod
    def text_content(cls, value: object) -> object:
        """Normalize typed content while retaining provider citation identities."""
        if not isinstance(value, list):
            return value
        text: list[str] = []
        for part in value:
            if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                raise ValueError("invalid_content_part")
            if part["type"] == "text":
                chunk = part.get("text")
                if not isinstance(chunk, str):
                    raise ValueError("invalid_text_content_part")
                text.append(chunk)
            elif part["type"] == "reference":
                reference_ids = part.get("reference_ids")
                if (
                    not isinstance(reference_ids, list)
                    or not reference_ids
                    or len(reference_ids) > 10
                    or not all(
                        isinstance(reference_id, str) and 0 < len(reference_id) <= 200
                        for reference_id in reference_ids
                    )
                ):
                    raise ValueError("invalid_reference_content_part")
                text.extend(
                    "[[atlas-reference:" + reference_id + "]]"
                    for reference_id in reference_ids
                )
        # Text parts can finish an answer word or continue a reference hostname.
        # Their meaning does not depend on sharing an SSE frame with a reference.
        return "".join(text)


class StreamChoice(BaseModel):
    index: Literal[0]
    delta: Delta
    finish_reason: Literal["stop", "tool_calls", "length"] | None = None


class Frame(BaseModel):
    id: str | None = None
    error: None = None
    choices: list[StreamChoice] = Field(max_length=1)
    usage: dict[str, object] | None = None


STREAM_BYTES_PER_TOKEN = 1024


# Arguments this long are a document or an app being written, not a stalled call.
TOOL_PROGRESS_CHARS = 2000


class StreamDecoder:
    def __init__(
        self,
        allow_length: bool = False,
        ignore_calls: bool = False,
        max_tokens: int | None = None,
    ) -> None:
        self.allow_length = allow_length
        self.ignore_calls = ignore_calls
        # The output is bounded by max_tokens (usage is checked against it).
        # The raw stream cap only guards memory against a provider that never
        # stops: each streamed token costs a few hundred bytes of SSE envelope,
        # so the cap scales with max_tokens instead of cutting a long answer.
        self.max_tokens = max_tokens
        self.provider_id: str | None = None
        self.reasoning = ""
        self.buffer = bytearray()
        self.total = 0
        self.text = ""
        self.calls: dict[int, dict[str, str]] = {}
        self.tool_progress_sent = False
        self.ignored_calls = False
        self.usage: dict[str, object] | None = None
        self.reason: str | None = None
        self.result: dict[str, object] | None = None

    def feed(self, piece: bytes) -> list[dict[str, object]]:
        self.total += len(piece)
        maximum = max(
            8000000 if self.allow_length else 800000,
            (self.max_tokens or 0) * STREAM_BYTES_PER_TOKEN,
        )
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
            payload = json.loads(data)
            if self.ignore_calls and isinstance(payload, dict):
                choices = payload.get("choices", [])
                if isinstance(choices, list):
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta")
                        if isinstance(delta, dict):
                            self.ignored_calls = (
                                self.ignored_calls or "tool_calls" in delta
                            )
                            delta.pop("tool_calls", None)
            try:
                frame = Frame.model_validate(payload)
            except ValidationError as error:
                malformed = malformed_tool_name(error)
                if malformed is not None:
                    self.calls.clear()
                    raise malformed from None
                raise
            if frame.id:
                if self.provider_id and frame.id != self.provider_id:
                    raise ValueError("provider_id_changed")
                self.provider_id = frame.id
            if frame.usage is not None:
                validate_usage(frame.usage, self.usage)
                Usage.model_validate(frame.usage)
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
                    if self.ignore_calls:
                        continue
                    target = self.calls.setdefault(
                        call.index, {"id": "", "name": "", "arguments": ""}
                    )
                    target["id"] += call.id
                    target["name"] += call.function.name
                    if len(target["name"]) > 80:
                        self.calls.clear()
                        raise MalformedToolCall(len(target["name"]))
                    target["arguments"] += call.function.arguments
                    if (
                        not self.tool_progress_sent
                        and len(target["arguments"]) >= TOOL_PROGRESS_CHARS
                    ):
                        # Tool arguments are not forwarded while they are written.
                        # Tell the caller once that a long call (a whole document
                        # or app) is under way, so that it is not cut as silent.
                        self.tool_progress_sent = True
                        events.append({"tool_progress": len(target["arguments"])})
                    # Arguments have no length cap of their own: an app is
                    # written in one call, and max_tokens bounds the output.
                    for key, maximum in (("id", 200),):
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
                self.reason = (
                    "stop"
                    if self.ignore_calls and choice.finish_reason == "tool_calls"
                    else choice.finish_reason
                )
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
        # Some compatible providers terminate valid tool payloads with stop.
        # Keep payload validation and reject missing calls or truncated tool data.
        if (calls and self.reason not in {"stop", "tool_calls"}) or (
            not calls and self.reason == "tool_calls"
        ):
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
