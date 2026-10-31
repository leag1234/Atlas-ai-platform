"""Text/function input translations at the stateless Responses boundary."""

from packages.limits import LimitError

import json
import logging
from typing import Any
from services.orchestrator.dev_chat import ChatInput


def fields(value: dict[str, Any], allowed: str) -> None:
    if set(value) - set(allowed.split()):
        raise ValueError("unsupported_field")


class SemanticFieldError(ValueError):
    """A client option whose semantics cannot be preserved by this route."""


def dropped(scope: str, names: set[str]) -> None:
    """Record only field names: request content must never reach compatibility logs."""
    if names:
        logging.getLogger("atlas.responses").warning(
            json.dumps(
                {
                    "event": "responses_fields_dropped",
                    "scope": scope,
                    "fields": sorted(names),
                }
            )
        )


def response_fields(value: dict[str, Any], allowed: set[str], scope: str) -> None:
    dropped(scope, set(value) - allowed)


def format_description(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


LOCAL_SHELL_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {"type": "array", "items": {"type": "string"}},
        "workdir": {"type": "string"},
        "timeout_ms": {"type": "integer", "minimum": 1},
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": ["command"],
    "additionalProperties": False,
}


CUSTOM_PARAMETERS = {
    "type": "object",
    "properties": {"input": {"type": "string"}},
    "required": ["input"],
    "additionalProperties": False,
}


def responses(value: dict[str, Any]) -> ChatInput:
    # OpenAI clients (Codex 0.98) send null for unset optional fields: treat null as absent.
    value = (
        {k: v for k, v in value.items() if v is not None}
        if isinstance(value, dict)
        else value
    )
    response_fields(
        value,
        {
            "model",
            "input",
            "instructions",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "max_output_tokens",
            "temperature",
            "stream",
            "store",
            "reasoning",
            "include",
            "prompt_cache_key",
            "metadata",
            "client_metadata",
            "text",
            "previous_response_id",
        },
        "request",
    )
    for field in ("previous_response_id", "project_id"):
        if field in value:
            raise SemanticFieldError("unsupported field: " + field)
    if value.get("store", False) is not False or value.get("include", []) not in (
        [],
        ["reasoning.encrypted_content"],
    ):
        raise ValueError("unsupported_storage_or_include")
    # metadata follows the public limits; client_metadata is the client's own
    # bookkeeping (Codex grows a turn description in it), never forwarded to a
    # model, so it only gets a wide size bound.
    for key, entries, characters in (
        ("metadata", 32, 1024),
        ("client_metadata", 64, 65536),
    ):
        meta = value.get(key, {})
        if isinstance(meta, dict):
            if len(meta) > entries:
                raise LimitError("invalid_metadata", len(meta), entries, "entries")
            for entry in meta.values():
                if isinstance(entry, str) and len(entry) > characters:
                    raise LimitError(
                        "invalid_metadata", len(entry), characters, "characters"
                    )
        if not isinstance(meta, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in meta.items()
        ):
            raise ValueError("invalid_metadata")
    if not isinstance(value.get("prompt_cache_key", ""), str):
        raise ValueError("invalid_cache_hint")
    reasoning = value.get("reasoning", {})
    if not isinstance(reasoning, dict):
        raise ValueError("invalid_reasoning")
    response_fields(reasoning, {"effort", "summary"}, "reasoning")
    messages: list[dict[str, Any]] = []
    instructions = value.get("instructions", "")
    if not isinstance(instructions, str):
        raise ValueError("invalid_instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})
    items = value["input"]
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    for item in items:
        kind = item.get("type", "message")
        if kind == "message":
            fields(item, "type role content id status")
            content = item["content"]
            if isinstance(content, list):
                parts = []
                for part in content:
                    fields(part, "type text annotations logprobs")
                    if (
                        part["type"] not in ("input_text", "output_text")
                        or not isinstance(part["text"], str)
                        or part.get("annotations")
                        or part.get("logprobs")
                    ):
                        raise ValueError("unsupported_content")
                    parts.append(part["text"])
                content = "\n".join(parts)
            messages.append({"role": item["role"], "content": content})
        elif kind == "function_call":
            fields(item, "type call_id name arguments id status")
            call = {
                "id": item["call_id"],
                "type": "function",
                "function": {"name": item["name"], "arguments": item["arguments"]},
            }
            if messages and messages[-1]["role"] == "assistant":
                messages[-1].setdefault("tool_calls", []).append(call)
            else:
                messages.append({"role": "assistant", "tool_calls": [call]})
        elif kind == "function_call_output":
            fields(item, "type call_id output id")
            if isinstance(item.get("id"), str) and len(item["id"]) > 1048576:
                raise LimitError(
                    "invalid_item_id", len(item["id"]), 1048576, "characters"
                )
            if "id" in item and (
                not isinstance(item["id"], str) or len(item["id"]) > 1048576
            ):
                raise ValueError("invalid_item_id")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item["call_id"],
                    "content": tool_output_text(item["output"]),
                }
            )
        elif kind in ("custom_tool_call", "local_shell_call"):
            response_fields(
                item,
                {"type", "id", "call_id", "name", "input", "action", "status"},
                kind,
            )
            call_id, name = item.get("call_id"), item.get("name")
            if not isinstance(call_id, str) or not isinstance(name, str):
                raise ValueError("invalid_tool_call")
            if kind == "custom_tool_call":
                input_value = item.get("input")
                if not isinstance(input_value, str):
                    raise ValueError("invalid_custom_tool_call")
                arguments: dict[str, Any] = {"input": input_value}
            else:
                action = item.get("action")
                if not isinstance(action, dict):
                    raise ValueError("invalid_local_shell_call")
                arguments = action
            call = {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            if messages and messages[-1]["role"] == "assistant":
                messages[-1].setdefault("tool_calls", []).append(call)
            else:
                messages.append({"role": "assistant", "tool_calls": [call]})
        elif kind in ("custom_tool_call_output", "local_shell_call_output"):
            response_fields(item, {"type", "id", "call_id", "output"}, kind)
            if not isinstance(item.get("call_id"), str) or "output" not in item:
                raise ValueError("invalid_tool_output")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item["call_id"],
                    "content": tool_output_text(item["output"]),
                }
            )
        elif kind == "reasoning":
            # Stateless raw providers cannot consume another provider's opaque state.
            fields(item, "type id summary content encrypted_content status")
            if not isinstance(item.get("summary", []), list) or not isinstance(
                item.get("encrypted_content", ""), (str, type(None))
            ):
                raise ValueError("invalid_reasoning_item")
        else:
            dropped("input", {"type:" + str(kind)})
    tools = []
    for tool in value.get("tools", []):
        if not isinstance(tool, dict) or not isinstance(tool.get("type"), str):
            # A malformed tool is a client error; only unknown typed tools are dropped.
            raise ValueError("invalid_tool")
        kind = tool.get("type")
        if kind == "function":
            response_fields(
                tool,
                {"type", "name", "description", "parameters", "strict"},
                "function_tool",
            )
            tools.append(
                {
                    "type": "function",
                    "function": {
                        k: v
                        for k, v in tool.items()
                        if k in {"name", "description", "parameters", "strict"}
                    },
                }
            )
        elif kind == "custom":
            response_fields(
                tool,
                {"type", "name", "description", "format", "grammar"},
                "custom_tool",
            )
            name = tool.get("name")
            if not isinstance(name, str):
                raise ValueError("invalid_custom_tool")
            description = tool.get("description", "")
            if not isinstance(description, str):
                raise ValueError("invalid_custom_tool")
            grammar = tool.get("grammar", tool.get("format"))
            if grammar is not None:
                description += "\nInput format: " + format_description(grammar)
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": CUSTOM_PARAMETERS,
                    },
                }
            )
        elif kind == "local_shell":
            response_fields(tool, {"type"}, "local_shell_tool")
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "local_shell",
                        "description": "Run a local shell command.",
                        "parameters": LOCAL_SHELL_PARAMETERS,
                    },
                }
            )
        elif kind == "namespace":
            response_fields(tool, {"type", "name", "tools"}, "namespace_tool")
            namespace, members = tool.get("name"), tool.get("tools")
            if not isinstance(namespace, str) or not isinstance(members, list):
                dropped("namespace_tool", {"invalid_namespace"})
                continue
            for member in members:
                if not isinstance(member, dict) or member.get("type") != "function":
                    dropped("namespace_tool", {"invalid_member"})
                    continue
                response_fields(
                    member,
                    {"type", "name", "description", "parameters", "strict"},
                    "namespace_function",
                )
                name = member.get("name")
                if not isinstance(name, str):
                    dropped("namespace_tool", {"invalid_member"})
                    continue
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            **{
                                k: v
                                for k, v in member.items()
                                if k in {"description", "parameters", "strict"}
                            },
                            "name": namespace + "__" + name,
                        },
                    }
                )
        else:
            dropped("tools", {"type:" + str(kind)})
    choice = value.get("tool_choice", "auto")
    if isinstance(choice, dict):
        fields(choice, "type name")
        if choice["type"] != "function":
            raise ValueError("unsupported_tool_choice")
        choice = {"type": "function", "function": {"name": choice["name"]}}
    text = value.get("text", {})
    if not isinstance(text, dict):
        raise ValueError("invalid_text_format")
    response_fields(text, {"format", "verbosity"}, "text")
    fmt = text.get("format", {"type": "text"})
    if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
        fields(fmt, "type name schema strict description")
        fmt = {
            "type": "json_schema",
            "json_schema": {k: v for k, v in fmt.items() if k != "type"},
        }
    return ChatInput.model_validate(
        {
            "response_format": fmt,
            "model": value["model"],
            "messages": messages,
            "tools": tools,
            "stream": value.get("stream", False),
            "stream_options": {"include_usage": True},
            # Codex sends no max_output_tokens; one long tool call (a whole file) can
            # exceed 4096 tokens, which ends the turn as incomplete (reported 2026-10-31).
            "max_tokens": value.get("max_output_tokens") or 16384,
            "temperature": value.get("temperature", 0),
            "parallel_tool_calls": value.get("parallel_tool_calls", True),
            "tool_choice": choice,
            "reasoning_effort": reasoning.get("effort", "none"),
        }
    )


def cache_hint(part: dict[str, Any]) -> None:
    if "cache_control" in part:
        cache = part["cache_control"]
        fields(cache, "type ttl")
        if cache["type"] != "ephemeral" or cache.get("ttl", "5m") not in ("5m", "1h"):
            raise ValueError("unsupported_cache_hint")


IMAGE_OMITTED = (
    "[Image omitted: Atlas coding models read text only. Describe the image in text, "
    "or save it to a file and refer to its path.]"
)


def tool_output_text(value: Any) -> str:
    """Tool output as text: Codex may return a list of text and image parts."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ValueError("invalid_tool_output")
    parts = []
    for part in value:
        if not isinstance(part, dict) or not isinstance(part.get("type"), str):
            raise ValueError("invalid_tool_output")
        if part["type"] in ("input_text", "output_text", "text"):
            if not isinstance(part.get("text"), str):
                raise ValueError("invalid_tool_output")
            parts.append(part["text"])
        elif part["type"] in ("input_image", "image"):
            parts.append(IMAGE_OMITTED)
        else:
            raise ValueError("unsupported_tool_output")
    return "\n".join(parts)


def text_blocks(value: Any) -> str:
    if isinstance(value, str):
        return value
    parts = []
    for part in value:
        fields(part, "type text cache_control")
        if part["type"] != "text" or not isinstance(part["text"], str):
            raise ValueError("unsupported_content")
        cache_hint(part)
        parts.append(part["text"])
    return "\n".join(parts)


def messages(value: dict[str, Any]) -> ChatInput:
    fields(
        value,
        "model messages system max_tokens tools tool_choice stream temperature metadata thinking output_config",
    )
    if value.get("thinking", {"type": "disabled"}) != {"type": "disabled"}:
        raise ValueError("unsupported_thinking")
    config = value.get("output_config", {})
    fields(config, "effort")
    if config.get("effort", "low") not in ("low", "medium", "high"):
        raise ValueError("unsupported_effort")
    metadata = value.get("metadata", {})
    if (
        not isinstance(metadata, dict)
        or set(metadata) - {"user_id"}
        or not isinstance(metadata.get("user_id", ""), str)
    ):
        raise ValueError("invalid_metadata")
    converted: list[dict[str, Any]] = []
    if "system" in value:
        converted.append({"role": "system", "content": text_blocks(value["system"])})
    for message in value["messages"]:
        fields(message, "role content")
        role, content = message["role"], message["content"]
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue
        texts, calls = [], []
        for block in content:
            kind = block["type"]
            if kind == "text":
                texts.append(text_blocks([block]))
            elif kind == "tool_use" and role == "assistant":
                fields(block, "type id name input")
                if not isinstance(block["input"], dict):
                    raise ValueError("invalid_tool_input")
                calls.append(
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block["input"], allow_nan=False),
                        },
                    }
                )
            elif kind == "tool_result" and role == "user" and not texts:
                fields(block, "type tool_use_id content is_error cache_control")
                cache_hint(block)
                if not isinstance(block.get("is_error", False), bool):
                    raise ValueError("invalid_tool_error")
                text = text_blocks(block.get("content", ""))
                if block.get("is_error"):
                    text = json.dumps({"tool_error": text})
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": text,
                    }
                )
            else:
                raise ValueError("unsupported_content")
        if texts or calls or not content:
            converted.append(
                {"role": role, "content": "\n".join(texts), "tool_calls": calls}
            )
    tools = []
    for tool in value.get("tools", []):
        fields(tool, "name description input_schema cache_control")
        cache_hint(tool)
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool["input_schema"],
                },
            }
        )
    choice = value.get("tool_choice", {"type": "auto"})
    fields(choice, "type name disable_parallel_tool_use")
    mode = choice["type"]
    selected: Any = {"auto": "auto", "none": "none", "any": "required"}.get(mode)
    if mode == "tool":
        selected = {"type": "function", "function": {"name": choice["name"]}}
    elif "name" in choice or selected is None:
        raise ValueError("unsupported_tool_choice")
    disabled = choice.get("disable_parallel_tool_use", False)
    if not isinstance(disabled, bool):
        raise ValueError("invalid_parallel_choice")
    return ChatInput.model_validate(
        {
            "model": value["model"],
            "messages": converted,
            "tools": tools,
            "max_tokens": value["max_tokens"],
            "stream": value.get("stream", False),
            "stream_options": {"include_usage": True},
            "tool_choice": selected,
            "parallel_tool_calls": not disabled,
            "temperature": value.get("temperature", 0),
        }
    )
