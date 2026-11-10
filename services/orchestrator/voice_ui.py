"""Browser voice controls: dictation and read-aloud through the local speech services.

The chat UI and the project composer call these same-origin routes; the routes
forward to the Atlas STT/TTS services on the contained network. Each capability
is enabled independently by its own switch (ATLAS_STT / ATLAS_TTS).
"""

import os
from pathlib import Path

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from packages.ports import atlas_ports
from services.orchestrator.project_ui import browser_origin, public_error
from services.orchestrator.serving import stt_enabled
from services.orchestrator.user_memory_ui import browser_user
from services.retrieval.project_identity import ProjectError

router = APIRouter()

MAX_BODY_BYTES = 26 * 1024 * 1024


def tts_enabled() -> bool:
    """Read-aloud is opt-in like the STT switch, on its own variable."""
    return os.environ.get("ATLAS_TTS") == "1"


def _service_url(service: str) -> str:
    return atlas_ports().url(service, "127.0.0.1")


@router.get("/api/atlas/voice/client.js")
def asset() -> Response:
    return Response(
        Path(__file__).with_name("voice.js").read_text(),
        media_type="text/javascript",
    )


@router.get("/api/atlas/voice/config")
def config(request: Request) -> JSONResponse:
    """Which voice capabilities this installation exposes, each independently."""
    try:
        browser_origin(request)
    except ProjectError as exc:
        return public_error(exc)
    return JSONResponse(
        {"stt": stt_enabled(), "tts": tts_enabled()},
        headers={"Cache-Control": "private, no-store"},
    )


async def _forward(request: Request, service: str, path: str) -> Response:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return public_error(ProjectError(413, "body_limit"))
    headers = {"Content-Type": request.headers.get("content-type", "application/json")}
    try:
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=120)
        ) as client:
            async with client.post(
                _service_url(service) + path, data=bytes(body), headers=headers
            ) as reply:
                payload = await reply.read()
                return Response(
                    payload,
                    status_code=reply.status,
                    media_type=reply.headers.get("content-type", "application/json"),
                    headers={"Cache-Control": "private, no-store"},
                )
    except (aiohttp.ClientError, TimeoutError):
        return public_error(ProjectError(503, "speech_unavailable"))


@router.post("/api/atlas/voice/transcriptions")
async def transcriptions(request: Request) -> Response:
    """Dictation: multipart audio to the local STT service."""
    try:
        # Signed-in users only: the speech services are not a public endpoint.
        await browser_user(request)
        if not stt_enabled():
            return public_error(ProjectError(503, "stt_unavailable"))
        return await _forward(request, "stt", "/v1/audio/transcriptions")
    except ProjectError as exc:
        return public_error(exc)


@router.post("/api/atlas/voice/speech")
async def speech(request: Request) -> Response:
    """Read-aloud: JSON text to the local TTS service."""
    try:
        # Signed-in users only: the speech services are not a public endpoint.
        await browser_user(request)
        if not tts_enabled():
            return public_error(ProjectError(503, "tts_unavailable"))
        return await _forward(request, "tts", "/v1/audio/speech")
    except ProjectError as exc:
        return public_error(exc)
