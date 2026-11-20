"""On-demand owned server: a strict request that finds it silent asks the host starter to
power it on and gets an honest answer (starting with Retry-After, or a named refusal with
its measured values: D5). Without a starter nothing changes; standard requests never
reach the starter. The starter here is a fake thread playing the operator service."""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiohttp
from fastapi.testclient import TestClient

from packages import privacy
from packages.owned_server import (
    BUDGET,
    START_LIMIT,
    STARTING,
    UNAVAILABLE,
    OwnedServerUnavailable,
    parse,
)
from packages.user_errors import ui_failure
from serverless_support import environment


def silent_url() -> str:
    """A loopback address where nothing listens (the GPU and its tunnel are down)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


@contextmanager
def strict() -> Iterator[None]:
    token = privacy.elevate(privacy.STRICT)
    try:
        yield
    finally:
        privacy.reset(token)


class FakeStarter:
    """Consumes request files and answers with a fixed status, like the root starter;
    ``then`` is a second status written ``after`` seconds later (a start that fails)."""

    def __init__(
        self,
        folder: Path,
        status: dict[str, Any],
        then: dict[str, Any] | None = None,
        after: float = 0.1,
    ) -> None:
        self.folder, self.status, self.requests = folder, status, 0
        self.then, self.after = then, after
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self) -> None:
        request = self.folder / "request"
        while not self.stop.is_set():
            if request.exists():
                request.unlink()
                self.requests += 1
                now = time.time()
                status = {"updated": now} | self.status
                if status.get("state") == "starting":
                    status.setdefault("since", now)
                self.write(status)
                if self.then is not None:
                    time.sleep(self.after)
                    self.write({"updated": time.time()} | self.then)
            time.sleep(0.02)

    def write(self, status: dict[str, Any]) -> None:
        (self.folder / "status.json.tmp").write_text(json.dumps(status))
        (self.folder / "status.json.tmp").replace(self.folder / "status.json")

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)


STARTING_STATUS = {"state": "starting", "eta_seconds": 120}
BUDGET_STATUS = {
    "state": "refused",
    "reason": "budget",
    "measured": 4.02,
    "limit": 4,
    "unit": "hours",
    "period": "today",
    "retry_after": 7200,
}


class StarterCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            patch.dict(
                os.environ,
                environment()
                | {
                    "ATLAS_OWNED_VLLM_BASE_URL": silent_url(),
                    "ATLAS_OWNED_VLLM_API_KEY": "owned-test-only",
                    "ATLAS_OWNED_SERVER_STARTER_DIR": str(self.folder),
                },
            )
        )
        os.environ.pop("ATLAS_PROVIDER", None)
        self.enterContext(patch("owned_start.ANSWER_SECONDS", 1.0))
        self.enterContext(patch("owned_start.CONFIRM_SECONDS", 0.5))

    def starter(
        self, status: dict[str, Any], then: dict[str, Any] | None = None
    ) -> FakeStarter:
        starter = FakeStarter(self.folder, status, then)
        self.addCleanup(starter.close)
        return starter


class EnsureTests(StarterCase):
    async def test_without_a_starter_nothing_is_probed_or_requested(self) -> None:
        import owned_start

        os.environ.pop("ATLAS_OWNED_SERVER_STARTER_DIR")
        with patch("owned_start.answers", side_effect=AssertionError("probed")):
            await owned_start.ensure()
        self.assertEqual(list(self.folder.iterdir()), [])

    async def test_a_server_that_answers_is_left_alone(self) -> None:
        import owned_start

        async def up() -> bool:
            return True

        with patch("owned_start.answers", up):
            await owned_start.ensure()
        self.assertFalse((self.folder / "request").exists())

    async def test_silent_server_is_started_and_reported_as_starting(self) -> None:
        import owned_start

        starter = self.starter(STARTING_STATUS)
        with self.assertRaises(OwnedServerUnavailable) as raised:
            await owned_start.ensure()
        answer = raised.exception
        self.assertEqual((answer.code, answer.status), (STARTING, 503))
        assert answer.retry_after is not None
        self.assertTrue(100 <= answer.retry_after <= 120, answer.retry_after)
        self.assertEqual(starter.requests, 1)
        self.assertIn("Nothing was sent", answer.message)

    async def test_a_start_in_flight_is_not_requested_again(self) -> None:
        import owned_start

        now = time.time()
        (self.folder / "status.json").write_text(
            json.dumps(
                {"state": "starting", "since": now - 60, "eta_seconds": 120}
                | {"updated": now - 60}
            )
        )
        with self.assertRaises(OwnedServerUnavailable) as raised:
            await owned_start.ensure()
        self.assertEqual(raised.exception.code, STARTING)
        self.assertLessEqual(raised.exception.retry_after or 0, 60)
        self.assertFalse((self.folder / "request").exists())

    async def test_budget_refusal_is_named_with_its_values(self) -> None:
        import owned_start

        self.starter(BUDGET_STATUS)
        with self.assertRaises(OwnedServerUnavailable) as raised:
            await owned_start.ensure()
        answer = raised.exception
        self.assertEqual((answer.code, answer.status), (BUDGET, 429))
        self.assertIn("4.02 hours used of 4 hours (today)", answer.message)
        self.assertNotIn("unavailable", answer.message)
        sentence = ui_failure(answer.code, "fr", answer.fields())
        self.assertEqual(
            sentence,
            "Le mode strict n'utilise que le serveur privé, et sa limite est atteinte : "
            "4.02 h sur 4 h aujourd'hui. Votre message n'a été envoyé nulle part "
            "ailleurs. Réessayez dans environ 2 h.",
        )

    async def test_start_limit_is_named_with_retry_after(self) -> None:
        import owned_start

        self.starter(
            {
                "state": "refused",
                "reason": "start_limit",
                "measured": 4,
                "limit": 4,
                "unit": "starts",
                "period": "hour",
                "retry_after": 900,
            }
        )
        with self.assertRaises(OwnedServerUnavailable) as raised:
            await owned_start.ensure()
        self.assertEqual(
            (
                raised.exception.code,
                raised.exception.status,
                raised.exception.retry_after,
            ),
            (START_LIMIT, 429, 900),
        )
        self.assertIn(
            "4 fois", ui_failure(START_LIMIT, "fr", raised.exception.fields()) or ""
        )

    async def test_a_silent_starter_leaves_the_usual_error(self) -> None:
        import owned_start

        with (
            patch("owned_start.ANSWER_SECONDS", 0.3),
            self.assertLogs("owned_start", level="WARNING") as logs,
        ):
            await owned_start.ensure()
        self.assertIn("owned_server_starter_silent", logs.output[0])
        self.assertTrue((self.folder / "request").exists())

    async def test_a_ready_answer_leaves_the_usual_error(self) -> None:
        import owned_start

        starter = self.starter({"state": "ready"})
        await owned_start.ensure()
        starter.close()

    async def test_a_start_that_fails_is_never_reported_as_starting(self) -> None:
        import owned_start

        # The starter says "starting", then the cloud refuses for lack of stock.
        starter = self.starter(
            STARTING_STATUS, {"state": "failed", "exit_code": 1, "reason": "capacity"}
        )
        with self.assertRaises(OwnedServerUnavailable) as raised:
            await owned_start.ensure()
        answer = raised.exception
        self.assertEqual((answer.code, answer.status), (UNAVAILABLE, 503))
        self.assertEqual(answer.fields()["reason"], "capacity")
        self.assertNotIn("starting", answer.message)
        sentence = ui_failure(answer.code, "fr", answer.fields()) or ""
        self.assertIn("ne peut pas démarrer", sentence)
        self.assertNotIn("démarre (", sentence)
        # The failure is held: the next request reads it and asks for no new start.
        with self.assertRaises(OwnedServerUnavailable) as again:
            await owned_start.ensure()
        self.assertEqual(again.exception.code, UNAVAILABLE)
        self.assertEqual(starter.requests, 1)

    async def test_a_start_in_flight_is_confirmed_before_starting_is_said(
        self,
    ) -> None:
        import owned_start

        # Another strict call of the same turn asked for the start a moment ago; the
        # cloud then refuses it. This call must read the refusal, not "starting".
        now = time.time()
        (self.folder / "status.json").write_text(
            json.dumps({"state": "starting", "since": now, "updated": now})
        )

        def refuse() -> None:
            time.sleep(0.2)
            (self.folder / "status.json").write_text(
                json.dumps(
                    {"state": "failed", "updated": time.time(), "reason": "capacity"}
                )
            )

        threading.Thread(target=refuse, daemon=True).start()
        with self.assertRaises(OwnedServerUnavailable) as raised:
            await owned_start.ensure()
        self.assertEqual(raised.exception.code, UNAVAILABLE)
        self.assertFalse((self.folder / "request").exists())

    async def test_an_old_failure_lets_a_new_start_be_tried(self) -> None:
        import owned_start

        old = time.time() - owned_start.FAILED_HOLD_SECONDS - 5
        (self.folder / "status.json").write_text(
            json.dumps({"state": "failed", "updated": old, "exit_code": 1})
        )
        starter = self.starter(STARTING_STATUS)
        with self.assertRaises(OwnedServerUnavailable) as raised:
            await owned_start.ensure()
        self.assertEqual(raised.exception.code, STARTING)
        self.assertEqual(starter.requests, 1)

    async def test_a_failure_without_reason_is_a_failed_start(self) -> None:
        import owned_start

        self.starter({"state": "failed", "exit_code": 1})
        with self.assertRaises(OwnedServerUnavailable) as raised:
            await owned_start.ensure()
        self.assertEqual(raised.exception.fields()["reason"], "start_failed")

    def test_a_pending_request_is_not_rewritten(self) -> None:
        import owned_start

        (self.folder / "request").write_text("1\n")
        self.assertIsNotNone(owned_start.request_start(self.folder))
        self.assertEqual((self.folder / "request").read_text(), "1\n")


class SessionDown:
    """An aiohttp session whose connection is refused (records each call)."""

    calls = 0

    def __init__(self, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> "SessionDown":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def post(self, url: str, **kwargs: object) -> Any:
        SessionDown.calls += 1
        raise aiohttp.ClientConnectionError("refused")


class GatewayTests(StarterCase):
    async def test_strict_stream_call_gets_the_starter_answer(self) -> None:
        from agent_provider import AgentRequest
        from stream_transport import attempt

        starter = self.starter(STARTING_STATUS)
        with strict():
            request = AgentRequest(
                messages=[{"role": "user", "content": "Bonjour"}], tools=[], timeout=5
            )
            self.assertEqual(request.privacy, "strict")
            with self.assertRaises(OwnedServerUnavailable) as raised:
                _ = [event async for event in attempt(request, "owned-vllm-model", 5)]
        self.assertEqual(raised.exception.code, STARTING)
        self.assertEqual(starter.requests, 1)

    async def test_an_auxiliary_call_never_powers_the_server_on(self) -> None:
        from agent_provider import AgentProvider, AgentRequest
        from stream_transport import attempt

        starter = self.starter(STARTING_STATUS)
        with strict():
            request = AgentRequest(
                messages=[{"role": "user", "content": "Bonjour"}],
                tools=[],
                timeout=5,
                start_owned=False,
            )
            with self.assertRaises(Exception) as raised:
                _ = [event async for event in attempt(request, "owned-vllm-model", 5)]
            self.assertNotIsInstance(raised.exception, OwnedServerUnavailable)
            with self.assertRaises(Exception) as raised:
                await AgentProvider().owned_complete(request)
            self.assertNotIsInstance(raised.exception, OwnedServerUnavailable)
        self.assertEqual(starter.requests, 0)
        # The default (a chat call may power the server on) stays off the wire.
        usual = AgentRequest(
            messages=[{"role": "user", "content": "Bonjour"}], tools=[], timeout=5
        )
        self.assertTrue(usual.start_owned)
        self.assertNotIn("start_owned", usual.model_dump())

    async def test_standard_call_never_reaches_the_starter(self) -> None:
        from agent_provider import AgentRequest
        from serverless import ServerlessPolicy
        from stream_transport import attempt

        starter = self.starter(STARTING_STATUS)

        async def never() -> None:
            raise AssertionError("standard traffic reached the starter")

        with (
            patch("owned_start.ensure", never),
            patch("stream_transport.aiohttp.ClientSession", SessionDown),
        ):
            request = AgentRequest(
                messages=[{"role": "user", "content": "Bonjour"}], tools=[], timeout=5
            )
            self.assertEqual(request.privacy, "standard")
            with self.assertRaises(RuntimeError) as raised:
                _ = [
                    event
                    async for event in attempt(
                        request, ServerlessPolicy().models["text"], 5
                    )
                ]
        self.assertEqual(str(raised.exception), "provider_error")
        self.assertGreater(SessionDown.calls, 0)
        self.assertEqual(starter.requests, 0)
        self.assertFalse((self.folder / "request").exists())

    async def test_gateway_boundary_names_the_answer_for_the_orchestrator(self) -> None:
        from services.orchestrator.loop import GatewayError, OwnedServerAnswer
        from services.orchestrator.model import GatewayModel
        from services.orchestrator.stream_client import receive
        from test_strict_privacy_egress import GatewayServer

        gateway = GatewayServer()
        self.addCleanup(gateway.close)
        self.starter(STARTING_STATUS)
        payload = {
            "messages": [{"role": "user", "content": "Bonjour"}],
            "tools": [],
            "timeout": 5,
        }

        async def sink(event: dict[str, object]) -> None:
            raise AssertionError("no delta expected")

        with strict():
            with self.assertRaises(OwnedServerAnswer) as streamed:
                await receive(gateway.url + "/agent/stream", payload, 30, sink)
            # The boundary itself; GatewayModel.post waits (test_owned_server_wait.py).
            with self.assertRaises(OwnedServerAnswer) as completed:
                await GatewayModel.send(gateway.url + "/agent/complete", payload, 30)
        for raised in (streamed.exception, completed.exception):
            self.assertIsInstance(raised, GatewayError)
            self.assertEqual((raised.code, raised.status), (STARTING, 503))
            self.assertIsNotNone(raised.retry_after)
            self.assertIn(
                "Le serveur privé démarre (environ 2 min)",
                ui_failure(raised.code, "fr", raised.measurements) or "",
            )


class ApiCase(StarterCase):
    def setUp(self) -> None:
        super().setUp()
        from services.orchestrator.api_keys import Store
        from services.orchestrator.chat_api import app

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = Store(root / "keys.sqlite", "fictional-pepper-" * 4, time.time)
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_DATA_DIR": str(root),
                    "ATLAS_INTERACTION_DIR": str(root / "chat-log"),
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                    "ATLAS_PRIVACY_MINIMUM": "",
                    "ATLAS_HIDDEN_PROFILES": "atlas-mistral",
                    "MISTRAL_API_KEY": "test-only",
                },
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=store,
            )
        )
        self.client = self.enterContext(TestClient(app))
        _, secret = store.create(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="owned-start",
            scopes=["model"],
            created_by="operator",
            daily_requests=100,
            daily_micro_eur=1000000,
        )
        self.key = {"Authorization": "Bearer " + secret}

    def chat(
        self, level: str, stream: bool = False, headers: dict[str, str] | None = None
    ) -> Any:
        return self.client.post(
            "/api/atlas/v1/chat/completions",
            headers=self.key | {privacy.HEADER: level} | (headers or {}),
            json={
                "model": "atlas-chat",
                "messages": [{"role": "user", "content": "Bonjour"}],
                "max_tokens": 50,
                "stream": stream,
            },
        )


class ApiTests(ApiCase):
    def test_strict_request_starts_the_server_and_gets_503_retry_after(self) -> None:
        starter = self.starter(STARTING_STATUS)
        for stream in (False, True, False):
            # A client that accepts no wait (test_owned_server_wait.py: the wait).
            reply = self.chat("strict", stream, headers={"X-Atlas-Owned-Wait": "0"})
            self.assertEqual(reply.status_code, 503, reply.text)
            error = reply.json()["error"]
            self.assertEqual(error["type"], STARTING)
            self.assertEqual(error["retry_after"], int(reply.headers["Retry-After"]))
            self.assertTrue(0 < int(reply.headers["Retry-After"]) <= 120)
        # One start for the three requests: the next ones read the status.
        self.assertEqual(starter.requests, 1)

    def test_budget_refusal_is_a_named_429_never_an_outage(self) -> None:
        self.starter(BUDGET_STATUS)
        reply = self.chat("strict")
        self.assertEqual(reply.status_code, 429, reply.text)
        error = reply.json()["error"]
        self.assertEqual(
            (error["type"], error["measured"], error["limit"], error["unit"]),
            (BUDGET, 4.02, 4, "hours"),
        )

    def test_standard_request_never_reaches_the_starter(self) -> None:
        from provider_retry import TransientProviderError

        starter = self.starter(STARTING_STATUS)
        sent: list[str] = []

        async def down(plan: Any) -> AsyncIterator[bytes]:
            sent.append(plan.privacy)
            raise TransientProviderError("provider_error", status=503)
            yield b""

        async def never() -> None:
            raise AssertionError("standard traffic reached the starter")

        with (
            patch("dev_gateway.transport", down),
            patch("owned_start.ensure", never),
        ):
            reply = self.chat("standard")
        self.assertEqual(reply.status_code, 503, reply.text)
        self.assertEqual(reply.json()["error"]["type"], "provider_unavailable")
        self.assertTrue(sent)
        self.assertNotIn("owned", sent)
        self.assertEqual(starter.requests, 0)


class WireTests(unittest.TestCase):
    def test_parse_accepts_only_known_codes_and_numbers(self) -> None:
        self.assertIsNone(parse({"code": "provider_error"}))
        self.assertIsNone(parse("owned_server_starting"))
        answer = parse(
            {"code": STARTING, "retry_after": "soon", "measured": True, "unit": "x"}
        )
        assert answer is not None
        self.assertEqual(answer.fields(), {"code": STARTING})

    def test_every_language_has_its_sentences(self) -> None:
        for language in ("fr", "en", "de", "es", "it"):
            for code, values in (
                (STARTING, {"retry_after": 115}),
                (BUDGET, BUDGET_STATUS),
                (START_LIMIT, {"measured": 4, "limit": 4, "retry_after": 600}),
                (UNAVAILABLE, {"reason": "capacity", "retry_after": 300}),
                (UNAVAILABLE, {"reason": "start_failed"}),
            ):
                with self.subTest(language=language, code=code):
                    sentence = ui_failure(code, language, values)
                    assert sentence is not None
                    self.assertNotIn("{", sentence)
                    self.assertNotIn(" .", sentence)


if __name__ == "__main__":
    unittest.main()
