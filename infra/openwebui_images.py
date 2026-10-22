"""ATLAS image references: installed as a native Open WebUI inlet filter."""

from collections import OrderedDict
from collections.abc import Awaitable, Callable
import asyncio
import base64
from pathlib import Path
import hashlib
from importlib import import_module
import re
import os
from typing import Any

import aiohttp


class Filter:
    def __init__(self) -> None:
        self.references: OrderedDict[str, str] = OrderedDict()

    async def upload(self, url: str) -> str:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15), trust_env=False
        ) as client:
            async with client.post(
                os.environ.get("ATLAS_ADAPTER_URL", "http://127.0.0.1:8020")
                + "/images/upload",
                json={"url": url},
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

    async def inlet(
        self,
        body: dict[str, Any],
        __user__: dict[str, Any] | None = None,
        __event_emitter__: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        if body.get("model") not in {"atlas-qwen", "atlas-glm", "atlas-deepseek"}:
            return body
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": "Réflexion… (0.0s)",
                        "done": False,
                        "action": "atlas_activity",
                    },
                }
            )
        files = body.get("files") or []
        documents = [file for file in files if file.get("type") == "file"]
        if len(documents) > 8:
            raise ValueError(f"Attached files: {len(documents)}; limit 8")
        if documents:
            # These imports execute inside Open WebUI, whose verified user and
            # database own file access; no client URL or filesystem path is trusted.
            Files = import_module("open_webui.models.files").Files
            Storage = import_module("open_webui.storage.provider").Storage

            last = body["messages"][-1]
            if isinstance(last["content"], str):
                last["content"] = [{"type": "text", "text": last["content"]}]
            for entry in documents:
                file = await Files.get_file_by_id(entry.get("id"))
                if not file or not __user__ or file.user_id != __user__.get("id"):
                    raise ValueError("Attachment unavailable for this user")
                path = await asyncio.to_thread(Storage.get_file, file.path)

                def read() -> bytes:
                    with Path(path).open("rb") as source:
                        return source.read(16 * 1024 * 1024 + 1)

                raw = await asyncio.to_thread(read)
                if len(raw) > 16 * 1024 * 1024:
                    raise ValueError(
                        f"Document bytes: {len(raw)}; limit {16 * 1024 * 1024}"
                    )
                last["content"].append(
                    {
                        "type": "file",
                        "file": {
                            "filename": file.meta.get("name", file.filename),
                            "file_data": base64.b64encode(raw).decode(),
                            "file_id": entry["id"],
                        },
                    }
                )
            # Consume originals before Open WebUI's fragment retrieval handler.
            body["files"] = [file for file in files if file not in documents]
        for message in body.get("messages", []):
            if message.get("role") != "user" or not isinstance(
                message.get("content"), list
            ):
                continue
            for part in message["content"]:
                data = part.get("image_url") if isinstance(part, dict) else None
                if not isinstance(data, dict) or not isinstance(data.get("url"), str):
                    continue
                url = data["url"]
                if not url.startswith("data:image/"):
                    continue
                digest = hashlib.sha256(url.encode()).hexdigest()
                if digest not in self.references:
                    if __event_emitter__:
                        await __event_emitter__(
                            {
                                "type": "status",
                                "data": {
                                    "description": "Preparing image references…",
                                    "done": False,
                                },
                            }
                        )
                    self.references[digest] = await self.upload(url)
                    while len(self.references) > 256:
                        self.references.popitem(last=False)
                else:
                    self.references.move_to_end(digest)
                data["url"] = self.references[digest]
        return body
