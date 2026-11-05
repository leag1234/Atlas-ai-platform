"""Speech to text of the public API: the transcription provider of the model gateway.

The provider is a preset of providers.yaml that declares a transcription_model; its key comes
from that preset's api_key_env. Audio is sent from memory and never stored or logged, nor is
the transcript. Strict privacy is refused by the edge: no owned transcription server exists yet.
"""

import os
from typing import Any

import aiohttp

import provider_settings

MAX_FILE_BYTES = 25 * 1024 * 1024
# The default provider is the first preset of providers.yaml that declares a
# transcription_model (owner decision 2026-11-05); ATLAS_TRANSCRIPTION_PROVIDER overrides it.
# Its published price is 0.0035 USD per minute of audio (pricing page fetched 2026-11-04),
# charged as a conservative 0.004 EUR per minute so the exchange rate never undercharges.
CONSERVATIVE_MICRO_EUR_PER_MINUTE = 4000
# Reservation before the call, when the duration is unknown: 128 kbit/s, at least one minute.
ESTIMATE_BYTES_PER_SECOND = 16000


class ProviderLimit(Exception):
    """The provider refused for quota or rate (HTTP 429): never reported as an outage."""

    def __init__(self, status: int) -> None:
        super().__init__("provider_limit")
        self.status = status


class AudioRefused(Exception):
    """The provider refused the audio itself (unreadable or unsupported file)."""


class ProviderUnavailable(Exception):
    """The provider could not be reached or did not answer in time."""


def provider() -> str:
    configured = os.environ.get("ATLAS_TRANSCRIPTION_PROVIDER", "").strip()
    if configured:
        return configured
    return next(
        (
            name
            for name, preset in provider_settings.presets().items()
            if preset.get("transcription_model")
        ),
        "",
    )


def model() -> str:
    value = provider_settings.preset_value(provider(), "transcription_model")
    if not isinstance(value, str) or not value:
        raise ValueError("transcription_not_configured")
    return value


def base_url() -> str:
    return provider_settings.validate_endpoint(
        str(provider_settings.preset_value(provider(), "base_url") or "")
    )


def configured() -> bool:
    """True when a transcription provider and its key are configured on this instance."""
    try:
        model()
        return bool(provider_settings.api_key_for(provider()))
    except ValueError:
        return False


def price_micro(seconds: float) -> int:
    """Cost of an audio duration in micro-EUR, rounded up."""
    return max(1, -(-int(seconds * CONSERVATIVE_MICRO_EUR_PER_MINUTE) // 60))


def estimated_micro(size: int) -> int:
    return price_micro(max(60.0, size / ESTIMATE_BYTES_PER_SECOND))


async def transcribe(
    audio: bytes,
    *,
    filename: str,
    content_type: str,
    language: str | None,
    temperature: float | None,
    segments: bool,
    timeout: float,
) -> dict[str, Any]:
    """Transcript, measured duration and cost; segments only when asked."""
    form = aiohttp.FormData()
    form.add_field("model", model())
    form.add_field("file", audio, filename=filename, content_type=content_type)
    if language is not None:
        form.add_field("language", language)
    if temperature is not None:
        form.add_field("temperature", str(temperature))
    if segments:
        form.add_field("timestamp_granularities", "segment")
    try:
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.post(
                base_url() + "/audio/transcriptions",
                data=form,
                headers={
                    "Authorization": "Bearer "
                    + provider_settings.api_key_for(provider())
                },
                allow_redirects=False,
            ) as response:
                if response.status == 429:
                    raise ProviderLimit(response.status)
                if response.status in {400, 413, 415, 422}:
                    raise AudioRefused()
                if response.status != 200:
                    raise RuntimeError("provider_error")
                answer = await response.json(content_type=None)
    except (OSError, TimeoutError, aiohttp.ClientConnectionError) as exc:
        raise ProviderUnavailable() from exc
    text = answer.get("text") if isinstance(answer, dict) else None
    if not isinstance(text, str):
        raise RuntimeError("provider_response_invalid")
    usage = answer.get("usage") or {}
    seconds = usage.get("prompt_audio_seconds") if isinstance(usage, dict) else None
    duration = (
        float(seconds)
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
        else None
    )
    detected = answer.get("language")
    result: dict[str, Any] = {
        "text": text,
        "language": detected if isinstance(detected, str) else None,
        "duration": duration,
        "cost_micro": None if duration is None else price_micro(duration),
    }
    if segments:
        rows = answer.get("segments")
        if not isinstance(rows, list):
            raise RuntimeError("provider_response_invalid")
        result["segments"] = [
            {
                "id": index,
                "start": float(row["start"]),
                "end": float(row["end"]),
                "text": str(row["text"]),
            }
            for index, row in enumerate(rows)
            if isinstance(row, dict)
            and all(isinstance(row.get(key), (int, float)) for key in ("start", "end"))
            and isinstance(row.get("text"), str)
        ]
    return result
