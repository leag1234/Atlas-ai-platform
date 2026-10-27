"""Select document evidence by turn and reference, never by chat-level presence."""

import re
import unicodedata
from pathlib import Path
from typing import Any


_IMAGE_EXTENSIONS = frozenset(
    {
        ".avif",
        ".bmp",
        ".gif",
        ".heic",
        ".jpeg",
        ".jpg",
        ".png",
        ".svg",
        ".tif",
        ".tiff",
        ".webp",
    }
)


def normalized(text: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(c)
    )


def message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def is_document_upload(entry: dict[str, Any]) -> bool:
    """Images are vision inputs even when an upstream UI labels them as files."""
    if entry.get("type") == "image":
        return False
    content_type = str(entry.get("content_type") or entry.get("mime_type") or "")
    if content_type.casefold().startswith("image/"):
        return False
    name = str(entry.get("filename") or entry.get("name") or "")
    return Path(name).suffix.casefold() not in _IMAGE_EXTENSIONS


def referenced_names(question: str, names: list[str], *, immediate: bool) -> set[str]:
    text = normalized(question)
    named = {name for name in names if normalized(name) in text}
    if named:
        return named
    if re.search(
        r"\b(?:this|that|the|attached|uploaded|ce|cet|cette|le|la|du|dies\w*|este|ese|questo)\s+(?:document|pdf|file|proposal|report|fichier|rapport|proposition|dokument)\b|\b(?:page|section)\s+\d+",
        text,
    ):
        return set(names)
    if (
        immediate
        and re.search(r"\b(?:its|their|it|them|son|sa|ses)\b", text)
        and re.search(
            r"\b(?:budget|cost|risks?|timeline|schedule|summary|summari\w*|explain|detail\w*|shorter|translate|resume\w*|cout\w*|risque\w*)\b",
            text,
        )
    ):
        return set(names)
    return set()


def select_documents(
    messages: list[dict[str, Any]], files_key: str = "files"
) -> list[dict[str, Any]]:
    """Input is the active message chain, ending at the current user turn."""
    if not messages:
        return []
    current = [
        entry
        for entry in messages[-1].get(files_key) or []
        if isinstance(entry, dict) and is_document_upload(entry)
    ]
    if current:
        return [dict(f, reused=False) for f in current]
    previous = [m for m in messages[:-1] if m.get("role") == "user"]
    available: dict[str, dict[str, Any]] = {}
    for message in previous:
        for entry in message.get(files_key) or []:
            if not isinstance(entry, dict) or not is_document_upload(entry):
                continue
            available[
                str(entry.get("filename") or entry.get("name") or entry.get("id"))
            ] = entry
    names = referenced_names(
        message_text(messages[-1]),
        list(available),
        immediate=bool(previous and previous[-1].get(files_key)),
    )
    return [
        dict(entry, reused=True) for name, entry in available.items() if name in names
    ]
