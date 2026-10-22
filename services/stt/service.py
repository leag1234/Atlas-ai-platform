"""OpenAI-compatible offline transcription backed by cached faster-whisper weights."""

from __future__ import annotations

from collections.abc import Iterable
import importlib
import io
import json
import os
from pathlib import Path
import threading
from typing import Any, Protocol, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

CONFIG = Path(__file__).with_name("config.json")
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_AUDIO_SECONDS = 120.0
ALLOWED_CONTENT_TYPES = {
    "audio/webm",
    "audio/ogg",
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp4",
    "video/webm",
    "video/mp4",
}

app = FastAPI(title="ATLAS STT", docs_url=None, redoc_url=None)


class Transcriber(Protocol):
    def transcribe(
        self, audio: io.BytesIO, language: str | None, prompt: str | None
    ) -> tuple[str, str]: ...


def _config() -> dict[str, object]:
    try:
        value = json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("stt_config_invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("stt_config_invalid")
    return value


def _model_directory() -> Path:
    root = Path(os.environ.get("ATLAS_STT_MODEL_DIR", "/opt/atlas-src/models/whisper"))
    repository = _config().get("model_repository")
    if not isinstance(repository, str):
        raise RuntimeError("stt_config_invalid")
    snapshots = root / ("models--" + repository.replace("/", "--")) / "snapshots"
    candidates = sorted(path for path in snapshots.glob("*") if path.is_dir())
    if len(candidates) != 1:
        raise FileNotFoundError("stt_model_unavailable")
    return candidates[0]


def _allowed_languages() -> set[str]:
    values = _config().get("allowed_languages")
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        raise RuntimeError("stt_config_invalid")
    return set(values)


def _duration(audio: bytes) -> float:
    # PyAV is imported on use: the CI environment does not install the speech stack.
    av: Any = importlib.import_module("av")
    try:
        with av.open(io.BytesIO(audio), mode="r") as container:
            streams = [stream for stream in container.streams if stream.type == "audio"]
            if not streams:
                raise ValueError("stt_not_audio")
            total = 0.0
            for frame in container.decode(streams[0]):
                if not isinstance(frame, av.AudioFrame):
                    continue
                total += float(frame.samples) / float(frame.sample_rate)
                if total > MAX_AUDIO_SECONDS:
                    return total
            return total
    except av.FFmpegError as exc:
        raise ValueError("stt_invalid_audio") from exc
    raise ValueError("stt_invalid_audio")


class FasterWhisperTranscriber:
    """Load one CPU model from the local cache only, and serialize CPU work."""

    def __init__(self) -> None:
        self._model: object | None = None
        self._lock = threading.Lock()

    def transcribe(
        self, audio: io.BytesIO, language: str | None, prompt: str | None
    ) -> tuple[str, str]:
        with self._lock:
            if self._model is None:
                threads = _config().get("cpu_threads")
                if not isinstance(threads, int) or threads < 1:
                    raise RuntimeError("stt_config_invalid")
                module = importlib.import_module("faster_whisper")
                model_class = cast("_WhisperFactory", getattr(module, "WhisperModel"))
                self._model = model_class(
                    str(_model_directory()),
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=threads,
                    num_workers=1,
                    local_files_only=True,
                )
            model = cast("_WhisperModel", self._model)
            segments, info = model.transcribe(
                audio,
                language=language,
                initial_prompt=prompt,
                beam_size=5,
                condition_on_previous_text=False,
            )
            return "".join(segment.text for segment in segments).strip(), str(
                info.language
            )


class _Segment(Protocol):
    text: str


class _Info(Protocol):
    language: str


class _WhisperModel(Protocol):
    def transcribe(
        self, audio: io.BytesIO, **kwargs: object
    ) -> tuple[Iterable[_Segment], _Info]: ...


class _WhisperFactory(Protocol):
    def __call__(
        self,
        model_size_or_path: str,
        *,
        device: str,
        compute_type: str,
        cpu_threads: int,
        num_workers: int,
        local_files_only: bool,
    ) -> _WhisperModel: ...


transcriber: Transcriber = FasterWhisperTranscriber()


def _multipart_parts(
    body: bytes, content_type: str
) -> dict[str, tuple[dict[str, str], bytes]]:
    marker = "boundary="
    if marker not in content_type:
        raise ValueError("stt_invalid_multipart")
    boundary = content_type.split(marker, 1)[1].strip().strip('"').encode()
    parts: dict[str, tuple[dict[str, str], bytes]] = {}
    for raw in body.split(b"--" + boundary)[1:-1]:
        raw = raw.strip(b"\r\n")
        if b"\r\n\r\n" not in raw:
            continue
        headers_raw, value = raw.split(b"\r\n\r\n", 1)
        headers: dict[str, str] = {}
        for line in headers_raw.decode("latin-1").split("\r\n"):
            key, separator, header_value = line.partition(":")
            if separator:
                headers[key.lower()] = header_value.strip()
        disposition = headers.get("content-disposition", "")
        name_marker = 'name="'
        if name_marker not in disposition:
            continue
        name = disposition.split(name_marker, 1)[1].split('"', 1)[0]
        parts[name] = (headers, value[:-2] if value.endswith(b"\r\n") else value)
    return parts


def _text(parts: dict[str, tuple[dict[str, str], bytes]], name: str) -> str | None:
    item = parts.get(name)
    return item[1].decode("utf-8") if item else None


@app.get("/v1/models")
def models() -> dict[str, object]:
    model_id = _config().get("model_id")
    if not isinstance(model_id, str):
        raise RuntimeError("stt_config_invalid")
    return {"data": [{"id": model_id, "object": "model"}], "object": "list"}


@app.post("/v1/audio/transcriptions", response_model=None)
async def transcription(request: Request) -> dict[str, str] | PlainTextResponse:
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        raise HTTPException(status_code=415, detail="stt_multipart_required")
    body = await request.body()
    if len(body) > MAX_AUDIO_BYTES + 16_384:
        raise HTTPException(status_code=413, detail="stt_audio_too_large")
    try:
        parts = _multipart_parts(body, content_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    item = parts.get("file")
    if item is None:
        raise HTTPException(status_code=400, detail="stt_file_required")
    headers, audio = item
    model = _text(parts, "model")
    if model != _config().get("model_id"):
        raise HTTPException(status_code=400, detail="stt_unknown_model")
    media_type = headers.get("content-type", "").lower()
    if media_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="stt_audio_required")
    if not audio:
        raise HTTPException(status_code=400, detail="stt_empty_audio")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="stt_audio_too_large")
    language = _text(parts, "language")
    if language is not None and language not in _allowed_languages():
        raise HTTPException(status_code=400, detail="stt_unsupported_language")
    response_format = _text(parts, "response_format") or "json"
    if response_format not in {"json", "text"}:
        raise HTTPException(status_code=400, detail="stt_unsupported_response_format")
    try:
        duration = _duration(audio)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if duration > MAX_AUDIO_SECONDS:
        raise HTTPException(status_code=413, detail="stt_audio_too_long")
    try:
        text, detected = transcriber.transcribe(
            io.BytesIO(audio), language, _text(parts, "prompt")
        )
    except FileNotFoundError:
        raise HTTPException(status_code=503, detail="stt_model_unavailable") from None
    if language is None and detected not in _allowed_languages():
        raise HTTPException(status_code=400, detail="stt_unsupported_language")
    if response_format == "text":
        return PlainTextResponse(text)
    return {"text": text}
