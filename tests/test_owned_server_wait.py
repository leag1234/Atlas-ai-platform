"""Strict privacy: a turn waits for the on-demand owned server instead of failing.

Staging 2026-11-20: a strict question with the owned GPU off showed "Le serveur privé
démarre… Renvoyez-le dans un instant." under "The response was interrupted." and a
Retry button. The turn now waits with a live status and answers once the server is
up; a refusal or a start that does not finish ends with a localized answer, never an
interface error, and nothing is ever sent to a non-owned provider.
"""

import asyncio
import json
import os
import threading
import time
import unittest
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from packages import privacy
from packages.owned_server import (
    BUDGET,
    START_LIMIT,
    STARTING,
    UNAVAILABLE,
    OwnedServerUnavailable,
)
from packages.user_errors import ui_failure
from test_owned_server_start import (
    STARTING_STATUS,
    ApiCase,
    StarterCase,
    strict,
)


def starting(retry_after: int = 110) -> OwnedServerUnavailable:
    return OwnedServerUnavailable(STARTING, retry_after=retry_after)


def budget() -> OwnedServerUnavailable:
    return OwnedServerUnavailable(
        BUDGET,
        retry_after=7200,
        measured=4.02,
        limit=4,
        unit="hours",
        period="today",
    )


def scripted(
    *answers: OwnedServerUnavailable | None,
) -> tuple[Callable[[], Awaitable[None]], list[int]]:
    """An owned_start.ensure that raises each answer in turn; None: the server is up."""
    queue = list(answers)
    calls = [0]

    async def ensure() -> None:
        calls[0] += 1
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if answer is not None:
            raise answer

    return ensure, calls


class WaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(patch("services.orchestrator.owned_wait.POLL_SECONDS", 0.05))

    async def attempt_sequence(
        self, *results: OwnedServerUnavailable | str
    ) -> tuple[Callable[[], Awaitable[str]], list[int]]:
        from services.orchestrator.loop import OwnedServerAnswer

        queue = list(results)
        calls = [0]

        async def attempt() -> str:
            calls[0] += 1
            result = queue.pop(0)
            if isinstance(result, OwnedServerUnavailable):
                raise OwnedServerAnswer(result)
            return result

        return attempt, calls

    async def test_cold_start_shows_status_then_makes_the_same_call_again(
        self,
    ) -> None:
        from services.orchestrator import owned_wait

        events: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        attempt, attempts = await self.attempt_sequence(starting(), "answer")
        ensure, polls = scripted(starting(100), starting(90), None)
        with patch("owned_start.ensure", ensure):
            self.assertEqual(await owned_wait.call(attempt, sink), "answer")
        self.assertEqual(attempts[0], 2)
        self.assertEqual(polls[0], 3)
        phases = [event["phase"] for event in events]
        self.assertEqual(phases[-1], "owned_server_ready")
        self.assertEqual(set(phases[:-1]), {"owned_server_starting"})
        self.assertGreaterEqual(len(phases), 3)
        waited = [float(cast(float, e["waited_seconds"])) for e in events[:-1]]
        self.assertEqual(waited, sorted(waited))
        self.assertTrue(all(cast(int, e["expected_seconds"]) > 0 for e in events[:-1]))

    async def test_a_refusal_during_the_wait_is_named_at_once(self) -> None:
        from services.orchestrator import owned_wait
        from services.orchestrator.loop import OwnedServerAnswer

        for refusal in (
            budget(),
            OwnedServerUnavailable(START_LIMIT, measured=4, limit=4, unit="starts"),
            OwnedServerUnavailable(UNAVAILABLE, reason="capacity", retry_after=300),
        ):
            with self.subTest(code=refusal.code):
                attempt, attempts = await self.attempt_sequence(starting(), "answer")
                ensure, _ = scripted(starting(), refusal)
                with patch("owned_start.ensure", ensure):
                    with self.assertRaises(OwnedServerAnswer) as raised:
                        await owned_wait.call(attempt, AsyncMock())
                self.assertEqual(raised.exception.code, refusal.code)
                self.assertEqual(attempts[0], 1)
                sentence = ui_failure(
                    raised.exception.code, "fr", raised.exception.measurements
                )
                self.assertIn("nulle part ailleurs", sentence or "")

    async def test_still_starting_after_the_limit_says_so_in_the_language(
        self,
    ) -> None:
        from services.orchestrator import owned_wait
        from services.orchestrator.loop import OwnedServerAnswer

        attempt, attempts = await self.attempt_sequence(starting(), "answer")
        ensure, _ = scripted(starting())
        with patch("owned_start.ensure", ensure):
            with self.assertRaises(OwnedServerAnswer) as raised:
                await owned_wait.call(attempt, AsyncMock(), limit=0.3)
        self.assertEqual(attempts[0], 1)
        exc = raised.exception
        self.assertEqual((exc.code, exc.status), (STARTING, 503))
        self.assertEqual(exc.measurements["waited"], 1)
        french = ui_failure(exc.code, "fr", exc.measurements) or ""
        self.assertIn("toujours pas prêt après 1 min", french)
        self.assertNotIn("Renvoyez-le dans un instant", french)
        for language in ("en", "de", "es", "it"):
            self.assertNotEqual(
                ui_failure(exc.code, language, exc.measurements), french
            )

    async def test_a_refusal_without_a_start_is_not_waited_for(self) -> None:
        from services.orchestrator import owned_wait
        from services.orchestrator.loop import OwnedServerAnswer

        attempt, attempts = await self.attempt_sequence(budget())
        ensure, polls = scripted(None)
        with patch("owned_start.ensure", ensure):
            with self.assertRaises(OwnedServerAnswer) as raised:
                await owned_wait.call(attempt, AsyncMock())
        self.assertEqual(raised.exception.code, BUDGET)
        self.assertEqual((attempts[0], polls[0]), (1, 0))

    async def test_a_client_that_accepts_no_wait_gets_starting_at_once(self) -> None:
        from services.orchestrator import owned_wait
        from services.orchestrator.loop import OwnedServerAnswer

        attempt, attempts = await self.attempt_sequence(starting(), "answer")
        ensure, polls = scripted(None)
        token = owned_wait.accepted.set(0)
        try:
            with patch("owned_start.ensure", ensure):
                with self.assertRaises(OwnedServerAnswer) as raised:
                    await owned_wait.call(attempt, AsyncMock())
        finally:
            owned_wait.accepted.reset(token)
        self.assertEqual(raised.exception.code, STARTING)
        self.assertNotIn("waited", raised.exception.measurements)
        self.assertEqual((attempts[0], polls[0]), (1, 0))

    async def test_the_turn_deadline_moves_by_the_wait(self) -> None:
        from services.orchestrator import owned_wait
        from services.orchestrator.deadline import request_deadline

        attempt, _ = await self.attempt_sequence(starting(), "answer")
        ensure, _ = scripted(*([starting()] * 8), None)
        started = time.monotonic()
        with patch("owned_start.ensure", ensure):
            async with request_deadline(0.15):
                self.assertEqual(await owned_wait.call(attempt, AsyncMock()), "answer")
        self.assertGreater(time.monotonic() - started, 0.3)

    def test_status_text_counts_the_wait_in_every_language(self) -> None:
        from services.orchestrator.owned_wait import status_text

        self.assertEqual(
            status_text("fr", 45.4, 120), "Démarrage du serveur privé… 45 s / ~2 min"
        )
        self.assertEqual(
            status_text("en", 130, 125), "Starting the private server… 130 s / ~3 min"
        )
        for language in ("de", "es", "it"):
            self.assertNotIn("Starting", status_text(language, 5, 120))

    def test_requested_wait_header(self) -> None:
        from services.orchestrator.owned_wait import WAIT_SECONDS, requested_wait

        for value, expected in (
            (None, WAIT_SECONDS),
            ("0", 0),
            ("30", 30),
            ("100000", WAIT_SECONDS),
            ("-5", 0),
            ("soon", WAIT_SECONDS),
        ):
            headers = {} if value is None else {"X-Atlas-Owned-Wait": value}
            self.assertEqual(requested_wait(headers), expected, value)


class ChatStreamTests(unittest.IsolatedAsyncioTestCase):
    """The chat interface's stream: status lines while waiting, then the answer; a
    refusal is the turn's text, never an error frame (which the interface shows as an
    interrupted answer with a Retry button)."""

    async def frames(
        self, process: Callable[[Any, Any], Awaitable[None]], guest: bool = True
    ) -> list[dict[str, Any]]:
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.chat_stream import response
        from services.orchestrator.interactions import Interaction

        with (
            patch.dict(
                os.environ,
                {
                    "ATLAS_STABLE": "1" if guest else "0",
                    "ATLAS_INTERACTION_DIR": "/dev/null",
                },
            ),
            patch("services.orchestrator.chat_stream.write_interaction"),
        ):
            request = ChatRequest.model_validate(
                {
                    "ui_locale": "fr",
                    "atlas_ui": True,
                    "messages": [
                        {
                            "role": "user",
                            "content": "Donne-moi des infos sur le cycle de l'eau",
                        }
                    ],
                }
            )
            stream = response(request, Interaction(), time.monotonic(), process)
            iterator = cast(AsyncGenerator[str, None], stream.body_iterator)
            frames = []
            async for frame in iterator:
                text = str(frame)
                if text.startswith("data: {"):
                    frames.append(json.loads(text.removeprefix("data: ")))
            return frames

    @staticmethod
    def statuses(frames: list[dict[str, Any]]) -> list[str]:
        return [
            str(frame["event"]["data"]["description"])
            for frame in frames
            if isinstance(frame.get("event"), dict)
            and frame["event"].get("type") == "status"
        ]

    @staticmethod
    def text(frames: list[dict[str, Any]]) -> str:
        return "".join(
            str(choice.get("delta", {}).get("content") or "")
            for frame in frames
            for choice in frame.get("choices", [])
        )

    async def test_waiting_turn_shows_a_counting_status_then_answers(self) -> None:
        from services.orchestrator.stream_client import sink_context

        async def process(payload: Any, item: Any) -> None:
            sink = sink_context.get()
            assert sink is not None
            await sink({"phase": "generating", "turn": 1})
            await sink(
                {
                    "phase": "owned_server_starting",
                    "waited_seconds": 0.0,
                    "expected_seconds": 120,
                }
            )
            await asyncio.sleep(2.2)
            await sink({"phase": "owned_server_ready"})
            await sink({"delta": {"content": "Le cycle de l'eau décrit…"}})
            item.reponse = "Le cycle de l'eau décrit…"
            item.state = "done"

        frames = await self.frames(process)
        statuses = self.statuses(frames)
        waiting = [s for s in statuses if s.startswith("Démarrage du serveur privé…")]
        self.assertGreaterEqual(len(waiting), 2, statuses)
        self.assertTrue(all(s.endswith("/ ~2 min") for s in waiting), waiting)
        self.assertIn("Démarrage du serveur privé… 2 s / ~2 min", waiting)
        self.assertIn("Le cycle de l'eau décrit…", self.text(frames))
        self.assertFalse(any("error" in frame for frame in frames))

    async def test_refusal_is_the_answer_text_never_an_error_frame(self) -> None:
        from services.orchestrator.loop import OwnedServerAnswer

        for answer, expected in (
            (budget(), "sa limite est atteinte : 4.02 h sur 4 h aujourd'hui"),
            (
                OwnedServerUnavailable(STARTING, retry_after=20, waited=240),
                "toujours pas prêt après 4 min",
            ),
            (
                OwnedServerUnavailable(UNAVAILABLE, reason="capacity"),
                "l'hébergeur n'a plus de machine",
            ),
        ):
            with self.subTest(code=answer.code):

                async def process(payload: Any, item: Any) -> None:
                    raise OwnedServerAnswer(answer)

                frames = await self.frames(process)
                self.assertFalse(any("error" in frame for frame in frames), frames)
                text = self.text(frames)
                self.assertIn(expected, text)
                self.assertIn("Votre message n'a été envoyé nulle part ailleurs", text)
                finish = [
                    choice.get("finish_reason")
                    for frame in frames
                    for choice in frame.get("choices", [])
                ]
                self.assertIn("stop", finish)


class FakeOwnedServer:
    """An OpenAI-compatible owned server that answers once it is "powered on"."""

    def __init__(self, port: int) -> None:
        self.paths: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_GET(self) -> None:
                owner.paths.append(self.path)
                body = json.dumps({"data": [{"id": "owned-model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                owner.paths.append(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                chunks = [
                    {
                        "id": "owned-1",
                        "object": "chat.completion.chunk",
                        "model": request.get("model"),
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "Bonjour !"},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "owned-1",
                        "object": "chat.completion.chunk",
                        "model": request.get("model"),
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 3,
                            "total_tokens": 15,
                        },
                    },
                ]
                if request.get("stream"):
                    body = (
                        "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                        + "data: [DONE]\n\n"
                    ).encode()
                    kind = "text/event-stream"
                else:
                    body = json.dumps(
                        {
                            "id": "owned-1",
                            "object": "chat.completion",
                            "model": request.get("model"),
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {
                                        "role": "assistant",
                                        "content": "Bonjour !",
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": chunks[1]["usage"],
                        }
                    ).encode()
                    kind = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


class GatewayWaitTests(StarterCase):
    """Through the real gateway: the strict call finds the server off, the start is
    requested, the turn waits, and the same call is answered by the owned server."""

    async def test_cold_start_then_answer_from_the_owned_server_only(self) -> None:
        from urllib.parse import urlparse

        from test_strict_privacy_egress import GatewayServer

        self.enterContext(patch("services.orchestrator.owned_wait.POLL_SECONDS", 0.1))
        self.enterContext(patch("services.orchestrator.owned_wait.WAIT_SECONDS", 20))
        gateway = GatewayServer()
        self.addCleanup(gateway.close)
        starter = self.starter(STARTING_STATUS)
        port = cast(int, urlparse(os.environ["ATLAS_OWNED_VLLM_BASE_URL"]).port)
        owned: list[FakeOwnedServer] = []
        events: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)
            starting = [e for e in events if e.get("phase") == "owned_server_starting"]
            if len(starting) == 3 and not owned:
                # The GPU and its tunnel come up while the turn waits.
                owned.append(FakeOwnedServer(port))
                self.addCleanup(owned[0].close)

        payload = {
            "messages": [{"role": "user", "content": "Bonjour"}],
            "tools": [],
            "timeout": 30,
        }
        from services.orchestrator import owned_wait
        from services.orchestrator.stream_client import receive

        with strict():
            wire = await owned_wait.call(
                lambda: receive(gateway.url + "/agent/stream", payload, 30, sink),
                sink,
            )
        self.assertEqual(wire["text"], "Bonjour !")
        self.assertEqual(starter.requests, 1)
        self.assertIn("/v1/chat/completions", owned[0].paths)
        phases = [e.get("phase") for e in events if "phase" in e]
        self.assertIn("owned_server_ready", phases)
        self.assertEqual(
            [e for e in events if "delta" in e][0]["delta"], {"content": "Bonjour !"}
        )


class ApiWaitTests(ApiCase):
    """The developer API waits too: a streamed request keeps its stream alive with SSE
    comments; X-Atlas-Owned-Wait: 0 keeps the immediate 503 with Retry-After."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch("services.orchestrator.owned_wait.POLL_SECONDS", 0.05))
        # Bounded: a wait that never ends fails the test instead of hanging it.
        self.enterContext(patch("services.orchestrator.owned_wait.WAIT_SECONDS", 20))

    def owned_transport(self, sent: list[str]) -> Callable[[Any], AsyncIterator[bytes]]:
        async def transport(plan: Any) -> AsyncIterator[bytes]:
            sent.append(plan.privacy)
            for chunk in (
                {
                    "id": "owned-1",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "Bonjour !"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "owned-1",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                },
            ):
                yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

        return transport

    def test_streamed_request_waits_with_keepalive_then_answers(self) -> None:
        sent: list[str] = []
        ensure, polls = scripted(starting(), starting(), starting(), None)
        with (
            patch("owned_start.ensure", ensure),
            patch("dev_gateway.transport", self.owned_transport(sent)),
        ):
            reply = self.chat("strict", True)
        self.assertEqual(reply.status_code, 200, reply.text)
        body = reply.text
        self.assertTrue(body.startswith(": owned server starting\n\n"), body[:200])
        self.assertGreaterEqual(body.count(": owned server starting"), 2)
        self.assertIn("Bonjour !", body)
        self.assertTrue(body.rstrip().endswith("data: [DONE]"))
        self.assertNotIn('"error"', body)
        # Only the owned server was called, once the start was over.
        self.assertEqual(sent, ["owned"])
        self.assertGreaterEqual(polls[0], 4)

    def test_streamed_request_ends_with_the_named_refusal(self) -> None:
        ensure, _ = scripted(starting(), budget())
        with patch("owned_start.ensure", ensure):
            reply = self.chat("strict", True)
        self.assertEqual(reply.status_code, 200, reply.text)
        frames = [
            json.loads(line.removeprefix("data: "))
            for line in reply.text.splitlines()
            if line.startswith("data: {")
        ]
        self.assertEqual(frames[-1]["error"]["type"], BUDGET)
        self.assertEqual(frames[-1]["error"]["measured"], 4.02)
        self.assertTrue(reply.text.rstrip().endswith("data: [DONE]"))

    def test_streamed_responses_request_keeps_the_owned_code(self) -> None:
        ensure, _ = scripted(starting(), budget())
        with patch("owned_start.ensure", ensure):
            reply = self.client.post(
                "/api/atlas/v1/responses",
                headers=self.key | {privacy.HEADER: "strict"},
                json={"model": "atlas-chat", "input": "Bonjour", "stream": True},
            )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertIn("event: error", reply.text)
        self.assertIn('"code": "owned_server_budget"', reply.text)
        self.assertIn("event: response.failed", reply.text)

    def test_non_streamed_request_waits_then_answers(self) -> None:
        sent: list[str] = []
        ensure, _ = scripted(starting(), starting(), None)
        with (
            patch("owned_start.ensure", ensure),
            patch("dev_gateway.transport", self.owned_transport(sent)),
        ):
            reply = self.chat("strict")
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["choices"][0]["message"]["content"], "Bonjour !")
        self.assertEqual(sent, ["owned"])

    def test_non_streamed_request_still_starting_after_its_wait(self) -> None:
        ensure, _ = scripted(starting())
        with patch("owned_start.ensure", ensure):
            reply = self.chat("strict", headers={"X-Atlas-Owned-Wait": "1"})
        self.assertEqual(reply.status_code, 503, reply.text)
        error = reply.json()["error"]
        self.assertEqual((error["type"], error["waited"]), (STARTING, 1))
        self.assertIn("Retry-After", reply.headers)
