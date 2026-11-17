"""Real-time dictation: a WebSocket relay from the browser microphone to a streaming engine.

Behind ATLAS_REALTIME_DICTATION=1 (off by default). The chat's voice button (chat-ui.js)
and a standalone test page use it. The browser sends 16 kHz mono PCM frames; Atlas relays
them to the selected engine and sends partial and final text back:

- ``local``: the owned CPU engine (services/stt/streaming.py), allowed at every level.
  It is the default; at most ATLAS_DICTATION_LOCAL_MAX_SESSIONS streams run at once.
- ``cloud``: the gateway's realtime transcription provider (realtime_transcription.py).
  The provider key stays on the server. It takes over when the local engine is busy or
  missing, only at the Standard privacy level: in Strict and Enhanced no audio leaves
  Atlas (audio cannot be pseudonymised; owner decision 2026-11-14).

The privacy level is decided here, not by the browser: the highest of the organisation
minimum, the level the user sees for the conversation (``chat``; a new conversation has no
id yet and shows the pending choice, see conversation_privacy.effective_level) and, for a
session started from a project (``scope`` names its organization and project), the
project's setting, read with the signed-in user's membership. Whatever cannot be read
counts as strict. The browser sends the level its selector shows (``privacy``): it may only
raise the level, never lift it.

Client messages: one JSON ``start`` ({"type": "start", "engine" ("auto" by default),
"dictation_language", "scope", "chat", "privacy", "cleanup"}), then binary PCM s16le frames,
``commit`` messages, then {"type": "stop"}. Server messages: ``ready`` (engine, and
``fallback`` when the cloud engine replaced a busy local one), ``turn``, ``partial`` (text
of the open utterance, replaced each time), ``final`` (a closed segment, by index),
``cleaned`` (a model-cleaned view of segments, see dictation_cleanup.py), ``transcript``
(the text to write for one piece, from the final pass), ``done`` (seconds, cost and the
final pass outcome) and ``error`` (a public code; a provider quota or rate refusal is
``provider_limit``, never an outage).

Pieces ("Insert when I pause"): the browser sends ``commit`` at the end of a turn. The
recording up to that turn's end is one piece: its final pass runs while the user goes on
speaking, and its ``transcript`` (``piece``, ``to``: the live segments it covers) is written
once into the composer; the session goes on until the user stops. ``stop`` closes the last
piece (``last``: true). Pieces reach the browser in order, each written once, never changed.

Language: ``dictation_language`` is a language the user chose for dictation. Without one the
engines detect the spoken language. The interface language is never used: an English
interface pinned French speech to English, and the provider then returned nothing (dictation
pilot, 2026-11-17).

Final pass (owner decision 2026-11-17): the live text is only shown in the voice panel. At
stop (or at each committed piece), the recording, kept in memory for the session only, is
transcribed once more and that text is written once into the composer. The batch pass
starts as soon as the user stops, while the live engine drains, and the model clean-up of
the live text is skipped at stop when a final pass replaces it (stop-to-text time, staging
2026-11-17: the drain, the last clean-up and the batch call ran one after the other). At the Standard level the gateway's batch
transcription provider does it (pilot word error rate: French 4.0 %, French with thinking
pauses 5.3 %, English 7.0 %, against 5.4 %, 35.6 % and 12.1 % live); at the Strict and
Enhanced levels the owned engine decodes the recording in long windows, so no audio leaves
Atlas. A failed or late final pass is logged and reported as such in ``transcript``; the
browser then writes the live text.

Limits: session length, idle time, frame size, audio pace, one session per user, a
global session cap and a local engine cap. Cost is reserved in the daily ledger at start
and settled at the end. Audio and text are never stored or logged: each session leaves one
``dictation_session`` line in the service journal (level, engines, durations, outcome,
cost), written at WARNING like the other operational lines, so that it reaches the journal
without a logging configuration.
"""

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import io
import wave

import aiohttp
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

from services.orchestrator.daily_budget import BudgetRefusal, DailyLedger
from services.orchestrator.dictation_cleanup import Cleaner
from services.orchestrator.dictation_cleanup import enabled as cleanup_enabled
from services.orchestrator.dictation_text import guess_language, remove_hesitations
from services.orchestrator.project_ui import public_error
from services.orchestrator.user_memory_ui import browser_user, token_user
from services.retrieval.project_identity import ProjectError

router = APIRouter()
log = logging.getLogger("atlas.dictation")
HERE = Path(__file__).parent
# The local engine detects the language itself among the 25 of its model card
# (https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3, read 2026-11-11).
LOCAL_LANGUAGES = frozenset(
    "bg cs da de el en es et fi fr hr hu it lt lv mt nl pl pt ro ru sk sl sv uk".split()
)
# The cloud engine announces 45 languages without listing them; the language is pinned only
# for those measured with it, the others are left to its own detection.
CLOUD_LANGUAGES_DEFAULT = "fr,en,de,es,it,nl"


def cloud_languages() -> frozenset[str]:
    raw = os.environ.get("ATLAS_DICTATION_CLOUD_LANGUAGES", CLOUD_LANGUAGES_DEFAULT)
    return frozenset(code.strip() for code in raw.split(",") if code.strip())


def spoken_language(value: object) -> str | None:
    """An ISO 639-1 code the user chose for dictation, else None (detected)."""
    if isinstance(value, str):
        code = value.strip().lower().split("-")[0]
        if len(code) == 2 and code.isascii() and code.isalpha():
            return code
    return None


BYTES_PER_SECOND = 32000


def enabled() -> bool:
    return os.environ.get("ATLAS_REALTIME_DICTATION") == "1"


def _number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def engines() -> list[str]:
    raw = os.environ.get("ATLAS_DICTATION_ENGINES", "local,cloud")
    return [
        name.strip() for name in raw.split(",") if name.strip() in {"cloud", "local"}
    ]


@dataclass(frozen=True)
class Limits:
    max_seconds: float
    idle_seconds: float
    max_frame: int
    max_sessions: int
    delay_ms: int
    max_local: int = 2

    @classmethod
    def current(cls) -> "Limits":
        return cls(
            _number("ATLAS_DICTATION_MAX_SECONDS", 300),
            _number("ATLAS_DICTATION_IDLE_SECONDS", 20),
            int(_number("ATLAS_DICTATION_MAX_FRAME_BYTES", 16000)),
            int(_number("ATLAS_DICTATION_MAX_SESSIONS", 4)),
            # 1200 ms, not 480: fewer invented sentence starts; French pilot word error
            # rate 8.5 % to 6.1 % for 0.26 s more to the final text (2026-11-17).
            int(_number("ATLAS_DICTATION_DELAY_MS", 1200)),
            # About one CPU core per local stream (measured 2026-11-11 on the 4-core VM).
            int(_number("ATLAS_DICTATION_LOCAL_MAX_SESSIONS", 2)),
        )


class Stream(Protocol):
    @property
    def sent_bytes(self) -> int: ...

    async def send(self, pcm: bytes) -> None: ...
    async def finish(self) -> None: ...
    def events(self) -> AsyncIterator[Any]: ...
    async def aclose(self) -> None: ...


class Refusal(Exception):
    def __init__(self, code: str, message: str, **extra: object) -> None:
        super().__init__(code)
        self.body: dict[str, object] = {
            "type": "error",
            "code": code,
            "message": message,
        } | extra


def cloud_refusal(privacy: str) -> Refusal:
    """Cloud dictation at a level that keeps audio in Atlas: a named refusal."""
    code = (
        "strict_privacy_unavailable"
        if privacy == "strict"
        else "privacy_feature_refused"
    )
    return Refusal(
        code,
        f"{privacy.capitalize()} privacy mode: no audio leaves Atlas, so cloud "
        "dictation is off.",
        privacy=privacy,
        feature="dictation_cloud",
    )


_active: set[str] = set()  # users with an open session
_local: set[str] = set()  # users with an open session on the local engine


@dataclass(frozen=True)
class SocketUser:
    subject: str  # interface user id: budget and session identity
    email: str  # verified email: project membership identity


async def socket_user(socket: WebSocket) -> SocketUser:
    """Same-origin signed-in user, as for the voice routes, on a WebSocket handshake."""
    expected = os.environ.get("ATLAS_PROJECT_ORIGIN") or str(socket.base_url).replace(
        "ws", "http", 1
    ).rstrip("/")
    if socket.headers.get("origin") != expected:
        raise ProjectError(403, "origin_rejected")
    user = await token_user(socket.cookies.get("token", ""))
    subject, email = user.get("id"), user.get("email")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 200:
        raise ProjectError(401, "unauthenticated")
    return SocketUser(
        subject, email if isinstance(email, str) and len(email) <= 320 else ""
    )


async def project_privacy(email: str, org: str, project: str) -> str:
    """The project's own privacy setting, read with the user's membership."""
    from services.orchestrator.chat_work import _public

    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=5)
    ) as client:
        status, value = await _public(
            client, email, "GET", f"/api/atlas/orgs/{org}/projects/{project}"
        )
    if status != 200:
        raise ProjectError(status, "project_unavailable")
    from packages.privacy import highest

    return highest(value.get("privacy"))


async def session_privacy(start: dict[str, Any], user: SocketUser) -> str:
    """The session's level: organisation minimum, the level the user sees for the
    conversation (a new one included), project and the browser's own ask, the highest
    wins; fail closed when the conversation or the project cannot be read."""
    from packages.privacy import highest, minimum
    from services.orchestrator.conversation_privacy import PENDING, effective_level

    chat = start.get("chat")
    # No id: a new conversation, whose level is the user's pending choice.
    chat = PENDING if chat is None or chat == "" else chat
    seen = (
        await asyncio.to_thread(effective_level, user.subject, chat)
        if isinstance(chat, str)
        else "strict"
    )
    asked = highest(start.get("privacy"), minimum(), seen)
    scope = start.get("scope")
    if not isinstance(scope, dict) or scope.get("project") is None:
        return asked
    try:
        org, project = (
            str(UUID(str(scope.get("org")))),
            str(UUID(str(scope["project"]))),
        )
    except ValueError:
        return "strict"
    if not user.email:
        return "strict"
    try:
        setting = await project_privacy(user.email, org, project)
    except Exception:  # noqa: BLE001 - an unreadable project setting counts as strict
        return "strict"
    return highest(asked, setting)


def local_installed() -> bool:
    from services.stt import streaming

    return streaming.available()


def cloud_configured() -> bool:
    import realtime_transcription

    return bool(realtime_transcription.configured())


def choose_engine(
    requested: object, privacy: str, limits: Limits, language: str | None = None
) -> tuple[str, bool]:
    """Local first; the cloud engine when the local one is busy, only at the Standard
    privacy level (Strict and Enhanced keep audio in Atlas).

    A language outside the local engine's own goes to the cloud engine at the Standard
    level. Returns the engine and whether it replaced a busy local engine.
    """
    allowed = engines()
    if (
        requested != "local"
        and language is not None
        and language not in LOCAL_LANGUAGES
        and privacy == "standard"
        and "cloud" in allowed
        and cloud_configured()
    ):
        return "cloud", False
    if requested == "cloud":
        if privacy != "standard":
            raise cloud_refusal(privacy)
        if "cloud" not in allowed:
            raise Refusal("engine_unavailable", "This engine is not enabled.")
        return "cloud", False
    local = "local" in allowed and local_installed()
    if local and len(_local) < limits.max_local:
        return "local", False
    if requested == "local" or privacy != "standard" or "cloud" not in allowed:
        if not local:
            raise Refusal("engine_unavailable", "The local engine is not installed.")
        raise Refusal(
            "local_busy",
            "The local dictation engine is busy"
            + (
                f"; {privacy} privacy keeps audio in Atlas."
                if privacy != "standard"
                else "."
            ),
            privacy=privacy,
        )
    if not cloud_configured():
        if local:
            raise Refusal("local_busy", "The local dictation engine is busy.")
        raise Refusal("engine_unavailable", "No realtime transcription provider.")
    return "cloud", local


def price_micro(engine: str, seconds: float) -> int:
    if engine != "cloud":
        return 0  # owned CPU: no provider charge
    import realtime_transcription

    return realtime_transcription.price_micro(seconds)


def final_price_micro(privacy: str, seconds: float) -> int:
    """Reserved cost of the final pass: the batch provider's price, nothing on owned CPU."""
    if final_pass_engine(privacy) != "batch":
        return 0
    import transcription

    return int(transcription.price_micro(seconds))


def batch_configured() -> bool:
    import transcription

    return bool(transcription.configured())


def final_pass_engine(privacy: str) -> str | None:
    """Which engine transcribes the whole recording at stop: the batch provider at the
    Standard level, the owned engine otherwise (or when no provider is configured)."""
    if os.environ.get("ATLAS_DICTATION_FINAL_PASS", "on") == "off":
        return None
    if privacy == "standard" and batch_configured():
        return "batch"
    if "local" in engines() and local_installed():
        return "local"
    return None


def final_pass_deadline(seconds: float) -> float:
    """Seconds allowed to the final pass of a recording: measured 1.25 s for 45 s by the
    provider and about 0.05 s per audio second on the owned CPU engine, with margin."""
    return min(
        _number("ATLAS_DICTATION_FINAL_PASS_MAX_S", 30),
        5 + 0.15 * seconds,
    )


def wav(pcm: bytes) -> bytes:
    """16 kHz mono 16-bit PCM in a WAV container, in memory."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(pcm)
    return buffer.getvalue()


@dataclass(frozen=True)
class FinalPass:
    text: str | None  # None when the pass failed: the live text is written
    engine: str | None
    outcome: str  # done, failed, late, provider_limit, skipped
    seconds: float = 0.0
    cost_micro: int = 0
    language: str | None = None


async def final_pass(
    pcm: bytes, privacy: str, language: str | None, speech: list[float] | None
) -> FinalPass:
    """The whole recording transcribed once more; never raises."""
    engine = final_pass_engine(privacy)
    audio_seconds = len(pcm) / BYTES_PER_SECOND
    if engine is None or audio_seconds < 0.3:
        return FinalPass(None, engine, "skipped")
    deadline = final_pass_deadline(audio_seconds)
    started = time.monotonic()
    try:
        if engine == "batch":
            import transcription

            result = await asyncio.wait_for(
                transcription.transcribe(
                    wav(pcm),
                    filename="dictation.wav",
                    content_type="audio/wav",
                    language=language if language in cloud_languages() else None,
                    temperature=None,
                    segments=False,
                    timeout=deadline,
                ),
                deadline + 1,
            )
            detected = result.get("language")
            cost_micro = result.get("cost_micro") or transcription.price_micro(
                audio_seconds
            )
            text = str(result["text"])
            spoken = language or (detected if isinstance(detected, str) else None)
        else:
            from services.stt import streaming

            text = await asyncio.wait_for(
                streaming.transcribe_windows(pcm, speech), deadline
            )
            cost_micro, spoken = 0, language
    except Exception as exc:  # noqa: BLE001 - the live text is written instead
        outcome = (
            "late"
            if isinstance(exc, TimeoutError)
            else "provider_limit"
            if type(exc).__name__ == "ProviderLimit"
            else "failed"
        )
        log.warning(
            "dictation_final_pass_failed engine=%s outcome=%s error_type=%s "
            "audio_seconds=%.1f",
            engine,
            outcome,
            type(exc).__name__,
            audio_seconds,
        )
        return FinalPass(None, engine, outcome, time.monotonic() - started)
    spoken = spoken or guess_language(text)
    return FinalPass(
        remove_hesitations(text.strip(), spoken),
        engine,
        "done",
        time.monotonic() - started,
        int(cost_micro),
        spoken,
    )


async def open_stream(engine: str, language: str | None, delay_ms: int) -> Stream:
    if engine == "local":
        from services.stt import streaming

        if not streaming.available():
            raise Refusal("engine_unavailable", "The local engine is not installed.")
        local = streaming.LocalStream()
        await local.connect(language=language)
        return local
    import realtime_transcription

    if not realtime_transcription.configured():
        raise Refusal("engine_unavailable", "No realtime transcription provider.")
    cloud = realtime_transcription.ProviderStream()
    try:
        await cloud.connect(language=language, delay_ms=delay_ms, turn_detection=True)
    except realtime_transcription.ProviderLimit as exc:
        raise Refusal(
            "provider_limit",
            "The transcription provider refused for quota or rate limit.",
            provider_status=exc.status,
        ) from None
    except realtime_transcription.ProviderUnavailable:
        raise Refusal(
            "engine_unavailable", "The transcription provider is unreachable."
        ) from None
    return CloudSegments(cloud, delay_ms)


@dataclass(frozen=True)
class Segmented:
    kind: str
    text: str = ""
    time_s: float | None = None
    seconds: float | None = None


class CloudSegments:
    """Append-only provider deltas grouped into partial and final turn text."""

    def __init__(self, stream: Any, delay_ms: int) -> None:
        self.stream = stream
        self.settle = delay_ms / 1000 + 0.5

    @property
    def sent_bytes(self) -> int:
        return int(self.stream.sent_bytes)

    async def send(self, pcm: bytes) -> None:
        await self.stream.send(pcm)

    async def finish(self) -> None:
        await self.stream.finish()

    async def aclose(self) -> None:
        await self.stream.aclose()

    async def events(self) -> AsyncIterator[Segmented]:
        import realtime_transcription

        source = self.stream.events().__aiter__()
        current, ending = "", False
        upcoming: asyncio.Future[Any] | None = None
        try:
            while True:
                if upcoming is None:
                    upcoming = asyncio.ensure_future(source.__anext__())
                # Waiting never cancels the provider read: a timeout only closes the turn.
                ready, _ = await asyncio.wait(
                    {upcoming}, timeout=self.settle if ending else None
                )
                if not ready:
                    # Turn ended and the streaming delay has passed: the turn text is final.
                    if current.strip():
                        yield Segmented("final", current.strip())
                    current, ending = "", False
                    continue
                try:
                    event = upcoming.result()
                except (
                    StopAsyncIteration,
                    realtime_transcription.ProviderUnavailable,
                ):
                    raise Refusal(
                        "engine_unavailable",
                        "The transcription provider closed the session.",
                    ) from None
                finally:
                    upcoming = None
                if event.kind == "delta":
                    shown = current.strip()
                    current += event.text
                    if current.strip() != shown:
                        yield Segmented("partial", current.strip())
                elif event.kind == "turn_start":
                    if ending and current.strip():
                        yield Segmented("final", current.strip())
                        current = ""
                    ending = False
                    yield Segmented("turn_start", time_s=event.time_s)
                elif event.kind == "turn_end":
                    ending = True
                    yield Segmented("turn_end", time_s=event.time_s)
                elif event.kind == "done":
                    if current.strip():
                        yield Segmented("final", current.strip())
                    yield Segmented("done", seconds=event.seconds)
                    return
                elif event.kind == "error":
                    raise Refusal(
                        "provider_error",
                        "The transcription provider reported an error.",
                        provider_code=event.code,
                    )

        finally:
            if upcoming is not None:
                upcoming.cancel()


VAD_BYTES = (
    1024  # bytes of audio per speech probability of the local engine (512 samples)
)


class Session:
    """One browser dictation session: relay, segments, clean-up, pieces and metering."""

    def __init__(
        self,
        socket: WebSocket,
        user: str,
        start: dict[str, Any],
        limits: Limits,
        *,
        engine: str,
        privacy: str,
    ) -> None:
        self.socket, self.user, self.limits = socket, user, limits
        self.engine = engine
        # The language the user chose for dictation; the interface language is not one.
        self.language = spoken_language(start.get("dictation_language"))
        self.privacy = privacy
        self.cleaner: Cleaner | None = None
        # Strict privacy cleans up only on an owned model; without one it stays off.
        owned = bool(os.environ.get("ATLAS_OWNED_VLLM_BASE_URL"))
        if (
            start.get("cleanup") is True
            and cleanup_enabled()
            and (self.privacy == "standard" or owned)
        ):
            self.cleaner = Cleaner(self.privacy, self.language)
        self.finals: list[str] = []
        self.cleaned_upto = 0  # finals before this index are closed by a final clean-up
        self.closed_text = ""  # cleaned text of the closed finals, the clean-up context
        self.partial = ""
        self.changed = False
        self.sending = asyncio.Lock()
        self.provider_seconds: float | None = None
        self.started = time.monotonic()
        self.closing: list[asyncio.Task[None]] = []
        self.closing_lock = asyncio.Lock()
        self.audio = bytearray()  # the recording, in memory for the final pass only
        # The local engine's speech probabilities: growing while it runs (``live``),
        # complete once it drained (``speech``); the local final pass reuses them.
        self.live_speech: list[float] | None = None
        self.speech: list[float] | None = None
        self.final: FinalPass | None = None  # the last piece's final pass
        self.passes: list[FinalPass] = []  # every piece's final pass, for cost and logs
        self.committed = 0  # bytes of the recording already given to a piece
        self.turn_end = 0  # bytes of the recording at the latest end of turn
        self.pieces: list[asyncio.Task[None]] = []
        self.final_task: asyncio.Task[None] | None = None
        self.stopped: float | None = None  # monotonic time of the user's stop
        self.drained: float | None = None  # the live engine's last event after stop
        self.text_sent: float | None = None  # the last piece's transcript

    async def emit(self, message: dict[str, object]) -> None:
        async with self.sending:
            await self.socket.send_text(json.dumps(message, ensure_ascii=False))

    async def receive(self, stream: Stream) -> None:
        """Browser to engine, with frame, pace, idle and length limits."""
        while True:
            try:
                message = await asyncio.wait_for(
                    self.socket.receive(), self.limits.idle_seconds
                )
            except TimeoutError:
                raise Refusal("idle_timeout", "No audio received.") from None
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect()
            data = message.get("bytes")
            if data is not None:
                if len(data) > self.limits.max_frame or len(data) % 2:
                    raise Refusal(
                        "invalid_audio", "Frames are 16-bit PCM, 0.5 s at most."
                    )
                seconds = (stream.sent_bytes + len(data)) / BYTES_PER_SECOND
                if seconds > self.limits.max_seconds:
                    self.stopped = time.monotonic()
                    await stream.finish()
                    await self.emit({"type": "limit", "code": "session_length"})
                    return
                if seconds > 2 * (time.monotonic() - self.started) + 5:
                    raise Refusal("audio_too_fast", "Audio must be streamed live.")
                self.audio.extend(data)
                await stream.send(data)
                continue
            text = message.get("text")
            if text is not None:
                control = json.loads(text)
                if not isinstance(control, dict):
                    continue
                if control.get("type") == "commit":
                    self.commit()
                elif control.get("type") == "stop":
                    self.stopped = time.monotonic()
                    await stream.finish()
                    return

    async def relay(self, stream: Stream) -> None:
        """Engine to browser; final clean-up at each end of turn."""
        async for event in stream.events():
            if event.kind == "partial":
                text = remove_hesitations(event.text, self.language)
                self.partial, self.changed = text, True
                await self.emit({"type": "partial", "text": text})
            elif event.kind == "final":
                self.partial = ""
                text = remove_hesitations(event.text, self.language)
                if text:
                    self.finals.append(text)
                    self.changed = True
                    await self.emit(
                        {
                            "type": "final",
                            "index": len(self.finals) - 1,
                            "text": text,
                        }
                    )
            elif event.kind in {"turn_start", "turn_end"}:
                if event.kind == "turn_end" and event.time_s is not None:
                    # Where the next piece starts when the browser commits this turn.
                    at = int(event.time_s * BYTES_PER_SECOND)
                    self.turn_end = max(self.turn_end, at - at % 2)
                await self.emit(
                    {"type": "turn", "event": event.kind[5:], "time_s": event.time_s}
                )
                if event.kind == "turn_end" and self.cleaner is not None:
                    # In the background: the next turn's text must not wait for it.
                    self.closing.append(asyncio.create_task(self.close_turn()))
            elif event.kind == "done":
                self.provider_seconds = event.seconds
                self.drained = time.monotonic()
                speech = getattr(stream, "speech", None)
                self.speech = speech if isinstance(speech, list) else None
                if final_pass_engine(self.privacy) is None:
                    # No final pass replaces the live text: it gets its last clean-up.
                    await self.close_turn(last=True)
                return

    def commit(self) -> None:
        """Close a piece at the latest end of turn; its final pass runs meanwhile."""
        cut = self.turn_end if self.committed < self.turn_end else len(self.audio)
        cut = min(cut, len(self.audio))
        if cut - self.committed < BYTES_PER_SECOND // 5:
            return  # nothing new was said since the last piece
        begin, self.committed = self.committed, cut
        self.add_piece(begin, cut, len(self.finals), last=False)

    def start_final(self) -> None:
        """The last piece: the rest of the recording, at stop."""
        if self.final_task is None:
            self.final_task = self.add_piece(
                self.committed, len(self.audio), None, last=True
            )

    def add_piece(
        self, begin: int, end: int, upto: int | None, *, last: bool
    ) -> asyncio.Task[None]:
        index, previous = len(self.pieces), self.pieces[-1] if self.pieces else None
        task = asyncio.create_task(
            self.piece(index, begin, end, upto, previous, last=last)
        )
        self.pieces.append(task)
        return task

    async def piece(
        self,
        index: int,
        begin: int,
        end: int,
        upto: int | None,
        previous: asyncio.Task[None] | None,
        *,
        last: bool,
    ) -> None:
        """One piece's final pass, sent after the previous piece (pieces keep their order)."""
        speech = self.speech if self.speech is not None else self.live_speech
        if last and self.speech is None:
            speech = None  # an undrained stream misses the last frames: VAD again
        window = (
            speech[begin // VAD_BYTES : -(-end // VAD_BYTES)]
            if speech is not None
            else None
        )
        result = await final_pass(
            bytes(self.audio[begin:end]), self.privacy, self.language, window
        )
        if previous is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(previous)
        self.passes.append(result)
        if last:
            self.final = result
            self.audio = bytearray()
            if result.outcome != "done" and self.cleaner is not None:
                # The live text is written instead: it gets its last clean-up.
                await self.close_turn(last=True)
            if result.outcome == "skipped":
                return
        await self.emit(
            {
                "type": "transcript",
                "piece": index,
                "to": upto,
                "last": last,
                "text": result.text,
                "outcome": result.outcome,
                "seconds": round(result.seconds, 2),
            }
        )
        if last:
            self.text_sent = time.monotonic()

    async def close_turn(self, *, last: bool = False) -> None:
        async with self.closing_lock:
            await self._close_turn(last)

    async def _close_turn(self, last: bool = False) -> None:
        if self.cleaner is None or self.cleaned_upto >= len(self.finals):
            return
        start, end = self.cleaned_upto, len(self.finals)
        raw = " ".join(self.finals[start:end])
        # Only the end of the dictation may change the last word (see accepted()).
        outcome = await self.cleaner.clean(
            self.closed_text, raw, final=True, open_end=not last
        )
        text = outcome.text if outcome.text is not None else raw
        self.cleaned_upto, self.closed_text = (
            end,
            (self.closed_text + " " + text).strip(),
        )
        await self.emit(
            {
                "type": "cleaned",
                "from": start,
                "to": end,
                "partial": False,
                "final": True,
                "text": text,
                "outcome": outcome.reason,
            }
        )

    async def tick(self) -> None:
        """Clean the open text every interval while it changes (never queues calls)."""
        interval = _number("ATLAS_DICTATION_CLEANUP_INTERVAL_S", 2.5)
        while self.cleaner is not None and self.stopped is None:
            await asyncio.sleep(interval)
            if not self.changed or self.stopped is not None:
                continue
            self.changed = False
            start, end = self.cleaned_upto, len(self.finals)
            partial = self.partial
            raw = " ".join(self.finals[start:end] + ([partial] if partial else []))
            if not raw.strip():
                continue
            outcome = await self.cleaner.clean(self.closed_text, raw, final=False)
            if outcome.text is None or start != self.cleaned_upto:
                continue
            await self.emit(
                {
                    "type": "cleaned",
                    "from": start,
                    "to": end,
                    "partial": bool(partial),
                    "final": False,
                    "text": outcome.text,
                    "outcome": outcome.reason,
                }
            )

    def record(self, stream: Stream | None, spent: Decimal, outcome: str) -> str:
        """The journal line of the session: no text, no audio, no identity."""
        final = self.final

        def since_stop(moment: float | None) -> float | None:
            if moment is None or self.stopped is None:
                return None
            return round(moment - self.stopped, 2)

        return json.dumps(
            {
                "event": "dictation_session",
                "outcome": outcome,
                "privacy": self.privacy,
                "engine": self.engine,
                "audio_seconds": round(
                    (stream.sent_bytes if stream else 0) / BYTES_PER_SECOND, 2
                ),
                "pieces": len(self.passes),
                "cleanup_calls": self.cleaner.calls if self.cleaner else 0,
                "final_pass": final.engine if final else None,
                "final_outcome": final.outcome if final else None,
                "final_seconds": round(final.seconds, 2) if final else None,
                "drain_seconds": since_stop(self.drained),
                "stop_to_text_seconds": since_stop(self.text_sent),
                "cost_eur": str(spent),
            }
        )


async def wait_tasks(
    tasks: set[asyncio.Task[None]], when: str
) -> set[asyncio.Task[None]]:
    """Wait as asyncio.wait does; a task that failed raises its exception."""
    done, _ = await asyncio.wait(tasks, return_when=when)
    for task in done:
        failure = task.exception()
        if failure is not None:
            raise failure
    return done


@router.websocket("/api/atlas/dictation/stream")
async def stream_socket(socket: WebSocket) -> None:
    if not enabled():
        await socket.close(code=1008)
        return
    try:
        identity = await socket_user(socket)
    except ProjectError:
        await socket.close(code=1008)
        return
    user = identity.subject
    await socket.accept()
    limits = Limits.current()
    session: Session | None = None
    stream: Stream | None = None
    ledger, reservation = DailyLedger(), None
    # The signed-in user's email and account id: one budget bucket (budget_identity.py).
    ledger.remember_alias(identity.email, user)
    tasks: list[asyncio.Task[None]] = []
    registered = False
    outcome = "disconnected"
    try:
        start = json.loads(await asyncio.wait_for(socket.receive_text(), 10))
        if not isinstance(start, dict) or start.get("type") != "start":
            raise Refusal("invalid_request", "The first message is a start message.")
        privacy = await session_privacy(start, identity)
        from packages.privacy import elevate

        # The session's level also holds at every provider egress (packages.privacy).
        elevate(privacy)
        if user in _active:
            raise Refusal(
                "session_open", "A dictation session is already open for this user."
            )
        if len(_active) >= limits.max_sessions:
            raise Refusal("busy", "Too many dictation sessions right now.")
        # No await between the choice and the registration: the local cap holds.
        engine, fallback = choose_engine(
            start.get("engine"),
            privacy,
            limits,
            spoken_language(start.get("dictation_language")),
        )
        if privacy != "standard" and engine != "local":
            raise cloud_refusal(privacy)
        _active.add(user)
        if engine == "local":
            _local.add(user)
        registered = True
        session = Session(socket, user, start, limits, engine=engine, privacy=privacy)
        try:
            reservation = ledger.reserve_cost(
                user,
                Decimal(price_micro(session.engine, limits.max_seconds)) / 1_000_000
                + Decimal(final_price_micro(session.privacy, limits.max_seconds))
                / 1_000_000
                + (Decimal("0.05") if session.cleaner else Decimal(0)),
                session.language or "en",
            )
        except BudgetRefusal as exc:
            raise Refusal("budget_limit", exc.message, category=exc.category) from None
        stream = await open_stream(
            session.engine,
            session.language if session.language in cloud_languages() else None,
            limits.delay_ms,
        )
        live_speech = getattr(stream, "speech", None)
        if isinstance(live_speech, list):
            session.live_speech = live_speech
        await session.emit(
            {
                "type": "ready",
                "engine": session.engine,
                "fallback": fallback,
                "privacy": session.privacy,
                "max_seconds": limits.max_seconds,
                "cleanup": session.cleaner is not None,
                # Where audio and text go, for the panel's label (never a provider name):
                # the live engine or the final pass at a partner provider, and the
                # clean-up at a shared model (Strict and Enhanced use owned ones only).
                "audio_out": session.engine == "cloud"
                or final_pass_engine(session.privacy) == "batch",
                "text_out": session.cleaner is not None
                and session.privacy == "standard",
            }
        )
        receive = asyncio.create_task(session.receive(stream))
        relay = asyncio.create_task(session.relay(stream))
        tasks = [receive, relay]
        if session.cleaner is not None:
            tasks.append(asyncio.create_task(session.tick()))
        done = await wait_tasks({receive, relay}, asyncio.FIRST_COMPLETED)
        if receive in done and final_pass_engine(session.privacy) == "batch":
            # The batch pass needs only the recording: it starts at stop, while the
            # live engine drains (the owned engine's pass reuses the drained VAD).
            session.start_final()
        await wait_tasks({receive, relay}, asyncio.FIRST_EXCEPTION)
        await asyncio.wait_for(relay, limits.idle_seconds + 10)
        session.start_final()
        assert session.final_task is not None
        await session.final_task
        await session.emit(
            {
                "type": "done",
                "seconds": round(stream.sent_bytes / BYTES_PER_SECOND, 2),
                "provider_seconds": session.provider_seconds,
                "cost_eur": str(cost(session, stream)),
                "cleanup": session.cleaner.summary() if session.cleaner else None,
                "final_pass": session.final.outcome if session.final else None,
            }
        )
        outcome = "done"
    except Refusal as exc:
        outcome = str(exc.body.get("code"))
        with contextlib.suppress(Exception):
            await socket.send_text(json.dumps(exc.body))
    except (WebSocketDisconnect, TimeoutError, ValueError, RuntimeError) as exc:
        outcome = (
            "disconnected"
            if isinstance(exc, WebSocketDisconnect)
            else "timeout"
            if isinstance(exc, TimeoutError)
            else "invalid"
        )
    except Exception as exc:  # noqa: BLE001 - an engine failure must still close cleanly
        outcome = "engine_failed"
        log.warning("dictation_engine_failed error_type=%s", type(exc).__name__)
        with contextlib.suppress(Exception):
            await socket.send_text(
                json.dumps(
                    {
                        "type": "error",
                        "code": "engine_unavailable",
                        "message": "The dictation engine failed.",
                    }
                )
            )
    finally:
        if registered:
            # Released first: closing the provider socket may take a moment.
            _active.discard(user)
            _local.discard(user)
        pending = tasks + (
            session.closing + session.pieces if session is not None else []
        )
        for task in pending:
            task.cancel()
        if stream is not None:
            await stream.aclose()
        spent = (
            cost(session, stream)
            if session is not None and stream is not None
            else Decimal(0)
        )
        if reservation is not None:
            ledger.settle_cost(reservation, spent)
        if session is not None:
            log.warning(session.record(stream, spent, outcome))
        else:
            log.warning(json.dumps({"event": "dictation_session", "outcome": outcome}))
        with contextlib.suppress(Exception):
            await socket.close()


def cost(session: Session, stream: Stream) -> Decimal:
    """Billed audio is the larger of streamed and provider-reported seconds."""
    seconds = max(stream.sent_bytes / BYTES_PER_SECOND, session.provider_seconds or 0.0)
    total = Decimal(price_micro(session.engine, seconds)) / 1_000_000
    for done in session.passes:
        total += Decimal(done.cost_micro) / 1_000_000
    if session.cleaner is not None:
        total += session.cleaner.cost_eur
    return total


@router.get("/api/atlas/dictation/")
async def page(request: Request) -> Response:
    if not enabled():
        return public_error(ProjectError(404, "not_found"))
    try:
        await browser_user(request)
    except ProjectError as exc:
        return public_error(exc)
    return HTMLResponse(
        (HERE / "dictation.html").read_text(),
        headers={"Cache-Control": "private, no-store"},
    )


_warming: set[asyncio.Task[None]] = set()


@router.get("/api/atlas/dictation/config")
async def config(request: Request) -> Response:
    """Whether the chat shows the Atlas voice button; warms the local engine up."""
    try:
        await browser_user(request)
    except ProjectError as exc:
        return public_error(exc)
    headers = {"Cache-Control": "private, no-store"}
    if not enabled():
        return JSONResponse({"enabled": False}, headers=headers)
    available = engines()
    if "local" in available and local_installed() and not _warming:
        from services.stt import streaming

        # The first dictation then skips the model load (a few seconds on this CPU).
        task = asyncio.create_task(asyncio.to_thread(streaming.warm_up))
        _warming.add(task)
    return JSONResponse(
        {"enabled": bool(available), "max_seconds": Limits.current().max_seconds},
        headers=headers,
    )


@router.get("/api/atlas/dictation/{name}")
def asset(name: str) -> Response:
    if not enabled() or name not in {"dictation.js", "dictation-worklet.js"}:
        return public_error(ProjectError(404, "not_found"))
    return Response(
        (HERE / name).read_text(),
        media_type="text/javascript",
        headers={"Cache-Control": "private, no-store"},
    )
