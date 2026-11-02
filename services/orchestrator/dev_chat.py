"""Chat input normalization and output assembly; tools are never executed here."""

from packages.limits import LimitError

import json
import time
from typing import Any, Literal
from pydantic import Field, model_validator
from packages.dev_request import Call, Request, ResponseFormat, Sampling, Strict, Tool
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


class RawChatInput(Sampling):
    """Raw gateway request: retain OpenAI content parts instead of normalizing them."""

    n: int = Field(default=1, ge=1, le=8)
    logprobs: bool = False
    top_logprobs: int | None = Field(default=None, ge=0, le=20)

    @model_validator(mode="before")
    @classmethod
    def completion_tokens(cls, value: Any) -> Any:
        # max_completion_tokens is the current OpenAI name of max_tokens.
        if isinstance(value, dict) and "max_completion_tokens" in value:
            value = dict(value)
            maximum = value.pop("max_completion_tokens")
            if "max_tokens" in value and value["max_tokens"] != maximum:
                raise ValueError("conflicting_maximum")
            value["max_tokens"] = maximum
        return value

    model: str = Field(pattern=r"^raw/.+$")
    messages: list[RawMessage] = Field(min_length=1, max_length=4096)
    tools: list[Tool] = Field(default_factory=list, max_length=64)
    response_format: ResponseFormat = Field(
        default_factory=lambda: ResponseFormat(type="text")
    )
    max_tokens: int = Field(default=4096, ge=1, le=32768)
    temperature: float = Field(default=0, ge=0, le=2)
    stream: bool = False
    stream_options: StreamOptions = StreamOptions()
    tool_choice: str | dict[str, Any] = "auto"
    parallel_tool_calls: bool = True
    reasoning_effort: Literal["none", "low", "medium", "high"] = "none"

    @property
    def validate_output(self) -> bool:
        return False


class Choice:
    """One answer of a completion; n > 1 asks the provider for several."""

    def __init__(self) -> None:
        self.text = ""
        self.calls: dict[int, dict[str, Any]] = {}
        # Provider index -> Atlas index, for providers that reuse index 0.
        self.call_slots: dict[int, int] = {}
        self.reason: str | None = None
        self.logprobs: list[Any] = []


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
        self.choices: dict[int, Choice] = {0: Choice()}
        self.usage: dict[str, Any] | None = None
        self.served_by: str | None = None
        self.privacy: str | None = None

    # The first choice keeps the single-answer interface used by every route.
    @property
    def text(self) -> str:
        return self.choices[0].text

    @text.setter
    def text(self, value: str) -> None:
        self.choices[0].text = value

    @property
    def calls(self) -> dict[int, dict[str, Any]]:
        return self.choices[0].calls

    @calls.setter
    def calls(self, value: dict[int, dict[str, Any]]) -> None:
        self.choices[0].calls = value

    @property
    def reason(self) -> str | None:
        return self.choices[0].reason

    @reason.setter
    def reason(self, value: str | None) -> None:
        self.choices[0].reason = value

    def wanted(self) -> int:
        return getattr(self.request, "n", 1)

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
            if frame.get("privacy"):
                self.privacy = str(frame["privacy"])
                chunk["privacy"] = self.privacy
        for choice in frame["choices"]:
            position = choice.get("index", 0)
            if type(position) is not int or not 0 <= position < self.wanted():
                raise ValueError("invalid_choice_index")
            st = self.choices.setdefault(position, Choice())
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
                st.text += content
                clean["content"] = content
            changes = []
            for call in delta.get("tool_calls") or []:
                index = call["index"]
                if type(index) is not int or not 0 <= index < 64:
                    raise ValueError("invalid_call_index")
                # Some providers stream every complete call at index 0 with a new
                # id: a new id on a slot already holding another call opens a call.
                slot = st.call_slots.get(index, index)
                new_id = call.get("id")
                if (
                    isinstance(new_id, str)
                    and new_id
                    and slot in st.calls
                    and st.calls[slot]["id"]
                    and new_id != st.calls[slot]["id"]
                ):
                    slot = len(st.calls)
                st.call_slots[index] = slot
                index = slot
                if index > len(st.calls) or index >= 64:
                    raise ValueError("invalid_call_index")
                target = st.calls.setdefault(
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
                    # Continuation chunks may repeat the id or name as "": no change.
                    if value is not None and value != "":
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
            if len(st.text) > 131072:
                raise LimitError("content_limit", len(st.text), 131072, "characters")
            reason = choice["finish_reason"]
            if reason is not None:
                st.reason = "tool_calls" if st.calls and reason == "stop" else reason
                ids = [c["id"] for c in st.calls.values()]
                if len(set(ids)) != len(ids) or any(not i for i in ids):
                    raise ValueError("invalid_call_id")
                if reason != "length":
                    choice_mode = self.request.tool_choice
                    if (
                        (choice_mode == "none" and st.calls)
                        or (choice_mode == "required" and not st.calls)
                        or (not self.request.parallel_tool_calls and len(st.calls) > 1)
                    ):
                        raise ValueError("tool_choice_violated")
                    if isinstance(choice_mode, dict) and (
                        not st.calls
                        or any(
                            c["function"]["name"] != choice_mode["function"]["name"]
                            for c in st.calls.values()
                        )
                    ):
                        raise ValueError("tool_choice_violated")
                    if bool(st.calls) != (st.reason == "tool_calls"):
                        raise ValueError("inconsistent_finish")
                    for c in st.calls.values():
                        if c["function"]["name"] not in {
                            t.function.name for t in self.request.tools
                        } or not isinstance(
                            json.loads(c["function"]["arguments"]), dict
                        ):
                            raise ValueError("invalid_tool_call")
                        json.dumps(
                            json.loads(c["function"]["arguments"]), allow_nan=False
                        )
            entry: dict[str, Any] = {
                "index": position,
                "delta": clean,
                "finish_reason": st.reason,
            }
            if getattr(self.request, "logprobs", False):
                content_logprobs = (choice.get("logprobs") or {}).get("content") or []
                st.logprobs.extend(content_logprobs)
                entry["logprobs"] = {"content": content_logprobs}
            chunk["choices"].append(entry)
        return chunk

    def validation_error(self) -> str | None:
        fmt = self.request.response_format
        for st in self.choices.values():
            if fmt.type != "text":
                problem = validate_json(
                    st.text, fmt.json_schema.schema_ if fmt.json_schema else None
                )
                if problem is not None:
                    return problem
            elif self.request.tool_choice == "none" and not st.text.strip():
                return "empty_text"
        return None

    def result(self) -> dict[str, Any]:
        if (
            any(st.reason is None for st in self.choices.values())
            or len(self.choices) != self.wanted()
            or self.usage is None
        ):
            raise ValueError("incomplete_response")
        choices = []
        for position in sorted(self.choices):
            st = self.choices[position]
            message: dict[str, Any] = {"role": "assistant", "content": st.text or None}
            if st.calls:
                message["tool_calls"] = list(st.calls.values())
            entry: dict[str, Any] = {
                "index": position,
                "message": message,
                "finish_reason": st.reason,
            }
            if getattr(self.request, "logprobs", False):
                entry["logprobs"] = {"content": st.logprobs}
            choices.append(entry)
        result = dict(
            self.base,
            object="chat.completion",
            usage=self.usage,
            choices=choices,
        )
        if self.served_by:
            result["served_by"] = self.served_by
        if self.privacy:
            result["privacy"] = self.privacy
        return result
