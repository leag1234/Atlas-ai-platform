"""Realtime speech to text through a provider preset: the cloud dictation engine.

The provider is the first preset of providers.yaml that declares a
realtime_transcription_model; ATLAS_REALTIME_TRANSCRIPTION_PROVIDER overrides it. The key
comes from that preset's api_key_env and never leaves this process. Audio frames are relayed
from memory and never stored or logged, nor is the transcript.

Protocol (the hosted realtime transcription provider, documentation fetched 2026-11-11, see the
preset sources): a WebSocket on /audio/transcriptions/realtime?model=..., a session.update
before the first audio, input_audio.append with base64 PCM, input_audio.end, then
transcription.text.delta (append-only text), turn.* events and transcription.done with
usage.prompt_audio_seconds.
"""

import asyncio
import base64
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aiohttp

import provider_settings

# Published price of the realtime model: 0.008 USD per minute of audio (model page fetched
# 2026-11-11); the EU regional endpoint is billed at 1.1 times list price, so 0.0088 USD.
# Charged as a conservative 0.01 EUR per minute so the exchange rate never undercharges.
CONSERVATIVE_MICRO_EUR_PER_MINUTE = 10000
SAMPLE_RATE = 16000
# Documented values: 80 to 1200 in steps of 80, or 2400.
ALLOWED_DELAYS = frozenset(range(80, 1201, 80)) | {2400}


class ProviderLimit(Exception):
    """The provider refused for quota or rate (HTTP 429): never reported as an outage."""

    def __init__(self, status: int) -> None:
        super().__init__("provider_limit")
        self.status = status


class ProviderUnavailable(Exception):
    """The provider could not be reached, refused the key, or closed the session."""


@dataclass(frozen=True)
class ProviderEvent:
    """One normalised provider event: delta, turn_start, turn_end, done or error."""

    kind: str
    text: str = ""
    time_s: float | None = None
    seconds: float | None = None
    code: str = ""


def provider() -> str:
    configured = os.environ.get("ATLAS_REALTIME_TRANSCRIPTION_PROVIDER", "").strip()
    if configured:
        return configured
    return next(
        (
            name
            for name, preset in provider_settings.presets().items()
            if preset.get("realtime_transcription_model")
        ),
        "",
    )


def model() -> str:
    value = provider_settings.preset_value(provider(), "realtime_transcription_model")
    if not isinstance(value, str) or not value:
        raise ValueError("realtime_transcription_not_configured")
    return value


def socket_url() -> str:
    base = provider_settings.preset_value(
        provider(), "realtime_transcription_base_url"
    ) or provider_settings.preset_value(provider(), "base_url")
    https = provider_settings.validate_endpoint(str(base or ""))
    return "wss://" + https.removeprefix("https://") + "/audio/transcriptions/realtime"


def configured() -> bool:
    """True when a realtime provider and its key are configured on this instance."""
    try:
        model()
        socket_url()
        return bool(provider_settings.api_key_for(provider()))
    except ValueError:
        return False


def price_micro(seconds: float) -> int:
    """Cost of an audio duration in micro-EUR, rounded up."""
    return max(1, -(-int(seconds * CONSERVATIVE_MICRO_EUR_PER_MINUTE) // 60))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def normalise(event: dict[str, Any]) -> ProviderEvent | None:
    """Map one provider message to a ProviderEvent; None for messages Atlas ignores."""
    kind = event.get("type")
    if kind == "transcription.text.delta":
        text = event.get("text")
        return ProviderEvent("delta", text) if isinstance(text, str) else None
    if kind == "turn.start":
        return ProviderEvent("turn_start", time_s=_number(event.get("time_s")))
    if kind == "turn.end":
        return ProviderEvent("turn_end", time_s=_number(event.get("time_s")))
    if kind == "transcription.done":
        usage = event.get("usage")
        seconds = (
            _number(usage.get("prompt_audio_seconds"))
            if isinstance(usage, dict)
            else None
        )
        text = event.get("text")
        return ProviderEvent(
            "done", text if isinstance(text, str) else "", seconds=seconds
        )
    if kind == "error":
        error = event.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        # Only the numeric provider code is kept: provider messages may quote input.
        return ProviderEvent(
            "error", code=str(code) if isinstance(code, (int, str)) else ""
        )
    return None


class ProviderStream:
    """One realtime session with the provider, opened by connect()."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._socket: aiohttp.ClientWebSocketResponse | None = None
        self.sent_bytes = 0

    async def connect(
        self,
        *,
        language: str | None,
        delay_ms: int,
        turn_detection: bool,
        context_bias: list[str] | None = None,
        timeout: float = 10,
    ) -> None:
        if delay_ms not in ALLOWED_DELAYS:
            raise ValueError("invalid_delay")
        session: dict[str, Any] = {
            "audio_format": {"encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE},
            "target_streaming_delay_ms": delay_ms,
        }
        if language:
            session["language"] = language
        if turn_detection:
            # Documented defaults; an empty object applies them.
            session["turn_detection"] = {}
        if context_bias:
            session["context_bias"] = context_bias[:100]
        self._session = aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=None, connect=timeout)
        )
        try:
            self._socket = await self._session.ws_connect(
                socket_url() + "?model=" + model(),
                headers={
                    "Authorization": "Bearer "
                    + provider_settings.api_key_for(provider())
                },
                heartbeat=20,
                max_msg_size=1 << 20,
            )
            await self._socket.send_str(
                json.dumps({"type": "session.update", "session": session})
            )
        except aiohttp.WSServerHandshakeError as exc:
            await self.aclose()
            if exc.status == 429:
                raise ProviderLimit(exc.status) from None
            raise ProviderUnavailable() from None
        except (OSError, TimeoutError, aiohttp.ClientError):
            await self.aclose()
            raise ProviderUnavailable() from None

    async def send(self, pcm: bytes) -> None:
        if self._socket is None or self._socket.closed:
            raise ProviderUnavailable()
        self.sent_bytes += len(pcm)
        await self._socket.send_str(
            json.dumps(
                {"type": "input_audio.append", "audio": base64.b64encode(pcm).decode()}
            )
        )

    async def finish(self) -> None:
        if self._socket is not None and not self._socket.closed:
            await self._socket.send_str(json.dumps({"type": "input_audio.end"}))

    async def events(self) -> AsyncIterator[ProviderEvent]:
        if self._socket is None:
            raise ProviderUnavailable()
        async for message in self._socket:
            if message.type != aiohttp.WSMsgType.TEXT:
                break
            try:
                data = json.loads(message.data)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            event = normalise(data)
            if event is not None:
                yield event
                if event.kind in {"done", "error"}:
                    return
        raise ProviderUnavailable()

    async def aclose(self) -> None:
        if self._socket is not None and not self._socket.closed:
            try:
                await asyncio.wait_for(self._socket.close(), 2)
            except (OSError, TimeoutError, aiohttp.ClientError):
                pass
        if self._session is not None:
            await self._session.close()
        self._socket = None
        self._session = None
