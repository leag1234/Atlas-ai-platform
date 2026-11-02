"""Model names and attached files on the direct API routes (Responses, Messages, routed chat)."""

import base64
import binascii
from pathlib import PurePath
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
