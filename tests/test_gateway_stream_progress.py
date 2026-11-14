"""Reasoning progress, single-attempt deadline and abandoned calls (2026-11-14).

The provider side is a local HTTP server streaming frames in the shapes
providers send; the gateway side is the real code: the HTTP boundary, the
quality controller, the aiohttp transport and the stream decoder.
"""

from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import asyncio
import json
import os
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

from agent_provider import AgentProvider, AgentRequest
from gateway_cpu import CPUModels
from http_gateway import serve
from quality import stream_quality
from serverless_support import environment
from streaming import StreamDecoder
import upstream


def frame(delta: dict[str, object], finish: str | None = None) -> bytes:
    return (
        "data: "
        + json.dumps(
            {
                "id": "cmpl-fictional",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
        )
        + "\n\n"
    ).encode()


def thinking(text: str, closed: bool = False) -> dict[str, object]:
    """A reasoning piece as Mistral streams it (shape checked live, 2026-11-14)."""
    return {
        "type": "thinking",
        "thinking": [{"type": "text", "text": text}],
        **({"closed": True} if closed else {}),
    }


def end(output: int = 40) -> bytes:
    usage = {"prompt_tokens": 20, "completion_tokens": output}
    return (
        frame({}, "stop")
        + ("data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n").encode()
        + b"data: [DONE]\n\n"
    )


class ReasoningDecodingTests(unittest.TestCase):
    def test_thinking_parts_are_reasoning_not_dropped(self) -> None:
        decoder = StreamDecoder(allow_length=True)
        events = decoder.feed(frame({"content": [thinking("Count the legs. ")]}))
        events += decoder.feed(frame({"content": [thinking("Four.", closed=True)]}))
        self.assertEqual(
            events,
            [
                {"delta": {"reasoning_content": "Count the legs. "}},
                {"delta": {"reasoning_content": "Four."}},
            ],
        )
        decoder.feed(frame({"content": "A cat."}) + end())
        decoder.finish()
        assert decoder.result is not None
        self.assertEqual(decoder.result["text"], "A cat.")
        self.assertEqual(decoder.result["reasoning"], "Count the legs. Four.")
        self.assertEqual(decoder.reasoning, "Count the legs. Four.")

    def test_frame_closing_reasoning_and_opening_answer_keeps_order(self) -> None:
        decoder = StreamDecoder(allow_length=True)
        events = decoder.feed(
            frame(
                {
                    "content": [
                        thinking("done", closed=True),
                        {"type": "text", "text": "Answer"},
                    ]
                }
            )
        )
        self.assertEqual(
            events,
            [
                {"delta": {"reasoning_content": "done"}},
                {"delta": {"content": "Answer"}},
            ],
        )

    def test_every_reasoning_shape_is_counted(self) -> None:
        shapes: list[dict[str, object]] = [
            {"reasoning_content": "step"},
            {"reasoning": "step"},
            {"content": [thinking("step")]},
            {"content": [{"type": "thinking", "thinking": "step"}]},
        ]
        for delta in shapes:
            with self.subTest(delta=delta):
                decoder = StreamDecoder(allow_length=True)
                self.assertEqual(
                    decoder.feed(frame(delta)),
                    [{"delta": {"reasoning_content": "step"}}],
                )
                self.assertEqual(decoder.text, "")

    def test_any_chunk_boundary_decodes_the_same_stream(self) -> None:
        # Network chunks may split a line, a CRLF pair or a multi-byte
        # character anywhere: the decoded stream must not depend on it.
        stream = (
            frame({"content": [thinking("Où placer le chêne ? ")]}).replace(
                b"\n\n", b"\r\n\r\n"
            )
            + b": keep-alive comment\n\n"
            + frame({"content": [thinking("À gauche.", closed=True)]})
            + frame({"content": "Un renard roux, né à l'été."})
            + end()
        )

        def decode(pieces: list[bytes]) -> tuple[list[dict[str, object]], object]:
            decoder = StreamDecoder(allow_length=True)
            events = [event for piece in pieces for event in decoder.feed(piece)]
            decoder.finish()
            return events, decoder.result

        whole = decode([stream])
        self.assertEqual(len(whole[0]), 3)
        for cut in range(1, len(stream)):
            self.assertEqual(decode([stream[:cut], stream[cut:]]), whole, cut)
        self.assertEqual(decode([stream[i : i + 1] for i in range(len(stream))]), whole)


class FakeProvider:
    """A local provider streaming reasoning, then an answer, at a fixed pace."""

    def __init__(self, reasoning_seconds: float, step: float = 0.05) -> None:
        self.reasoning_seconds = reasoning_seconds
        self.step = step
        self.requests = 0
        self.frames_sent = 0
        self.closed_at: float | None = None
        self.finished = threading.Event()
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                provider.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                started = time.monotonic()
                try:
                    while time.monotonic() - started < provider.reasoning_seconds:
                        self.wfile.write(frame({"content": [thinking("Hmm. ")]}))
                        self.wfile.flush()
                        provider.frames_sent += 1
                        time.sleep(provider.step)
                    self.wfile.write(frame({"content": "A drawn fox."}) + end())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    provider.closed_at = time.monotonic()
                finally:
                    provider.finished.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    @contextmanager
    def running(self) -> Iterator["FakeProvider"]:
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        try:
            with (
                patch.dict(os.environ, environment()),
                patch("provider_settings.endpoint", return_value=self.url),
            ):
                yield self
        finally:
            self.server.shutdown()
            self.server.server_close()


def profile_request(**options: object) -> dict[str, object]:
    return {
        "messages": [{"role": "user", "content": "Draw a fox in a meadow."}],
        "tools": [],
        "timeout": 1.0,
        "local_enabled": False,
        "profile": "atlas-glm",
        "max_tokens": 1000,
        **options,
    }


class ReasoningProgressTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, payload: dict[str, object]) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        async for event in stream_quality(
            AgentProvider(),
            AgentRequest.model_validate(payload),
            complete_response=True,
        ):
            events.append(event)
        return events

    async def test_streamed_reasoning_keeps_a_call_alive(self) -> None:
        # The first attempt of a complete call is cut at half its deadline
        # (0.5 s) unless it makes progress; here the model only reasons for
        # 1.2 s, in Mistral's typed parts, before it answers.
        with (
            FakeProvider(reasoning_seconds=1.2).running() as provider,
            patch("quality.PROGRESS_STALL_SECONDS", 0.4),
        ):
            events = await self.collect(profile_request(progress_timeout=5.0))
        self.assertEqual(provider.requests, 1)
        result = events[-1]["result"]
        assert isinstance(result, dict)
        self.assertEqual(result["text"], "A drawn fox.")
        self.assertTrue(str(result["reasoning"]).startswith("Hmm. "))
        self.assertTrue(
            any(
                isinstance(delta := event.get("delta"), dict)
                and "reasoning_content" in delta
                for event in events
            )
        )

    async def test_silent_call_is_still_cut(self) -> None:
        # Progress is reasoning actually received, not time: a provider that
        # sends nothing is cut at the stall window as before.
        seen: list[float] = []

        async def silent(
            request: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            seen.append(timeout)
            await asyncio.sleep(10)
            yield {}

        started = time.monotonic()
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", silent),
            self.assertRaises(TimeoutError),
        ):
            await self.collect(profile_request(single_attempt=True))
        self.assertLess(time.monotonic() - started, 2.0)

    async def test_single_attempt_gets_its_whole_deadline(self) -> None:
        seen: list[float] = []

        async def upstream(
            request: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            seen.append(timeout)
            # Silent for longer than half the deadline, as a model reasoning
            # on a provider that streams nothing meanwhile.
            await asyncio.sleep(0.7)
            yield {
                "result": {
                    "text": "A drawn fox.",
                    "calls": [],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 40},
                    "finish_reason": "stop",
                    "reasoning": "",
                    "reasoning_tokens": 0,
                }
            }

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            events = await self.collect(profile_request(single_attempt=True))
            self.assertEqual(len(seen), 1)
            self.assertAlmostEqual(seen[0], 1.0, delta=0.05)
            result = events[-1]["result"]
            assert isinstance(result, dict)
            self.assertEqual(result["text"], "A drawn fox.")
            # Without single_attempt the first attempt still keeps the
            # recovery share (half of a complete call's deadline).
            seen.clear()
            with self.assertRaises(Exception):
                await self.collect(profile_request())
            self.assertAlmostEqual(seen[0], 0.5, delta=0.05)


class AbandonedCallTests(unittest.TestCase):
    """A caller that leaves closes the provider stream: no generation for nobody."""

    def abandon(self, path: str) -> FakeProvider:
        with (
            FakeProvider(reasoning_seconds=20.0).running() as provider,
            patch("upstream.abort", wraps=upstream.abort) as aborted,
        ):
            gateway = serve(Mock(spec=CPUModels), 0)
            thread = threading.Thread(target=gateway.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps(profile_request(timeout=60.0)).encode()
                client = socket.create_connection(
                    ("127.0.0.1", gateway.server_address[1])
                )
                client.sendall(
                    (
                        f"POST {path} HTTP/1.1\r\nHost: gateway\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\n\r\n"
                    ).encode()
                    + body
                )
                deadline = time.monotonic() + 5
                while provider.frames_sent < 5 and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertGreaterEqual(provider.frames_sent, 5)
                with self.assertLogs("http_gateway", "WARNING") as logs:
                    left = time.monotonic()
                    client.close()
                    self.assertTrue(provider.finished.wait(5))
            finally:
                gateway.shutdown()
                gateway.server_close()
        assert provider.closed_at is not None
        # Closed upstream within a second, not after the 20 s of generation.
        self.assertLess(provider.closed_at - left, 1.0)
        self.assertIn('"event": "gateway_client_gone"', "\n".join(logs.output))
        # Dropped, not closed gracefully: over TLS a graceful close of an unread
        # response waits for the provider, past the end of the request's loop.
        aborted.assert_called_once()
        return provider

    def test_abandoned_complete_call_closes_the_provider_stream(self) -> None:
        self.abandon("/agent/complete")

    def test_abandoned_streamed_call_closes_the_provider_stream(self) -> None:
        self.abandon("/agent/stream")


if __name__ == "__main__":
    unittest.main()
