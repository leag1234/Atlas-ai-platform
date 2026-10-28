"""ATLAS image references: installed as a native Open WebUI inlet filter."""

from collections import OrderedDict
from collections.abc import Awaitable, Callable
import asyncio
import base64
from pathlib import Path
import hashlib
import json
from types import SimpleNamespace
from importlib import import_module
import re
import os
import unicodedata
from uuid import UUID
from typing import Any

import aiohttp

def status_language(messages: list[SimpleNamespace], locale: object) -> str:
    """Use ATLAS localization when mounted, with a native Open WebUI fallback."""
    try:
        from packages.language import conversation_language
        from packages.language import status_language as atlas_status_language
    except ModuleNotFoundError as exc:
        if exc.name != "packages":
            raise
        if isinstance(locale, str) and locale in {"fr", "de", "es", "it", "en"}:
            return locale
        return "en"
    if os.environ.get("ATLAS_STABLE") == "1" and messages:
        fallback = (
            locale
            if isinstance(locale, str) and locale in {"fr", "de", "es", "it", "en"}
            else "en"
        )
        return conversation_language(messages, str(fallback))
    return atlas_status_language(messages, locale)


def normalized(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )


def fallback_documents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select current or explicitly referenced document uploads by chat turn."""
    if not messages:
        return []
    current = [
        file
        for file in messages[-1].get("files") or []
        if _is_document_upload(file)
    ]
    if current:
        return [dict(file, reused=False) for file in current]
    previous = [message for message in messages[:-1] if message.get("role") == "user"]
    available: dict[str, dict[str, Any]] = {}
    for message in previous:
        for file in message.get("files") or []:
            if not _is_document_upload(file):
                continue
            name = str(file.get("filename") or file.get("name") or file.get("id"))
            available[name] = file
    content = messages[-1].get("content", "")
    question = (
        content
        if isinstance(content, str)
        else "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    )
    text = normalized(question)
    names = {name for name in available if normalized(name) in text}
    if not names and re.search(
        r"\b(?:this|that|the|attached|uploaded|ce|cet|cette|le|la|du|dies\w*|este|ese|questo)\s+(?:document|pdf|file|proposal|report|fichier|rapport|proposition|dokument)\b|\b(?:page|section)\s+\d+",
        text,
    ):
        names = set(available)
    immediate = bool(previous and previous[-1].get("files"))
    if (
        not names
        and immediate
        and re.search(r"\b(?:its|their|it|them|son|sa|ses)\b", text)
        and re.search(
            r"\b(?:budget|cost|risks?|timeline|schedule|summary|summari\w*|explain|detail\w*|shorter|translate|resume\w*|cout\w*|risque\w*)\b",
            text,
        )
    ):
        names = set(available)
    return [dict(file, reused=True) for name, file in available.items() if name in names]


def _is_document_upload(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("type") == "image":
        return False
    content_type = str(entry.get("content_type") or entry.get("mime_type") or "")
    if content_type.casefold().startswith("image/"):
        return False
    name = str(entry.get("filename") or entry.get("name") or "")
    return Path(name).suffix.casefold() not in {
        ".avif", ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp"
    }


def _is_image_upload(entry: object) -> bool:
    return isinstance(entry, dict) and not _is_document_upload(entry)


def select_documents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use the shared selector when available, without requiring it at load time."""
    try:
        from packages.turn_attachments import select_documents as atlas_select_documents
    except ModuleNotFoundError as exc:
        # A stack may mount only part of the package: fall back in both cases.
        if exc.name not in ("packages", "packages.turn_attachments"):
            raise
        return fallback_documents(messages)
    return atlas_select_documents(messages)


class Filter:
    def __init__(self) -> None:
        self.references: OrderedDict[tuple[str, str], str] = OrderedDict()

    async def upload(self, url: str, owner: str = "unknown") -> str:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15), trust_env=False
        ) as client:
            async with client.post(
                os.environ.get("ATLAS_ADAPTER_URL", "http://127.0.0.1:8020")
                + "/images/upload",
                json={"url": url},
                headers={"X-OpenWebUI-User-Id": owner},
                allow_redirects=False,
            ) as response:
                data = await response.json()
                if response.status != 200:
                    raise ValueError(
                        "Image upload: "
                        + str(data.get("message", data.get("error", "failed")))
                    )
                reference = data.get("url")
                if not isinstance(reference, str) or not re.fullmatch(
                    r"https?://[^/]+/images/[a-f0-9]{32}", reference
                ):
                    raise ValueError("Invalid uploaded image reference")
                return reference

    async def owned_file(
        self, entry: dict[str, Any], user: dict[str, Any] | None
    ) -> tuple[Any, bytes]:
        Files = import_module("open_webui.models.files").Files
        Storage = import_module("open_webui.storage.provider").Storage
        file = await Files.get_file_by_id(entry.get("id"))
        if not file or not user or file.user_id != user.get("id"):
            raise ValueError("Attachment unavailable for this user")
        path = await asyncio.to_thread(Storage.get_file, file.path)

        def read() -> bytes:
            with Path(path).open("rb") as source:
                return source.read(16 * 1024 * 1024 + 1)

        raw = await asyncio.to_thread(read)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError(f"Document bytes: {len(raw)}; limit {16 * 1024 * 1024}")
        return file, raw

    async def inlet(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __metadata__: dict[str, Any] | None = None,
        __event_emitter__: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        # The injected composer sets this opaque browser-local token.  Rejecting
        # malformed values here means an arbitrary extra field never becomes a
        # server cancellation handle.
        token = body.get("cancel_id")
        if token is not None:
            try:
                UUID(str(token))
            except ValueError:
                body.pop("cancel_id", None)
        if body.get("model") not in {"atlas-qwen", "atlas-glm", "atlas-deepseek"}:
            return body
        messages = []
        for message in body.get("messages", []):
            content = message.get("content", "")
            text = (
                content
                if isinstance(content, str)
                else "\n".join(
                    part.get("text", "")
                    for part in content
                    if part.get("type") == "text"
                )
            )
            messages.append(SimpleNamespace(role=message.get("role", ""), text=text))
        language = status_language(messages, body.get("ui_locale"))
        labels = json.loads(Path("prompts/activity-labels.json").read_text())
        labels = labels.get(language, labels["en"])
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": labels["thinking"]
                        + ("" if os.environ.get("ATLAS_STABLE") == "1" else " (0.0s)"),
                        "done": False,
                        "action": "atlas_activity",
                    },
                }
            )
        files = body.get("files") or []
        documents = [file for file in files if _is_document_upload(file)]
        original_documents = documents
        metadata = __metadata__ or {}
        current_images = []
        if metadata.get("chat_id") and metadata.get("user_message_id"):
            Chats = import_module("open_webui.models.chats").Chats
            chat = await Chats.get_chat_by_id_and_user_id(
                metadata["chat_id"], (__user__ or {}).get("id")
            )
            if chat is None:
                raise ValueError("Chat unavailable for this user")
            mapping = chat.chat.get("history", {}).get("messages", {})
            chain = []
            key = metadata["user_message_id"]
            visited = set()
            while key and key not in visited:
                visited.add(key)
                message = mapping.get(key)
                if not isinstance(message, dict):
                    raise ValueError("Attachment turn unavailable")
                chain.append(
                    {
                        **message,
                        "files": [
                            f
                            for f in message.get("files", [])
                        if isinstance(f, dict)
                        ],
                    }
                )
                key = message.get("parentId")
            chain.reverse()
            if not chain or chain[-1].get("role") != "user":
                raise ValueError("Attachment turn unavailable")
            documents = select_documents(chain)
            original = mapping[metadata["user_message_id"]]
            current_images = [
                f
                for f in original.get("files", [])
                if _is_image_upload(f)
            ]
        else:
            current_images = [file for file in files if _is_image_upload(file)]
        if current_images:
            last = body["messages"][-1]
            content = last.get("content", "")
            last["content"] = (
                [{"type": "text", "text": content}]
                if isinstance(content, str)
                else [p for p in content if p.get("type") != "image_url"]
            )
            for entry in current_images:
                file, raw = await self.owned_file(entry, __user__)
                mime = str(file.meta.get("content_type", "image/png"))
                if not mime.startswith("image/"):
                    raise ValueError("Image attachment has invalid content type")
                last["content"].append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:"
                            + mime
                            + ";base64,"
                            + base64.b64encode(raw).decode()
                        },
                    }
                )

        if len(documents) > 8:
            raise ValueError(f"Attached files: {len(documents)}; limit 8")
        if documents:
            last = body["messages"][-1]
            if isinstance(last["content"], str):
                last["content"] = [{"type": "text", "text": last["content"]}]
            for entry in documents:
                file, raw = await self.owned_file(entry, __user__)
                last["content"].append(
                    {
                        "type": "file",
                        "file": {
                            "filename": file.meta.get("name", file.filename),
                            "file_data": base64.b64encode(raw).decode(),
                            "file_id": entry["id"],
                            **({"reused": True} if entry.get("reused") else {}),
                        },
                    }
                )
            # Consume originals before Open WebUI's fragment retrieval handler.
            body["files"] = [file for file in files if file not in original_documents]
        body["files"] = [
            file
            for file in files
            if file not in original_documents and file not in current_images
        ]
        owner = str((__user__ or {}).get("id") or "unknown")
        for index, message in enumerate(body.get("messages", [])):
            if message.get("role") != "user" or not isinstance(
                message.get("content"), list
            ):
                continue
            for part in message["content"]:
                data = part.get("image_url") if isinstance(part, dict) else None
                if not isinstance(data, dict) or not isinstance(data.get("url"), str):
                    continue
                if index < len(body["messages"]) - 1:
                    part.clear()
                    part.update(type="text", text="[Image attached in an earlier turn]")
                    continue
                url = data["url"]
                if not url.startswith("data:image/"):
                    continue
                digest = (owner, hashlib.sha256(url.encode()).hexdigest())
                if digest not in self.references:
                    if __event_emitter__:
                        await __event_emitter__(
                            {
                                "type": "status",
                                "data": {
                                    "description": labels["thinking"],
                                    "done": False,
                                },
                            }
                        )
                    self.references[digest] = await self.upload(url, owner)
                    while len(self.references) > 256:
                        self.references.popitem(last=False)
                else:
                    self.references.move_to_end(digest)
                data["url"] = self.references[digest]
        return body
