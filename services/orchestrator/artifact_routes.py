"""Owner-checked artefact routes with isolated preview responses."""

from __future__ import annotations

import os
import re
from urllib.parse import quote, urlsplit

import aiohttp
from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from services.orchestrator import artifact_store
from services.orchestrator.artifact_store import ArtifactError
from services.orchestrator.artifact_tokens import issue_token, verify_token
from services.orchestrator.project_ui import verified_subject
from services.retrieval.project_identity import ProjectError


router = APIRouter()
PREVIEW_PREFIX = "/api/atlas/preview/"
ARTIFACT_PREFIX = "/api/atlas/artifacts/"
PREVIEW_PATH = "/api/atlas/preview/{token}/{filename}"
ARTIFACT_ID = r"[0-9a-f]{32}"
VERSION = r"[1-9][0-9]{0,3}"
FILENAME = r"[^/]+"
PREVIEW_CSP = (
    "sandbox allow-scripts; default-src 'none'; "
    "script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline'; "
    "img-src data: blob:; media-src data: blob:; font-src data:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'; "
    "frame-src 'none'; worker-src 'none'; manifest-src 'none'; frame-ancestors "
)
PREVIEW_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), display-capture=(), geolocation=(), "
        "gyroscope=(), hid=(), magnetometer=(), microphone=(), midi=(), "
        "payment=(), publickey-credentials-get=(), serial=(), usb=()"
    ),
    "X-Robots-Tag": "noindex, nofollow",
}
ATLAS_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
}
DOWNLOAD_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox",
    "Cache-Control": "private, no-store",
}
PREVIEW_HEADER_NAMES = frozenset(name.lower().encode() for name in PREVIEW_HEADERS)
DOWNLOAD_HEADER_NAMES = frozenset(name.lower().encode() for name in DOWNLOAD_HEADERS)
STORAGE_SHIM = (
    b"<script>(function(){var f=function(){var s={};"
    b"Object.defineProperty(s,'length',{get:function(){return Object.keys(s).filter(function(k){return k!=='length'&&k!=='getItem'&&k!=='setItem'&&k!=='removeItem'&&k!=='clear'&&k!=='key'}).length}});"
    b"s.getItem=function(k){return Object.prototype.hasOwnProperty.call(s,k)?s[k]:null};"
    b"s.setItem=function(k,v){s[k]=String(v)};s.removeItem=function(k){delete s[k]};"
    b"s.clear=function(){Object.keys(s).forEach(function(k){if(k!=='length'&&k!=='getItem'&&k!=='setItem'&&k!=='removeItem'&&k!=='clear'&&k!=='key')delete s[k]})};"
    b"s.key=function(i){return Object.keys(s).filter(function(k){return k!=='length'&&k!=='getItem'&&k!=='setItem'&&k!=='removeItem'&&k!=='clear'&&k!=='key'})[i]||null};return s};"
    b"for(var n of ['localStorage','sessionStorage']){"
    b"try{window[n].getItem('__atlas')}catch(e){"
    b"Object.defineProperty(window,n,{value:f()})}}})();</script>"
)


def enabled() -> bool:
    return os.environ.get("ATLAS_ARTIFACTS") == "1"


def preview_origin() -> str:
    return os.environ.get("ATLAS_PREVIEW_ORIGIN", "").strip()


def _frame_ancestors() -> str:
    if preview_origin():
        return os.environ.get("ATLAS_PREVIEW_FRAME_ANCESTORS", "").strip()
    return "'self'"


def _host_allowed(request: Request, expected: str) -> bool:
    return request.headers.get("host", "").lower() == urlsplit(expected).netloc.lower()


def _valid_artifact_id(artifact_id: str) -> bool:
    return re.fullmatch(ARTIFACT_ID, artifact_id) is not None


def _valid_version(version: str) -> bool:
    return re.fullmatch(VERSION, version) is not None


def _not_found() -> Response:
    return Response(status_code=404, media_type="text/plain; charset=utf-8")


def _artifact_response(content: bytes, filename: str) -> Response:
    return Response(
        content,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )


def _download_response(content: bytes, filename: str) -> Response:
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", filename)
    return Response(
        content,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                + "filename*=UTF-8''"
                + quote(filename, safe="")
            )
        },
    )


def _insert_storage_shim(content: bytes) -> bytes:
    marker = b"<head>"
    position = content.find(marker)
    if position < 0:
        return content
    start = position + len(marker)
    return content[:start] + STORAGE_SHIM + content[start:]


async def _owner(request: Request) -> str:
    async with aiohttp.ClientSession() as client:
        return await verified_subject(request, client)


@router.api_route(
    "/api/atlas/artifacts/{artifact_id}/versions/{version}/open",
    methods=["GET", "HEAD"],
    response_model=None,
)
async def open_artifact(artifact_id: str, version: str, request: Request) -> Response:
    if (
        not enabled()
        or not _valid_artifact_id(artifact_id)
        or not _valid_version(version)
    ):
        return _not_found()
    version_number = int(version)
    if preview_origin() and _host_allowed(request, preview_origin()):
        return _not_found()
    try:
        subject = await _owner(request)
        artifact = artifact_store.read(artifact_id, subject, version_number)
    except (ProjectError, ArtifactError):
        return _not_found()
    token = issue_token(artifact_id, version_number)
    base = preview_origin() or ""
    return RedirectResponse(
        f"{base}/api/atlas/preview/{token}/{quote(artifact.filename, safe='')}",
        status_code=303,
    )


@router.api_route(
    "/api/atlas/artifacts/{artifact_id}/versions/{version}/download",
    methods=["GET", "HEAD"],
    response_model=None,
)
async def download_artifact(
    artifact_id: str, version: str, request: Request
) -> Response:
    if (
        not enabled()
        or not _valid_artifact_id(artifact_id)
        or not _valid_version(version)
    ):
        return _not_found()
    version_number = int(version)
    if preview_origin() and _host_allowed(request, preview_origin()):
        return _not_found()
    try:
        subject = await _owner(request)
        artifact = artifact_store.read(artifact_id, subject, version_number)
    except (ProjectError, ArtifactError):
        return _not_found()
    return _download_response(artifact.content, artifact.filename)


@router.api_route(
    PREVIEW_PATH,
    methods=["GET", "HEAD"],
    response_model=None,
)
async def preview(token: str, filename: str, request: Request) -> Response:
    if not enabled():
        return _not_found()
    if preview_origin() and not _host_allowed(request, preview_origin()):
        return _not_found()
    fetch_dest = request.headers.get("sec-fetch-dest")
    if fetch_dest is not None and fetch_dest not in {"document", "iframe"}:
        return _not_found()
    if not re.fullmatch(FILENAME, filename) or filename in {".", ".."}:
        return _not_found()
    parts = token.split(".")
    if len(parts) != 4 or not _valid_artifact_id(parts[0]):
        return _not_found()
    artifact_id = parts[0]
    try:
        version = int(parts[1])
    except ValueError:
        return _not_found()
    if not _valid_version(parts[1]) or not verify_token(token, artifact_id, version):
        return _not_found()
    try:
        if preview_origin():
            artifact = artifact_store.read_unowned(artifact_id, version)
        else:
            subject = await _owner(request)
            artifact = artifact_store.read(artifact_id, subject, version)
    except (ProjectError, ArtifactError):
        return _not_found()
    if artifact.filename != filename:
        return _not_found()
    return _artifact_response(_insert_storage_shim(artifact.content), filename)


@router.api_route(
    "/api/atlas/preview/{path:path}",
    methods=["GET", "HEAD"],
    response_model=None,
)
async def preview_get_not_found(path: str) -> Response:
    return _not_found()


@router.api_route(
    "/api/atlas/preview/{path:path}",
    methods=["POST", "PUT", "PATCH", "DELETE"],
    response_model=None,
)
async def preview_not_found(path: str) -> Response:
    return Response(status_code=405, headers={"Allow": "GET, HEAD"})


class ArtifactHeadersMiddleware:
    """Set isolation headers on every artefact response, including framework errors."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith((PREVIEW_PREFIX, ARTIFACT_PREFIX)):
            await self.app(scope, receive, send)
            return
        download = path.startswith(ARTIFACT_PREFIX) and path.endswith("/download")
        ancestors = _frame_ancestors()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for forbidden in (b"set-cookie", b"access-control-allow-origin"):
                    headers = [
                        (name, value)
                        for name, value in headers
                        if name.lower() != forbidden
                    ]
                if path.startswith(PREVIEW_PREFIX):
                    headers = [
                        (name, value)
                        for name, value in headers
                        if name.lower() not in PREVIEW_HEADER_NAMES
                        and name.lower() != b"content-security-policy"
                    ]
                    headers.append(
                        (b"content-security-policy", (PREVIEW_CSP + ancestors).encode())
                    )
                    for name, value in PREVIEW_HEADERS.items():
                        headers.append((name.encode(), value.encode()))
                elif download:
                    headers = [
                        (name, value)
                        for name, value in headers
                        if name.lower() not in DOWNLOAD_HEADER_NAMES
                    ]
                    headers.extend(
                        (name.encode(), value.encode())
                        for name, value in DOWNLOAD_HEADERS.items()
                    )
                else:
                    headers = [
                        (name, value)
                        for name, value in headers
                        if name.lower() not in {name.lower() for name in ATLAS_HEADERS}
                    ]
                    headers.extend(
                        (name.encode(), value.encode())
                        for name, value in ATLAS_HEADERS.items()
                    )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)
