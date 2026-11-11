"""Real-time dictation: a WebSocket relay from the browser microphone to a streaming engine.

Prototype behind ATLAS_REALTIME_DICTATION=1 (off by default), served on its own test page
and never injected into the chat composer. The browser sends 16 kHz mono PCM frames; Atlas
relays them to the selected engine and sends partial and final text back:

- ``cloud``: the gateway's realtime transcription provider (realtime_transcription.py).
  The provider key stays on the server. Refused in strict privacy: no audio leaves Atlas.
- ``local``: the owned CPU engine (services/stt/streaming.py), allowed in strict privacy.

Client messages: one JSON ``start`` ({"type": "start", "engine", "language", "privacy",
"cleanup"}), then binary PCM s16le frames, then {"type": "stop"}. Server messages:
``ready``, ``turn``, ``partial`` (text of the open utterance, replaced each time), ``final``
(a closed segment, by index), ``cleaned`` (a model-cleaned view of segments, see
dictation_cleanup.py), ``done`` (seconds and cost) and ``error`` (a public code; a provider
quota or rate refusal is ``provider_limit``, never an outage).

Limits: session length, idle time, frame size, audio pace, one session per user and a
global session cap. Cost is reserved in the daily ledger at start and settled at the end.
Audio and text are never stored or logged.
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

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response

from services.orchestrator.daily_budget import BudgetRefusal, DailyLedger
from services.orchestrator.dictation_cleanup import Cleaner
from services.orchestrator.dictation_cleanup import enabled as cleanup_enabled
from services.orchestrator.project_ui import public_error
from services.orchestrator.user_memory_ui import browser_user, token_subject
from services.retrieval.project_identity import ProjectError

router = APIRouter()
log = logging.getLogger("atlas.dictation")
HERE = Path(__file__).parent
LANGUAGES = frozenset({"fr", "en", "de", "es", "it"})
BYTES_PER_SECOND = 32000


def enabled() -> bool:
    return os.environ.get("ATLAS_REALTIME_DICTATION") == "1"


def _number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def engines() -> list[str]:
    raw = os.environ.get("ATLAS_DICTATION_ENGINES", "cloud")
    return [name for name in raw.split(",") if name.strip() in {"cloud", "local"}]


@dataclass(frozen=True)
class Limits:
    max_seconds: float
    idle_seconds: float
    max_frame: int
    max_sessions: int
    delay_ms: int

    @classmethod
    def current(cls) -> "Limits":
        return cls(
            _number("ATLAS_DICTATION_MAX_SECONDS", 300),
            _number("ATLAS_DICTATION_IDLE_SECONDS", 20),
            int(_number("ATLAS_DICTATION_MAX_FRAME_BYTES", 16000)),
            int(_number("ATLAS_DICTATION_MAX_SESSIONS", 4)),
            int(_number("ATLAS_DICTATION_DELAY_MS", 480)),
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


_active: set[str] = set()


async def socket_user(socket: WebSocket) -> str:
    """Same-origin signed-in user, as for the voice routes, on a WebSocket handshake."""
    expected = os.environ.get("ATLAS_PROJECT_ORIGIN") or str(socket.base_url).replace(
        "ws", "http", 1
    ).rstrip("/")
    if socket.headers.get("origin") != expected:
        raise ProjectError(403, "origin_rejected")
    return await token_subject(socket.cookies.get("token", ""))


def price_micro(engine: str, seconds: float) -> int:
    if engine != "cloud":
        return 0  # owned CPU: no provider charge
    import realtime_transcription

    return realtime_transcription.price_micro(seconds)


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


class Session:
    """One browser dictation session: relay, segments, clean-up and metering."""

    def __init__(
        self, socket: WebSocket, user: str, start: dict[str, Any], limits: Limits
    ) -> None:
        self.socket, self.user, self.limits = socket, user, limits
        self.engine = str(start.get("engine") or engines()[0])
        language = start.get("language")
        self.language = language if language in LANGUAGES else None
        self.privacy = "strict" if start.get("privacy") == "strict" else "standard"
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

    async def emit(self, message: dict[str, object]) -> None:
        async with self.sending:
            await self.socket.send_text(json.dumps(message, ensure_ascii=False))

    def check(self) -> None:
        if self.engine not in engines():
            raise Refusal("engine_unavailable", "This engine is not enabled.")
        if self.privacy == "strict" and self.engine != "local":
            raise Refusal(
                "strict_privacy_unavailable",
                "Strict privacy mode: no audio leaves Atlas, so cloud dictation is off.",
            )

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
                    await stream.finish()
                    await self.emit({"type": "limit", "code": "session_length"})
                    return
                if seconds > 2 * (time.monotonic() - self.started) + 5:
                    raise Refusal("audio_too_fast", "Audio must be streamed live.")
                await stream.send(data)
                continue
            text = message.get("text")
            if text is not None:
                control = json.loads(text)
                if isinstance(control, dict) and control.get("type") == "stop":
                    await stream.finish()
                    return

    async def relay(self, stream: Stream) -> None:
        """Engine to browser; final clean-up at each end of turn."""
        async for event in stream.events():
            if event.kind == "partial":
                self.partial, self.changed = event.text, True
                await self.emit({"type": "partial", "text": event.text})
            elif event.kind == "final":
                self.partial = ""
                if event.text:
                    self.finals.append(event.text)
                    self.changed = True
                    await self.emit(
                        {
                            "type": "final",
                            "index": len(self.finals) - 1,
                            "text": event.text,
                        }
                    )
            elif event.kind in {"turn_start", "turn_end"}:
                await self.emit(
                    {"type": "turn", "event": event.kind[5:], "time_s": event.time_s}
                )
                if event.kind == "turn_end" and self.cleaner is not None:
                    # In the background: the next turn's text must not wait for it.
                    self.closing.append(asyncio.create_task(self.close_turn()))
            elif event.kind == "done":
                self.provider_seconds = event.seconds
                await self.close_turn()
                return

    async def close_turn(self) -> None:
        async with self.closing_lock:
            await self._close_turn()

    async def _close_turn(self) -> None:
        if self.cleaner is None or self.cleaned_upto >= len(self.finals):
            return
        start, end = self.cleaned_upto, len(self.finals)
        raw = " ".join(self.finals[start:end])
        outcome = await self.cleaner.clean(self.closed_text, raw, final=True)
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
        while self.cleaner is not None:
            await asyncio.sleep(interval)
            if not self.changed:
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


@router.websocket("/api/atlas/dictation/stream")
async def stream_socket(socket: WebSocket) -> None:
    if not enabled():
        await socket.close(code=1008)
        return
    try:
        user = await socket_user(socket)
    except ProjectError:
        await socket.close(code=1008)
        return
    await socket.accept()
    limits = Limits.current()
    session: Session | None = None
    stream: Stream | None = None
    ledger, reservation = DailyLedger(), None
    tasks: list[asyncio.Task[None]] = []
    registered = False
    try:
        start = json.loads(await asyncio.wait_for(socket.receive_text(), 10))
        if not isinstance(start, dict) or start.get("type") != "start":
            raise Refusal("invalid_request", "The first message is a start message.")
        session = Session(socket, user, start, limits)
        session.check()
        if user in _active:
            raise Refusal("busy", "A dictation session is already open for this user.")
        if len(_active) >= limits.max_sessions:
            raise Refusal("busy", "Too many dictation sessions right now.")
        _active.add(user)
        registered = True
        try:
            reservation = ledger.reserve_cost(
                user,
                Decimal(price_micro(session.engine, limits.max_seconds)) / 1_000_000
                + (Decimal("0.05") if session.cleaner else Decimal(0)),
                session.language or "en",
            )
        except BudgetRefusal as exc:
            raise Refusal("budget_limit", exc.message, category=exc.category) from None
        stream = await open_stream(session.engine, session.language, limits.delay_ms)
        await session.emit(
            {
                "type": "ready",
                "engine": session.engine,
                "max_seconds": limits.max_seconds,
                "cleanup": session.cleaner is not None,
            }
        )
        tasks = [
            asyncio.create_task(session.receive(stream)),
            asyncio.create_task(session.relay(stream)),
        ]
        if session.cleaner is not None:
            tasks.append(asyncio.create_task(session.tick()))
        relay = tasks[1]
        await asyncio.wait({tasks[0], relay}, return_when=asyncio.FIRST_EXCEPTION)
        for task in (tasks[0], relay):
            failure = task.exception() if task.done() else None
            if failure is not None:
                raise failure
        await asyncio.wait_for(relay, limits.idle_seconds + 10)
        await session.emit(
            {
                "type": "done",
                "seconds": round(stream.sent_bytes / BYTES_PER_SECOND, 2),
                "provider_seconds": session.provider_seconds,
                "cost_eur": str(cost(session, stream)),
                "cleanup": session.cleaner.summary() if session.cleaner else None,
            }
        )
    except Refusal as exc:
        with contextlib.suppress(Exception):
            await socket.send_text(json.dumps(exc.body))
    except (WebSocketDisconnect, TimeoutError, ValueError, RuntimeError):
        pass
    except Exception as exc:  # noqa: BLE001 - an engine failure must still close cleanly
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
        for task in tasks + (session.closing if session is not None else []):
            task.cancel()
        if stream is not None:
            await stream.aclose()
        if reservation is not None and session is not None:
            spent = cost(session, stream) if stream is not None else Decimal(0)
            ledger.settle_cost(reservation, spent)
            log.info(
                json.dumps(
                    {
                        "event": "dictation_session",
                        "engine": session.engine,
                        "audio_seconds": round(
                            (stream.sent_bytes if stream else 0) / BYTES_PER_SECOND, 2
                        ),
                        "cost_eur": str(spent),
                        "cleanup_calls": session.cleaner.calls
                        if session.cleaner
                        else 0,
                    }
                )
            )
        with contextlib.suppress(Exception):
            await socket.close()


def cost(session: Session, stream: Stream) -> Decimal:
    """Billed audio is the larger of streamed and provider-reported seconds."""
    seconds = max(stream.sent_bytes / BYTES_PER_SECOND, session.provider_seconds or 0.0)
    total = Decimal(price_micro(session.engine, seconds)) / 1_000_000
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


@router.get("/api/atlas/dictation/{name}")
def asset(name: str) -> Response:
    if not enabled() or name not in {"dictation.js", "dictation-worklet.js"}:
        return public_error(ProjectError(404, "not_found"))
    return Response(
        (HERE / name).read_text(),
        media_type="text/javascript",
        headers={"Cache-Control": "private, no-store"},
    )
