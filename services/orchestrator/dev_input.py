"""Text/function input translations at the stateless Responses boundary."""

from packages.limits import LimitError

import json
import logging
from typing import Any
from services.orchestrator.dev_chat import ChatInput


class SemanticFieldError(ValueError):
    """A client option whose semantics cannot be preserved by this route."""


def fields(value: dict[str, Any], allowed: str) -> None:
    unknown = set(value) - set(allowed.split())
    if unknown:
        # Field names are client keys, not values: naming them says what to remove.
        raise SemanticFieldError(
            "unsupported field: " + ", ".join(sorted(unknown))[:200]
        )


STORAGE_MESSAGE = (
    "Atlas does not store responses or conversations. Send the full conversation "
    "history in input instead of previous_response_id or conversation."
)


class StorageRefusal(SemanticFieldError):
    """A feature that needs responses stored on the server (owner decision 2026-11-02)."""

    def __init__(self) -> None:
        super().__init__(STORAGE_MESSAGE)


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


SHELL_PARAMETERS = {
    "type": "object",
    "properties": {
        "commands": {"type": "array", "items": {"type": "string"}},
        "timeout_ms": {"type": "integer"},
        "max_output_length": {"type": "integer"},
    },
    "required": ["commands"],
}
APPLY_PATCH_PARAMETERS = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["create_file", "update_file", "delete_file"],
                },
                "path": {"type": "string"},
                "diff": {
                    "type": "string",
                    "description": (
                        "V4A diff without *** Begin Patch or *** End Patch lines. "
                        "create_file: every line of the new file prefixed with +. "
                        "update_file: @@ hunks of context lines (space), removed lines (-) "
                        "and added lines (+). Omitted for delete_file."
                    ),
                },
            },
            "required": ["type", "path"],
        }
    },
    "required": ["operation"],
}
CLIENT_TOOLS = {
    "shell": (
        "Run shell commands on the client and return their output.",
        SHELL_PARAMETERS,
    ),
    "apply_patch": (
        "Create, update or delete a file on the client with a diff.",
        APPLY_PATCH_PARAMETERS,
    ),
}

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


def responses_body(value: dict[str, Any]) -> dict[str, Any]:
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
            "top_p",
            "stream",
            "store",
            "reasoning",
            "include",
            "prompt_cache_key",
            "metadata",
            "client_metadata",
            "text",
            "previous_response_id",
            "conversation",
            "background",
        },
        "request",
    )
    # Nothing is stored: chaining by id, conversations and background runs need storage.
    # store: true is accepted and the response reports store: false.
    if (
        "previous_response_id" in value
        or "conversation" in value
        or value.get("background")
    ):
        raise StorageRefusal()
    if "project_id" in value:
        raise SemanticFieldError("unsupported field: project_id")
    include = value.get("include", [])
    if not isinstance(include, list) or not all(isinstance(i, str) for i in include):
        raise ValueError("invalid_include")
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
                parts: list[dict[str, Any]] = []
                for part in content:
                    if part.get("type") == "input_image":
                        fields(part, "type image_url detail file_id")
                        if not isinstance(part.get("image_url"), str):
                            # file_id needs the Files API, not available yet.
                            raise ValueError("unsupported_content")
                        image: dict[str, Any] = {"url": part["image_url"]}
                        if part.get("detail") in ("low", "high", "auto"):
                            image["detail"] = part["detail"]
                        parts.append({"type": "image_url", "image_url": image})
                        continue
                    if part.get("type") == "input_file":
                        fields(part, "type filename file_data file_url file_id")
                        if not isinstance(part.get("file_data"), str):
                            raise ValueError("unsupported_content")
                        parts.append(
                            {
                                "type": "file",
                                "file": {
                                    "filename": str(
                                        part.get("filename") or "attachment"
                                    ),
                                    "file_data": part["file_data"],
                                },
                            }
                        )
                        continue
                    fields(part, "type text annotations logprobs")
                    if (
                        part["type"] not in ("input_text", "output_text")
                        or not isinstance(part["text"], str)
                        or part.get("annotations")
                        or part.get("logprobs")
                    ):
                        raise ValueError("unsupported_content")
                    parts.append({"type": "text", "text": part["text"]})
                content = joined(parts)
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
        elif kind in ("shell_call", "apply_patch_call"):
            response_fields(
                item,
                {
                    "type",
                    "id",
                    "call_id",
                    "action",
                    "operation",
                    "status",
                    "environment",
                    "caller",
                    "created_by",
                },
                kind,
            )
            call_id = item.get("call_id")
            payload = (
                {
                    "commands": (item.get("action") or {}).get("commands", []),
                    **{
                        k: v
                        for k, v in (item.get("action") or {}).items()
                        if k in ("timeout_ms", "max_output_length") and v is not None
                    },
                }
                if kind == "shell_call"
                else {"operation": item.get("operation")}
            )
            if not isinstance(call_id, str):
                raise ValueError("invalid_tool_call")
            call = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "shell" if kind == "shell_call" else "apply_patch",
                    "arguments": json.dumps(payload),
                },
            }
            if messages and messages[-1]["role"] == "assistant":
                messages[-1].setdefault("tool_calls", []).append(call)
            else:
                messages.append({"role": "assistant", "tool_calls": [call]})
        elif kind in ("shell_call_output", "apply_patch_call_output"):
            response_fields(
                item,
                {
                    "type",
                    "id",
                    "call_id",
                    "output",
                    "status",
                    "max_output_length",
                    "caller",
                    "created_by",
                },
                kind,
            )
            if not isinstance(item.get("call_id"), str):
                raise ValueError("invalid_tool_output")
            result = (
                item.get("output")
                if kind == "shell_call_output"
                else {"status": item.get("status"), "output": item.get("output")}
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item["call_id"],
                    "content": json.dumps(result, ensure_ascii=False),
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
    hosted: list[str] = []
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
        elif kind in CLIENT_TOOLS:
            response_fields(
                tool, {"type", "environment", "allowed_callers"}, kind + "_tool"
            )
            description, parameters = CLIENT_TOOLS[kind]
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": kind,
                        "description": description,
                        "parameters": parameters,
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
        elif str(kind).startswith(("web_search", "web_search_preview")):
            # Hosted tool: Atlas runs the search between model turns (hosted_tools).
            from services.orchestrator.hosted_tools import SEARCH_FUNCTION

            if "web_search" not in hosted:
                hosted.append("web_search")
                tools.append(SEARCH_FUNCTION)
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
    return {
        "response_format": fmt,
        "model": value["model"],
        "messages": messages,
        "tools": tools,
        "stream": value.get("stream", False),
        "stream_options": {"include_usage": True},
        "max_tokens": value.get("max_output_tokens") or 4096,
        "temperature": value.get("temperature", 0),
        **({"top_p": value["top_p"]} if value.get("top_p") is not None else {}),
        "parallel_tool_calls": value.get("parallel_tool_calls", True),
        "tool_choice": choice,
        "reasoning_effort": reasoning.get("effort", "none"),
        **({"_hosted": hosted} if hosted else {}),
    }


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


def messages_body(value: dict[str, Any]) -> dict[str, Any]:
    fields(
        value,
        "model messages system max_tokens tools tool_choice stream temperature metadata thinking output_config"
        " stop_sequences top_p top_k cache_control service_tier",
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
        texts: list[dict[str, Any]] = []
        calls = []
        for block in content:
            kind = block["type"]
            if kind == "text":
                texts.append({"type": "text", "text": text_blocks([block])})
            elif kind == "server_tool_use" and role == "assistant":
                continue
            elif kind == "web_search_tool_result" and role == "assistant":
                rows = (
                    block.get("content")
                    if isinstance(block.get("content"), list)
                    else []
                )
                texts.append(
                    {
                        "type": "text",
                        "text": "Web search results: "
                        + "; ".join(
                            f"{row.get('title', '')} ({row.get('url', '')})"
                            for row in rows
                            if isinstance(row, dict)
                        ),
                    }
                )
            elif kind in ("thinking", "redacted_thinking") and role == "assistant":
                # Reasoning of an earlier turn: the routed models cannot use another
                # model's signed reasoning, and it is not part of the answer.
                continue
            elif kind == "image" and role == "user":
                fields(block, "type source cache_control")
                source = block["source"]
                if source.get("type") == "base64":
                    url = f"data:{source['media_type']};base64,{source['data']}"
                elif source.get("type") == "url" and isinstance(source.get("url"), str):
                    url = source["url"]
                else:
                    raise ValueError("unsupported_content")
                texts.append({"type": "image_url", "image_url": {"url": url}})
            elif kind == "document" and role == "user":
                fields(block, "type source title context citations cache_control")
                source, title = block["source"], str(block.get("title") or "document")
                if source.get("type") == "text":
                    texts.append(
                        {"type": "text", "text": f"Document {title}:\n{source['data']}"}
                    )
                elif source.get("type") == "base64":
                    suffix = (
                        ".pdf"
                        if source.get("media_type") == "application/pdf"
                        else ".txt"
                    )
                    name = title if "." in title else title + suffix
                    texts.append(
                        {
                            "type": "file",
                            "file": {"filename": name, "file_data": source["data"]},
                        }
                    )
                elif source.get("type") == "content":
                    texts.append(
                        {
                            "type": "text",
                            "text": f"Document {title}:\n"
                            + text_blocks(source["content"]),
                        }
                    )
                else:
                    raise ValueError("unsupported_content")
            elif kind == "search_result" and role == "user":
                fields(block, "type source title content citations cache_control")
                texts.append(
                    {
                        "type": "text",
                        "text": f"Search result {block.get('title', '')} ({block.get('source', '')}):\n"
                        + text_blocks(block.get("content", [])),
                    }
                )
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
                {
                    "role": role,
                    "content": joined(texts) if texts else "",
                    "tool_calls": calls,
                }
            )
    tools = []
    hosted: list[str] = []
    for tool in value.get("tools", []):
        if str(tool.get("type", "")).startswith("web_search_"):
            # Server tool: Atlas runs the search between model turns (hosted_tools).
            from services.orchestrator.hosted_tools import SEARCH_FUNCTION

            fields(
                tool,
                "type name max_uses allowed_domains blocked_domains user_location cache_control",
            )
            if "web_search" not in hosted:
                hosted.append("web_search")
                tools.append(SEARCH_FUNCTION)
            continue
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
    return {
        "model": value["model"],
        "messages": converted,
        "tools": tools,
        "max_tokens": value["max_tokens"],
        "stream": value.get("stream", False),
        "stream_options": {"include_usage": True},
        "tool_choice": selected,
        "parallel_tool_calls": not disabled,
        "temperature": value.get("temperature", 0),
        **{
            target: value[source]
            for source, target in (
                ("stop_sequences", "stop"),
                ("top_p", "top_p"),
                ("top_k", "top_k"),
            )
            if value.get(source) not in (None, [])
        },
        **({"_hosted": hosted} if hosted else {}),
    }


def responses(value: dict[str, Any]) -> ChatInput:
    body = responses_body(value)
    # Hosted tools run only on the routed key route; elsewhere they stay dropped.
    if body.pop("_hosted", None):
        body["tools"] = [
            tool for tool in body["tools"] if tool["function"]["name"] != "web_search"
        ]
    return ChatInput.model_validate(body)


def messages(value: dict[str, Any]) -> ChatInput:
    body = messages_body(value)
    # Hosted tools run only on the routed key route; elsewhere they stay unavailable.
    if body.pop("_hosted", None):
        body["tools"] = [
            tool for tool in body["tools"] if tool["function"]["name"] != "web_search"
        ]
    return ChatInput.model_validate(body)


def text_only(body: dict[str, Any]) -> dict[str, Any]:
    """The body with media parts as text, for the checks of the text-only schema."""
    copy = dict(body, messages=[dict(m) for m in body["messages"]])
    copy.pop("_hosted", None)
    for message in copy["messages"]:
        if isinstance(message.get("content"), list):
            message["content"] = "\n".join(
                str(part.get("text", "[" + str(part.get("type")) + "]"))
                for part in message["content"]
                if isinstance(part, dict)
            )
    return copy


def joined(parts: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """Plain text when every part is text, as before; content parts when media is present."""
    if all(part["type"] == "text" for part in parts):
        return "\n".join(part["text"] for part in parts)
    return parts
