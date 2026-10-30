"""Opaque local image references keep binary data out of model history."""

import base64
import asyncio
import fcntl
import hashlib
import json
import os
from packages.ports import atlas_ports
import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import Response, JSONResponse
from packages.images import image_info
from packages.image_upload import normalize_uploads, UploadError
from typing import Any
from packages.turn_attachments import is_document_upload, select_documents
from services.orchestrator.documents import Attachment
from services.orchestrator.daily_budget import user_from_headers

router = APIRouter()


def directory() -> Path:
    return Path(os.environ.get("ATLAS_IMAGE_DIR", "BRAIN/generated-images"))


def store(url: str, metadata: dict[str, str] | None = None) -> str:
    image_info(url)
    root = directory()
    if root.is_symlink():
        raise ValueError("unsafe_image_directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    key = uuid4().hex
    with (root / (key + ".png")).open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(base64.b64decode(url.split(",", 1)[1], validate=True))
    if metadata:
        path = root / (key + ".json")
        with path.open("x") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(metadata, output)
    return (
        os.environ.get(
            "ATLAS_PUBLIC_URL", atlas_ports().url("adapter", "localhost")
        ).rstrip("/")
        + "/images/"
        + key
    )


def generated_medium(reference_url: str) -> str | None:
    """Return bounded server-written medium metadata for an opaque image URL."""
    match = re.search(r"/images/([a-f0-9]{32})$", reference_url)
    if match is None:
        return None
    path = directory() / (match.group(1) + ".json")
    if path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, OSError, ValueError):
        return None
    medium = value.get("medium") if isinstance(value, dict) else None
    return medium if isinstance(medium, str) and 1 <= len(medium) <= 200 else None


@router.get("/images/{key}")
def owned_image_response(key: str, request: Request) -> Response:
    return generated_image(key, user_from_headers(request.headers))


def generated_image(key: str, owner: str = "unknown") -> Response:
    if not re.fullmatch(r"[a-f0-9]{32}", key):
        return Response(status_code=404)
    path = directory() / (key + ".png")
    try:
        check_owner(key, owner)
    except (ValueError, OSError):
        return Response(status_code=404)
    if path.is_symlink():
        return Response(status_code=404)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Response(status_code=404)
    return Response(
        raw,
        media_type="image/jpeg" if raw.startswith(b"\xff\xd8") else "image/png",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=86400",
        },
    )


def store_svg(svg: str) -> str:
    """Store validated model SVG separately from user-uploaded raster images."""
    root = directory()
    if root.is_symlink():
        raise ValueError("unsafe_image_directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    key = uuid4().hex
    with (root / (key + ".svg")).open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(svg.encode("utf-8"))
    return (
        os.environ.get(
            "ATLAS_PUBLIC_URL", atlas_ports().url("adapter", "localhost")
        ).rstrip("/")
        + "/images/svg/"
        + key
    )


@router.get("/images/svg/{key}")
def generated_svg(key: str, download: bool = False) -> Response:
    if not re.fullmatch(r"[a-f0-9]{32}", key):
        return Response(status_code=404)
    path = directory() / (key + ".svg")
    if path.is_symlink():
        return Response(status_code=404)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Response(status_code=404)
    if len(raw) > 96_000:
        return Response(status_code=404)
    return Response(
        raw,
        media_type="image/svg+xml",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "attachment; filename=diagram.svg"
            if download
            else "inline",
            "Cache-Control": "private, max-age=86400",
        },
    )


def reference(key: str) -> str:
    return (
        os.environ.get(
            "ATLAS_PUBLIC_URL", atlas_ports().url("adapter", "localhost")
        ).rstrip("/")
        + "/images/"
        + key
    )


def check_owner(key: str, owner: str) -> None:
    path = directory() / (key + ".owner")
    if path.is_symlink():
        raise ValueError("unknown_image_reference")
    if path.exists():
        if path.read_text() != hashlib.sha256(owner.encode()).hexdigest():
            raise ValueError("unknown_image_reference")
    elif owner != "unknown":
        raise ValueError("unknown_image_reference")


def load_uploaded(url: str, owner: str = "unknown") -> str:
    prefix = reference("")
    if not url.startswith(prefix):
        raise ValueError("unknown_image_reference")
    key = url[len(prefix) :]
    if not re.fullmatch(r"[a-f0-9]{32}", key) or directory().is_symlink():
        raise ValueError("unknown_image_reference")
    check_owner(key, owner)
    fd = os.open(directory() / (key + ".png"), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        raw = source.read(2 * 1024 * 1024 + 1)
    mime = "jpeg" if raw.startswith(b"\xff\xd8") else "png"
    data = "data:image/" + mime + ";base64," + base64.b64encode(raw).decode()
    image_info(data)
    return data


def store_uploaded(url: str, owner: str = "unknown") -> str:
    image_info(url)
    root = directory()
    if root.is_symlink():
        raise ValueError("unsafe_image_directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    digest = hashlib.sha256((owner + "\0" + url).encode()).hexdigest()
    lock_fd = os.open(
        root / "upload.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        index_path = root / "upload-index.json"
        if index_path.is_symlink():
            raise ValueError("unsafe_image_index")
        index = json.loads(index_path.read_text()) if index_path.exists() else {}
        if not isinstance(index, dict):
            raise ValueError("invalid_image_index")
        if digest in index:
            candidate = reference(str(index[digest]))
            load_uploaded(candidate, owner)
            return candidate
        url_ref = store(url)
        key = url_ref.rsplit("/", 1)[-1]
        with (root / (key + ".owner")).open("x") as ownership:
            os.fchmod(ownership.fileno(), 0o600)
            ownership.write(hashlib.sha256(owner.encode()).hexdigest())
        index[digest] = key
        temporary = root / ("upload-index-" + uuid4().hex + ".tmp")
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "w") as output:
            json.dump(index, output)
        os.replace(temporary, index_path)
        return url_ref


def prepare_uploads(
    value: Any, owner: str = "unknown"
) -> tuple[Any, list[dict[str, int]], list[str]]:
    if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
        return value, [], []
    # Only file parts create document inputs; ignore untrusted internal fields.
    value["documents"] = []
    messages = value["messages"]
    if not 1 <= len(messages) <= 100:
        return value, [], []
    document_turns = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        documents = (
            [
                Attachment.model_validate(p.get("file")).model_dump()
                for p in content
                if isinstance(p, dict)
                and p.get("type") == "file"
                and is_document_upload(dict(p.get("file") or {}))
            ]
            if isinstance(content, list) and message.get("role") == "user"
            else []
        )
        document_turns.append({**message, "files": documents})
    value["documents"] = select_documents(document_turns)
    # A reused file delivered on the current turn by the verified inlet retains its status.
    current = document_turns[-1].get("files", []) if document_turns else []
    if current:
        value["documents"] = current
    changes: list[dict[str, int]] = []
    current_refs: list[str] = []
    for index, message in enumerate(messages):
        if (
            not isinstance(message, dict)
            or message.get("role") != "user"
            or not isinstance(message.get("content"), list)
        ):
            continue
        if index < len(messages) - 1:
            for part in message["content"]:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    part.clear()
                    part.update(type="text", text="[Image attached in an earlier turn]")
        # Normalize each turn under the existing per-turn count and byte ceilings.
        _, normalized = normalize_uploads({"messages": [message]})
        changes.extend(normalized)
        for part in message["content"]:
            if isinstance(part, dict) and part.get("type") == "file":
                attachment = Attachment.model_validate(part.get("file"))
                part.clear()
                part.update(
                    type="text", text="[Attachment: " + attachment.filename + "]"
                )
                continue
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            data = part.get("image_url")
            if not isinstance(data, dict) or not isinstance(data.get("url"), str):
                continue
            url = data["url"]
            if url.startswith("data:"):
                url_ref = store_uploaded(url, owner)
            else:
                url_ref = url
                # This reads only owned local references, never arbitrary URLs.
                load_uploaded(url_ref, owner)
            if index == len(messages) - 1:
                current_refs.append(url_ref)
                data["url"] = load_uploaded(url_ref, owner)
            else:
                part.clear()
                part.update(type="text", text="[Image reference: " + url_ref + "]")
    return value, changes, current_refs


@router.post("/images/upload")
async def upload_image(request: Request) -> Response:
    if request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
        return JSONResponse({"error": "origin_rejected"}, status_code=403)
    try:
        body = bytearray()
        async with asyncio.timeout(5):
            async for piece in request.stream():
                body.extend(piece)
                if len(body) > 6 * 1024 * 1024:
                    raise UploadError(
                        "image_upload_size_exceeded",
                        len(body),
                        6 * 1024 * 1024,
                        "bytes",
                    )
        data = json.loads(body)
        if (
            not isinstance(data, dict)
            or set(data) != {"url"}
            or not isinstance(data["url"], str)
            or not data["url"].startswith("data:image/")
        ):
            raise ValueError("invalid_image_upload")
        part = {"type": "image_url", "image_url": {"url": data["url"]}}
        normalize_uploads({"messages": [{"content": [part]}]})
        image_url = part["image_url"]
        assert isinstance(image_url, dict)
        url_ref = await asyncio.to_thread(
            store_uploaded, image_url["url"], user_from_headers(request.headers)
        )
        return JSONResponse({"url": url_ref})
    except UploadError as exc:
        return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=413)
    except (ValueError, OSError):
        return JSONResponse({"error": "invalid_image_upload"}, status_code=400)
    except TimeoutError:
        return JSONResponse(
            {"error": "upload_timeout", "limit_seconds": 5}, status_code=408
        )
