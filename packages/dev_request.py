"""REQ-DEV-003/005: strict text/function request shared with the code gateway."""

import json
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Function(Strict):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str = Field(default="", max_length=16000)
    parameters: dict[str, Any]
    strict: bool = False


class Tool(Strict):
    type: Literal["function"] = "function"
    function: Function


class CallFunction(Strict):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    arguments: str = Field(max_length=65536)


class Call(Strict):
    id: str = Field(min_length=1, max_length=200)
    type: Literal["function"] = "function"
    function: CallFunction


class Message(Strict):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[Call] = Field(default_factory=list, max_length=64)
    tool_call_id: str | None = None
    name: str | None = Field(default=None, max_length=64)


class Request(Strict):
    messages: list[Message] = Field(min_length=1, max_length=1024)
    tools: list[Tool] = Field(default_factory=list, max_length=64)
    max_tokens: int = Field(default=512, ge=1, le=8192)
    temperature: float = Field(default=0, ge=0, le=2)
    tool_choice: str | dict[str, Any] = "auto"
    parallel_tool_calls: bool = True
    reasoning_effort: Literal["none", "low"] = "none"

    @model_validator(mode="after")
    def validate_conversation(self) -> "Request":
        names = [t.function.name for t in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("duplicate_tool")
        choice = self.tool_choice
        if isinstance(choice, str):
            if choice not in ("auto", "none", "required") or (
                choice == "required" and not names
            ):
                raise ValueError("invalid_tool_choice")
        elif (
            set(choice) != {"type", "function"}
            or choice["type"] != "function"
            or not isinstance(choice["function"], dict)
            or set(choice["function"]) != {"name"}
            or choice["function"]["name"] not in names
        ):
            raise ValueError("invalid_tool_choice")
        pending: set[str] = set()
        used: set[str] = set()
        for message in self.messages:
            if message.role == "tool":
                if (
                    message.tool_call_id not in pending
                    or message.tool_calls
                    or message.content is None
                ):
                    raise ValueError("orphan_tool_result")
                pending.remove(message.tool_call_id)
                continue
            if pending or message.tool_call_id is not None:
                raise ValueError("missing_tool_result")
            if message.tool_calls and message.role != "assistant":
                raise ValueError("invalid_tool_role")
            if not message.tool_calls and message.content is None:
                raise ValueError("missing_content")
            for call in message.tool_calls:
                if call.id in used or not isinstance(
                    json.loads(call.function.arguments), dict
                ):
                    raise ValueError("invalid_tool_call")
                json.dumps(json.loads(call.function.arguments), allow_nan=False)
                pending.add(call.id)
                used.add(call.id)
        if pending:
            raise ValueError("missing_tool_result")
        return self
