"""Polite closings after a delivery are answered in the chat (staging 2026-11-16).

"merci, c'est parfait !" after an app went to the app path again. The cases of
tests/fixtures/polite_closings.json run through the code rule that settles the
model router's decision, the lexical gates (router off, timed out or failed)
and the routing log line. The live part is tools/closing_route_eval.py.
"""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from packages.artifact_intent import app_intent
from packages.closing_intent import MAX_WORDS, polite_closing
from packages.file_intent import produces_file, requests_file_delivery
from packages.work_intent import continues_work, work_reason
from services.orchestrator import request_router
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.request_router import Decision, settle

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "polite_closings.json").read_text(
        encoding="utf-8"
    )
)["cases"]
CLOSINGS = [case["text"] for case in CASES if case["expect"] == "closing"]
REQUESTS = [case["text"] for case in CASES if case["expect"] == "request"]
CHANGES = [case["text"] for case in CASES if case.get("change")]
APP_LINK = "/api/atlas/artifacts/" + "d" * 32 + "/versions/1/open"
WITH_APP = (
    {"role": "user", "content": "fais-moi un petit jeu de memory"},
    {"role": "assistant", "content": f"Voici le jeu. [Ouvrir]({APP_LINK})"},
)


class FixtureTests(unittest.TestCase):
    def test_the_set_is_varied(self) -> None:
        self.assertGreaterEqual(len(CLOSINGS), 30)
        self.assertGreaterEqual(len(REQUESTS), 15)
        self.assertTrue(
            {"fr", "en", "de", "es", "it"} <= {case["lang"] for case in CASES}
        )


class DetectorTests(unittest.TestCase):
    def test_closings(self) -> None:
        for text in CLOSINGS:
            with self.subTest(text=text):
                self.assertTrue(polite_closing(text))

    def test_requests_are_not_closings(self) -> None:
        for text in REQUESTS:
            with self.subTest(text=text):
                self.assertFalse(polite_closing(text))

    def test_edges(self) -> None:
        for text in ("", "   ", "?", "...", "ok", "merci ?", "thanks?", "I think so"):
            with self.subTest(text=text):
                self.assertFalse(polite_closing(text))
        long = "merci " + "beaucoup " * MAX_WORDS
        self.assertFalse(polite_closing(long))


class SettleTests(unittest.TestCase):
    def test_a_closing_never_rebuilds_or_reruns(self) -> None:
        for kind in ("app", "drawing", "file", "run"):
            for text in CLOSINGS:
                with self.subTest(kind=kind, text=text):
                    self.assertEqual(
                        settle(text, Decision(kind=kind, follow_up=True)),
                        Decision(kind="answer"),
                    )

    def test_requests_keep_the_model_kind(self) -> None:
        for text in REQUESTS:
            routed = Decision(kind="app", follow_up=True)
            with self.subTest(text=text):
                self.assertEqual(settle(text, routed), routed)

    def test_machine_tasks_and_answers_are_left_alone(self) -> None:
        remote = Decision(kind="remote", follow_up=True)
        self.assertEqual(settle("merci, c'est parfait !", remote), remote)
        research = Decision(kind="research")
        self.assertEqual(settle("thanks", research), research)
        self.assertIsNone(settle("thanks", None))


class LexicalGateTests(unittest.TestCase):
    def test_closings_stay_in_the_chat(self) -> None:
        for text in CLOSINGS:
            with self.subTest(text=text):
                self.assertIsNone(app_intent(text, WITH_APP))
                self.assertFalse(produces_file(text))
                self.assertFalse(requests_file_delivery(text))
                self.assertIsNone(work_reason(text))
                self.assertFalse(continues_work(text))

    def test_changes_still_modify_the_app(self) -> None:
        for text in CHANGES:
            with self.subTest(text=text):
                self.assertEqual(app_intent(text, WITH_APP), "modify")


def _request(text: str) -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "messages": [*WITH_APP, {"role": "user", "content": text}],
            "ui_locale": "fr",
            "lang": "fr",
        }
    )


class RoutingLogTests(unittest.IsolatedAsyncioTestCase):
    async def _route(self, text: str, gateway: AsyncMock) -> tuple[object, str]:
        req = _request(text)
        with (
            patch.dict(os.environ, {"ATLAS_MODEL_ROUTER": "1"}),
            patch("services.orchestrator.request_router._gateway", gateway),
            self.assertLogs("services.orchestrator.request_router", "WARNING") as logs,
        ):
            routed = await request_router.route(req)
        self.assertEqual(len(logs.output), 1)
        self.assertTrue(logs.output[0].startswith("WARNING:"))
        # Never the user's text.
        self.assertNotIn("parfait", logs.output[0])
        self.assertNotIn("timer", logs.output[0])
        return routed, logs.output[0]

    async def test_a_closing_settled_by_code(self) -> None:
        gateway = AsyncMock(
            return_value={
                "text": '{"kind": "app", "follow_up": true}',
                "calls": [],
                "cost_eur": "0.0001",
            }
        )
        routed, line = await self._route("merci, c'est parfait !", gateway)
        self.assertEqual(routed, Decision(kind="answer"))
        for field in (
            "kind=answer",
            "source=settle",
            "rule=closing",
            "fallback=none",
            "privacy=standard",
        ):
            self.assertIn(field, line)

    async def test_a_change_keeps_the_model_decision(self) -> None:
        gateway = AsyncMock(
            return_value={
                "text": '{"kind": "app", "follow_up": true}',
                "calls": [],
                "cost_eur": "0.0001",
            }
        )
        routed, line = await self._route("thanks, now add a timer", gateway)
        self.assertEqual(routed, Decision(kind="app", follow_up=True))
        self.assertIn("source=model", line)
        self.assertIn("rule=none", line)

    async def test_no_decision_is_logged_as_lexical(self) -> None:
        gateway = AsyncMock(side_effect=TimeoutError)
        routed, line = await self._route("parfait, ajoute un timer", gateway)
        self.assertIsNone(routed)
        for field in ("kind=none", "source=lexical", "fallback=timeout"):
            self.assertIn(field, line)


if __name__ == "__main__":
    unittest.main()
