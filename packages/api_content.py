"""Model names and attached files on the direct API routes (Responses, Messages, routed chat)."""

import base64
import binascii
from pathlib import PurePath
from collections.abc import Mapping
from typing import Any

from packages.attachment_reader import extract_bytes
from packages.profiles import PROFILES


class FileContentError(ValueError):
    """A client file part that cannot be turned into text."""


def assistant_name(name: object) -> bool:
    """The Atlas assistant and its profiles are served on Chat Completions only."""
    return name == "atlas" or name in PROFILES


def assistant_route_message() -> str:
    return (
        "The Atlas assistant (atlas, "
        + ", ".join(PROFILES)
        + ") is served on /chat/completions. On this route use atlas-dev or atlas-chat."
    )


def inline_files(messages: list[Any]) -> list[Any]:
    """Replace OpenAI file parts by their extracted text; direct models read text and images only."""
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for index, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") != "file":
                continue
            file = part.get("file")
            if not isinstance(file, dict) or not isinstance(file.get("file_data"), str):
                raise FileContentError("file_data_required")
            name = str(file.get("filename") or "attachment.txt")
            data = file["file_data"]
            if data.startswith("data:"):
                data = data.split(",", 1)[-1]
            try:
                raw = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError):
                raise FileContentError("invalid_file_data") from None
            text, _, _ = extract_bytes(raw, PurePath(name).suffix.lower() or ".txt")
            if not text.strip():
                raise FileContentError("unreadable_file")
            content[index] = {"type": "text", "text": f"Attached file {name}:\n{text}"}
    return messages


# Fields that common OpenAI-compatible clients (opencode, AI SDK, LiteLLM) send and that the direct
# models do not use. They are dropped before validation instead of failing the whole request; any
# other unknown field is still refused with its name.
IGNORED_TOP_LEVEL = frozenset(
    {
        "store",
        "metadata",
        "user",
        "safety_identifier",
        "service_tier",
        "verbosity",
        "prompt_cache_key",
        "prompt_cache_retention",
    }
)
# Accepted by the OpenAI API but applied by none of the hosted providers (logit_bias), or
# not assembled into Atlas responses yet (logprobs). Refused by name rather than ignored.
UNSUPPORTED = ("logit_bias", "logprobs", "top_logprobs")


def unsupported_option(body: dict[str, Any]) -> str | None:
    """Name of an option that would be silently lost, if one is set."""
    for name in UNSUPPORTED:
        if body.get(name):
            return name
    return None


IGNORED_MESSAGE = frozenset(
    {
        "reasoning_content",
        "reasoning",
        "reasoning_text",
        "reasoning_details",
        "refusal",
        "annotations",
    }
)


def drop_client_extras(body: dict[str, Any]) -> dict[str, Any]:
    """Remove the known optional client fields above, and cache markers on parts and tools."""
    for name in IGNORED_TOP_LEVEL:
        body.pop(name, None)
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        for name in IGNORED_MESSAGE:
            message.pop(name, None)
        message.pop("cache_control", None)
        if isinstance(message.get("content"), list):
            for part in message["content"]:
                if isinstance(part, dict):
                    part.pop("cache_control", None)
    for tool in body.get("tools") or []:
        if isinstance(tool, dict):
            tool.pop("cache_control", None)
    return body


def messages_protocol(headers: Mapping[str, str]) -> bool:
    """Clients of the Anthropic Messages format send its version header on every request."""
    return "anthropic-version" in headers
