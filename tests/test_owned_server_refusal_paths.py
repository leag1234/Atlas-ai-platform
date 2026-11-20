"""Strict privacy: a refusal of the owned server reaches every client by name.

Staging 2026-11-20 22:57 (battery on stable-20261111-141): with the owned L4 at its daily
cap and the host's starter service stopped, a strict API request to the assistant got
502 request_stopped with the loop's internal summary ("Explicit stop: provider_error.
Tools executed: 0..."), where the same refusal had been a named 429 owned_server_budget.
The assistant on the Responses and Messages formats answered an empty completed response
for the same refusal.

Here, through the real gateway and the real API edge, with a fake starter playing the
operator service: the cap refusal and a silent starter, for the assistant (``atlas``)
and a direct model (``atlas-chat``), on Chat Completions, Responses and Messages,
streamed and not streamed; and the chat interface's turn, which reads one sentence in
the conversation's language as its answer.
"""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from packages import privacy
from packages.owned_server import BUDGET, UNAVAILABLE
from test_owned_server_start import BUDGET_STATUS, ApiCase

QUESTION = "Quelle est la capitale de l'Australie ? Réponds en une phrase."
# Text written for the logs that must never reach a client (battery raw-text check).
RAW = (
    "Explicit stop",
    "Tools executed",
    "Tokens reserved",
    "Cost reserved",
    "provider_error",
    "request_stopped",
    "Traceback",
)
FORMATS = ("chat", "responses", "messages")
MODELS = ("atlas", "atlas-chat")


class RefusalPaths(ApiCase):
    def setUp(self) -> None:
        super().setUp()
        from services.orchestrator.api_keys import Store
        from test_strict_privacy_egress import GatewayServer

        gateway = GatewayServer()
        self.addCleanup(gateway.close)
        self.enterContext(patch.dict(os.environ, {"ATLAS_GATEWAY_URL": gateway.url}))
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = Store(root / "keys.sqlite", "fictional-pepper-" * 4, time.time)
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=store,
            )
        )
        _, secret = store.create(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="owned-refusal",
            scopes=["model", "assistant"],
            created_by="operator",
            daily_requests=200,
            daily_micro_eur=1000000,
        )
        self.key = {"Authorization": "Bearer " + secret}

    def send(self, model: str, protocol: str, stream: bool) -> Any:
        body: dict[str, Any] = {"model": model, "stream": stream}
        if protocol == "responses":
            path, body["input"] = "/api/atlas/v1/responses", QUESTION
        else:
            path = (
                "/api/atlas/v1/chat/completions"
                if protocol == "chat"
                else "/api/atlas/v1/messages"
            )
            body["messages"] = [{"role": "user", "content": QUESTION}]
            if protocol == "messages":
                body["max_tokens"] = 200
        return self.client.post(
            path, headers=self.key | {privacy.HEADER: "strict"}, json=body
        )

    @staticmethod
    def error_of(protocol: str, value: dict[str, Any]) -> dict[str, Any]:
        """The error object of a non-streamed reply or of a stream's error frame."""
        if protocol == "messages":
            assert value.get("type") == "error", value
        error = value.get("error")
        assert isinstance(error, dict), value
        return error

    @staticmethod
    def stream_errors(protocol: str, text: str) -> list[dict[str, Any]]:
        errors = []
        for line in text.splitlines():
            if not line.startswith("data: {"):
                continue
            frame = json.loads(line.removeprefix("data: "))
            if protocol == "chat" and isinstance(frame.get("error"), dict):
                errors.append(frame["error"])
            elif protocol != "chat" and frame.get("type") == "error":
                # Responses: {"type": "error", "code", "message"}; Messages:
                # {"type": "error", "error": {...}}.
                errors.append(frame.get("error") or frame)
        return errors

    def check_every_path(self, code: str, status: int, values: dict[str, Any]) -> None:
        for model in MODELS:
            for protocol in FORMATS:
                for stream in (False, True):
                    with self.subTest(model=model, protocol=protocol, stream=stream):
                        reply = self.send(model, protocol, stream)
                        for raw in RAW:
                            self.assertNotIn(raw, reply.text)
                        if reply.status_code == 200:
                            # A stream already open: the format's error event.
                            self.assertTrue(stream, reply.text[:300])
                            errors = self.stream_errors(protocol, reply.text)
                            self.assertTrue(errors, reply.text[-600:])
                            found = errors[-1]
                            self.assertEqual(found.get("code"), code, found)
                            self.assertIn("Nothing was sent", found.get("message", ""))
                            if protocol == "responses":
                                self.assertIn("event: response.failed", reply.text)
                            if protocol == "chat":
                                self.assertTrue(
                                    reply.text.rstrip().endswith("data: [DONE]")
                                )
                            continue
                        self.assertEqual(reply.status_code, status, reply.text[:400])
                        self.assertIn("Retry-After", reply.headers)
                        error = self.error_of(protocol, reply.json())
                        self.assertEqual(error["code"], code, error)
                        if protocol != "messages":
                            self.assertEqual(error["type"], code, error)
                        self.assertIn("Nothing was sent", error["message"])
                        for name, value in values.items():
                            self.assertEqual(error.get(name), value, error)

    def test_cap_reached_is_named_on_every_api_path(self) -> None:
        starter = self.starter(BUDGET_STATUS)
        self.check_every_path(
            BUDGET, 429, {"measured": 4.02, "limit": 4, "unit": "hours"}
        )
        # Twelve requests, several strict calls each: one start request, the
        # refusal is then read from the starter's status (owned_start.py).
        self.assertEqual(starter.requests, 1)

    def test_silent_starter_is_named_on_every_api_path(self) -> None:
        self.enterContext(patch("owned_start.ANSWER_SECONDS", 0.2))
        self.check_every_path(UNAVAILABLE, 503, {"reason": "starter_unavailable"})

    def interface_turn(self) -> tuple[list[dict[str, Any]], str]:
        """A strict turn of the chat interface (streamed, atlas_ui), in French."""
        from packages.profiles import default_profile

        with patch(
            "services.orchestrator.conversation_privacy.bind",
            return_value=privacy.STRICT,
        ):
            reply = self.client.post(
                "/v1/chat/completions",
                headers={"X-OpenWebUI-User-Id": "fictional-interface-user"},
                json={
                    "model": default_profile(),
                    "stream": True,
                    "atlas_ui": True,
                    "ui_locale": "fr",
                    "messages": [{"role": "user", "content": QUESTION}],
                },
            )
        self.assertEqual(reply.status_code, 200, reply.text[:300])
        frames = [
            json.loads(line.removeprefix("data: "))
            for line in reply.text.splitlines()
            if line.startswith("data: {")
        ]
        text = "".join(
            str(choice.get("delta", {}).get("content") or "")
            for frame in frames
            for choice in frame.get("choices", [])
        )
        return frames, text

    def check_interface(self, expected: str) -> None:
        frames, text = self.interface_turn()
        self.assertFalse([f for f in frames if "error" in f], frames[-3:])
        self.assertIn(expected, text)
        self.assertIn("Votre message n'a été envoyé nulle part ailleurs", text)
        for raw in RAW:
            self.assertNotIn(raw, text)
        finish = [
            choice.get("finish_reason")
            for frame in frames
            for choice in frame.get("choices", [])
        ]
        self.assertIn("stop", finish)

    def test_interface_turn_reads_the_cap_as_its_answer(self) -> None:
        self.starter(BUDGET_STATUS)
        self.check_interface("sa limite est atteinte : 4.02 h sur 4 h aujourd'hui")

    def test_interface_turn_reads_a_silent_starter_as_its_answer(self) -> None:
        self.enterContext(patch("owned_start.ANSWER_SECONDS", 0.2))
        self.check_interface("son service de démarrage ne répond pas")
