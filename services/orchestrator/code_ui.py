"""Narrow owner-only same-origin relay contract for the Code interface."""

import re
import os
import posixpath
from urllib.parse import urlsplit, unquote


def relay_header(header: bytes) -> bytes | None:
    """Preserve streaming bodies; refuse guest, foreign-origin and unknown routes."""
    from services.orchestrator.code_workspace import enabled

    if project_mode() or not enabled():
        return None
    line, _, rest = header.partition(b"\r\n")
    method, target, version = line.split(b" ", 2)
    path = target.removeprefix(b"/atlas-code")
    allowed = (
        method == b"POST"
        and (
            path == b"/v1/chat/completions"
            or re.fullmatch(rb"/code/approvals/[a-f0-9]{32}", path)
        )
    ) or (
        method == b"GET"
        and (
            path == b"/code/ui"
            or re.fullmatch(rb"/code/sessions/[a-f0-9-]{36}/status", path)
        )
    )
    host = re.search(rb"(?im)^host:\s*([^\r\n]+)", rest)
    origin = re.search(rb"(?im)^origin:\s*([^\r\n]+)", rest)
    fetch_site = re.search(rb"(?im)^sec-fetch-site:\s*([^\r\n]+)", rest)
    if not allowed or host is None:
        return None
    if fetch_site and fetch_site.group(1).strip() == b"cross-site":
        return None
    if (
        origin
        and urlsplit(origin.group(1).decode("latin1")).netloc
        != host.group(1).decode("latin1").strip()
    ):
        return None
    # Origin was checked against the browser-facing host; the adapter is loopback.
    lines = [
        row
        for row in rest.split(b"\r\n")
        if row.split(b":", 1)[0].lower()
        not in (b"origin", b"connection", b"authorization", b"cookie")
    ]
    return (
        b" ".join((method, path, version))
        + b"\r\n"
        + b"\r\n".join([*lines, b"Connection: close"])
    )


def project_mode() -> bool:
    return os.environ.get("ATLAS_PROJECT_MODE") == "1" or bool(
        os.environ.get("ATLAS_PROJECT_DSN")
    )


def legacy_project_route(path: str) -> bool:
    """Normalize like the host router before denying legacy project capabilities."""
    path = path.split("?", 1)[0]
    for _ in range(8):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if unquote(path) != path or "\\" in path:
        return True
    path = "/" + posixpath.normpath(path).lstrip("/")
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in (
            "/projects",
            "/code",
            "/atlas-code",
            "/api/v1/terminals",
            "/api/v1/files",
            "/images",
        )
    )
