"""Chat input normalization and output assembly; tools are never executed here."""

from packages.limits import LimitError

import json
import time
from typing import Any, Literal
from pydantic import Field, model_validator
from packages.dev_request import Call, Request, ResponseFormat, Strict, Tool
from packages.dev_format import validate_json


class StreamOptions(Strict):
    include_usage: bool = False


class ChatInput(Request):
    model: Literal["atlas-code"]
    n: int = Field(default=1, ge=1, le=1, exclude=True)
    stream: bool = False
    stream_options: StreamOptions = StreamOptions()

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, value: Any) -> Any:
        value = json.loads(json.dumps(value, allow_nan=False))
        if not isinstance(value, dict):
            raise ValueError("invalid_input")
        if "max_completion_tokens" in value:
            maximum = value.pop("max_completion_tokens")
            if "max_tokens" in value and value["max_tokens"] != maximum:
                raise ValueError("conflicting_maximum")
            value["max_tokens"] = maximum
        for message in value.get("messages", []):
            if message.get("role") == "developer":
                message["role"] = "system"
            content = message.get("content")
            if isinstance(content, list):
                if any(
                    set(p) != {"type", "text"}
                    or p["type"] != "text"
                    or not isinstance(p["text"], str)
                    for p in content
                ):
                    raise ValueError("unsupported_content")
                message["content"] = "\n".join(p["text"] for p in content)
        return value


class RawMessage(Strict):
    """Provider-compatible message content, including vision content parts."""

    role: Literal["system", "user", "assistant", "tool", "developer"]
    content: Any = None
    tool_calls: list[Call] = Field(default_factory=list, max_length=64)
    tool_call_id: str | None = None
    name: str | None = Field(default=None, max_length=64)


class RawChatInput(Strict):
    """Raw gateway request: retain OpenAI content parts instead of normalizing them."""

    model: str = Field(pattern=r"^raw/.+$")
    messages: list[RawMessage] = Field(min_length=1, max_length=1024)
    tools: list[Tool] = Field(default_factory=list, max_length=64)
    response_format: ResponseFormat = Field(
        default_factory=lambda: ResponseFormat(type="text")
    )
    max_tokens: int = Field(default=512, ge=1, le=8192)
    temperature: float = Field(default=0, ge=0, le=2)
    stream: bool = False
    stream_options: StreamOptions = StreamOptions()
    tool_choice: str | dict[str, Any] = "auto"
    parallel_tool_calls: bool = True
    reasoning_effort: Literal["none", "low", "medium", "high"] = "none"

    @property
    def validate_output(self) -> bool:
        return False


class Output:
    def __init__(
        self,
        request_id: str,
        request: Request | RawChatInput,
        model: str = "atlas-code",
    ) -> None:
        self.base = {
            "id": "chatcmpl-" + request_id,
            "model": model,
            "created": int(time.time()),
        }
        self.request = request
        self.text = ""
        self.calls: dict[int, dict[str, Any]] = {}
        self.reason: str | None = None
        self.usage: dict[str, Any] | None = None
        self.served_by: str | None = None

    def feed(self, frame: dict[str, Any]) -> dict[str, Any]:
        chunk: dict[str, Any] = dict(
            self.base, object="chat.completion.chunk", choices=[]
        )
        if "usage" in frame:
            self.usage = frame["usage"]
            assert self.usage is not None
            self.usage["total_tokens"] = (
                self.usage["prompt_tokens"] + self.usage["completion_tokens"]
            )
            chunk["usage"] = self.usage
            if frame.get("served_by"):
                self.served_by = str(frame["served_by"])
                chunk["served_by"] = self.served_by
        for choice in frame["choices"]:
            delta = choice["delta"]
            clean: dict[str, Any] = {}
            if "role" in delta:
                if delta["role"] != "assistant":
                    raise ValueError("invalid_role")
                clean["role"] = "assistant"
            content = delta.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ValueError("invalid_content")
                self.text += content
                clean["content"] = content
            changes = []
            for call in delta.get("tool_calls") or []:
                index = call["index"]
                if (
                    type(index) is not int
                    or not 0 <= index < 64
                    or index > len(self.calls)
                ):
                    raise ValueError("invalid_call_index")
                target = self.calls.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                update: dict[str, Any] = {
                    "index": index,
                    "type": "function",
                    "function": {},
                }
                for source, destination, field in (
                    (call, target, "id"),
                    (call["function"], target["function"], "name"),
                ):
                    value = source.get(field)
                    if value is not None:
                        if (
                            not isinstance(value, str)
                            or len(value) > 200
                            or (destination[field] and value != destination[field])
                        ):
                            raise ValueError("unstable_call_identity")
                        destination[field] = value
                        (update if field == "id" else update["function"])[field] = value
                arguments = call["function"].get("arguments", "")
                if not isinstance(arguments, str):
                    raise ValueError("invalid_arguments")
                target["function"]["arguments"] += arguments
                update["function"]["arguments"] = arguments
                if len(target["function"]["arguments"]) > 65536:
                    raise LimitError(
                        "arguments_limit",
                        len(target["function"]["arguments"]),
                        65536,
                        "characters",
                    )
                changes.append(update)
            if changes:
                clean["tool_calls"] = changes
            if len(self.text) > 131072:
                raise LimitError("content_limit", len(self.text), 131072, "characters")
            reason = choice["finish_reason"]
            if reason is not None:
                self.reason = (
                    "tool_calls" if self.calls and reason == "stop" else reason
                )
                ids = [c["id"] for c in self.calls.values()]
                if len(set(ids)) != len(ids) or any(not i for i in ids):
                    raise ValueError("invalid_call_id")
                if reason != "length":
                    choice_mode = self.request.tool_choice
                    if (
                        (choice_mode == "none" and self.calls)
                        or (choice_mode == "required" and not self.calls)
                        or (
                            not self.request.parallel_tool_calls and len(self.calls) > 1
                        )
                    ):
                        raise ValueError("tool_choice_violated")
                    if isinstance(choice_mode, dict) and (
                        not self.calls
                        or any(
                            c["function"]["name"] != choice_mode["function"]["name"]
                            for c in self.calls.values()
                        )
                    ):
                        raise ValueError("tool_choice_violated")
                    if bool(self.calls) != (self.reason == "tool_calls"):
                        raise ValueError("inconsistent_finish")
                    for c in self.calls.values():
                        if c["function"]["name"] not in {
                            t.function.name for t in self.request.tools
                        } or not isinstance(
                            json.loads(c["function"]["arguments"]), dict
                        ):
                            raise ValueError("invalid_tool_call")
                        json.dumps(
                            json.loads(c["function"]["arguments"]), allow_nan=False
                        )
            chunk["choices"].append(
                {"index": 0, "delta": clean, "finish_reason": self.reason}
            )
        return chunk

    def validation_error(self) -> str | None:
        fmt = self.request.response_format
        if fmt.type != "text":
            return validate_json(
                self.text, fmt.json_schema.schema_ if fmt.json_schema else None
            )
        if self.request.tool_choice == "none" and not self.text.strip():
            return "empty_text"
        return None

    def result(self) -> dict[str, Any]:
        if self.reason is None or self.usage is None:
            raise ValueError("incomplete_response")
        message: dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.calls:
            message["tool_calls"] = list(self.calls.values())
        result = dict(
            self.base,
            object="chat.completion",
            usage=self.usage,
            choices=[{"index": 0, "message": message, "finish_reason": self.reason}],
        )
        if self.served_by:
            result["served_by"] = self.served_by
        return result
