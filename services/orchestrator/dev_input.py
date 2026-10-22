"""Text/function input translations; unsupported extensions fail before inference."""

from packages.limits import LimitError

import json
from typing import Any
from services.orchestrator.dev_chat import ChatInput


def fields(value: dict[str, Any], allowed: str) -> None:
    if set(value) - set(allowed.split()):
        raise ValueError("unsupported_field")


def responses(value: dict[str, Any]) -> ChatInput:
    fields(
        value,
        "model input instructions tools tool_choice parallel_tool_calls max_output_tokens temperature stream store reasoning include prompt_cache_key metadata client_metadata",
    )
    if value.get("store", False) is not False or value.get("include", []) not in (
        [],
        ["reasoning.encrypted_content"],
    ):
        raise ValueError("unsupported_storage_or_include")
    for key in ("metadata", "client_metadata"):
        meta = value.get(key, {})
        if isinstance(meta, dict):
            if len(meta) > 32:
                raise LimitError("invalid_metadata", len(meta), 32, "entries")
            for entry in meta.values():
                if isinstance(entry, str) and len(entry) > 1024:
                    raise LimitError("invalid_metadata", len(entry), 1024, "characters")
        if (
            not isinstance(meta, dict)
            or len(meta) > 32
            or any(
                not isinstance(k, str) or not isinstance(v, str) or len(v) > 1024
                for k, v in meta.items()
            )
        ):
            raise ValueError("invalid_metadata")
    if not isinstance(value.get("prompt_cache_key", ""), str):
        raise ValueError("invalid_cache_hint")
    reasoning = value.get("reasoning", {})
    fields(reasoning, "effort summary")
    if reasoning.get("summary", "none") != "none":
        raise ValueError("unsupported_reasoning_summary")
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
                    "content": item["output"],
                }
            )
        else:
            raise ValueError("unsupported_input_type")
    tools = []
    for tool in value.get("tools", []):
        fields(tool, "type name description parameters strict")
        if tool["type"] != "function":
            raise ValueError("unsupported_tool")
        tools.append(
            {
                "type": "function",
                "function": {k: v for k, v in tool.items() if k != "type"},
            }
        )
    choice = value.get("tool_choice", "auto")
    if isinstance(choice, dict):
        fields(choice, "type name")
        if choice["type"] != "function":
            raise ValueError("unsupported_tool_choice")
        choice = {"type": "function", "function": {"name": choice["name"]}}
    return ChatInput.model_validate(
        {
            "model": value["model"],
            "messages": messages,
            "tools": tools,
            "stream": value.get("stream", False),
            "stream_options": {"include_usage": True},
            "max_tokens": value.get("max_output_tokens", 512),
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
        fields(tool, "name description input_schema")
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
