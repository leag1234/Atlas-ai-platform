"""Plain writing requests stay a text answer in the chat (staging 2026-11-16).

The deterministic part of tools/writing_route_eval.py: the same cases
(tests/fixtures/plain_writing_routes.json) through the lexical gates, which
decide when the model router is off, times out or fails, and through the code
rule that settles the router's decision.
"""

import json
import unittest
from pathlib import Path

from packages.artifact_intent import app_intent
from packages.file_intent import requests_file_delivery
from packages.work_intent import work_reason
from packages.writing_intent import plain_writing
from services.orchestrator.request_router import Decision, settle

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "plain_writing_routes.json").read_text(
        encoding="utf-8"
    )
)["cases"]
WRITING = [case["text"] for case in CASES if case["expect"] == "answer"]
APPS = [case["text"] for case in CASES if case["expect"] == "app"]
FILES = [case for case in CASES if case["expect"] == "file"]
APP_LINK = "/api/atlas/artifacts/" + "c" * 32 + "/versions/1/open"
WITH_APP = ({"role": "assistant", "content": f"[Open]({APP_LINK})"},)


class FixtureTests(unittest.TestCase):
    def test_the_set_is_varied(self) -> None:
        self.assertGreaterEqual(len(WRITING), 30)
        self.assertGreaterEqual(len(APPS) + len(FILES), 15)
        self.assertTrue({"fr", "en", "de", "es"} <= {case["lang"] for case in CASES})


class LexicalGateTests(unittest.TestCase):
    def test_writing_requests_never_open_the_app_panel(self) -> None:
        for text in WRITING:
            with self.subTest(text=text):
                self.assertTrue(plain_writing(text))
                self.assertIsNone(app_intent(text, ()))
                # With an app in the conversation, a new text is not that app.
                self.assertIsNone(app_intent(text, WITH_APP))

    def test_writing_requests_are_not_files_or_work(self) -> None:
        for text in WRITING:
            with self.subTest(text=text):
                self.assertFalse(requests_file_delivery(text))
                self.assertIsNone(work_reason(text))

    def test_counter_examples_keep_their_path(self) -> None:
        for text in APPS:
            with self.subTest(text=text):
                self.assertFalse(plain_writing(text))
                self.assertEqual(app_intent(text, ()), "create")
        for case in FILES:
            with self.subTest(text=case["text"]):
                self.assertFalse(plain_writing(case["text"]))
                self.assertIsNone(app_intent(case["text"], ()))

    def test_a_change_to_the_current_app_stays_a_change(self) -> None:
        # "Add a victory message" changes the game delivered earlier.
        self.assertEqual(
            app_intent("ajoute un message de victoire", WITH_APP), "modify"
        )
        self.assertEqual(app_intent("add a welcome text", WITH_APP), "modify")

    def test_pasted_material_does_not_decide(self) -> None:
        # Words of the quoted or pasted text are not the request.
        self.assertTrue(
            plain_writing("reply to this review: 'Your app crashes on every page.'")
        )
        self.assertTrue(
            plain_writing(
                "réponds à cet avis : « le jeu est trop dur, l'appli plante »"
            )
        )
        self.assertTrue(
            plain_writing(
                "résume ce texte :\nLe site du club propose un quiz en ligne."
            )
        )
        # Before the colon, the request itself names a file.
        self.assertFalse(plain_writing("résume ce texte en PDF : Le club a gagné."))

    def test_other_deliverables_are_left_to_the_other_gates(self) -> None:
        for text in (
            "écris un message d'anniversaire animé avec des confettis",
            "make a birthday message generator",
            "écris une lettre et mets-la dans un document Word",
            "write a poem and draw a picture of it",
            "écris un script python qui envoie un message",
            "fais un tableau de suivi de mes idées de cadeaux",
            "écris mon CV",
            "write a letter I can print",
            "explique-moi comment écrire une lettre",
        ):
            with self.subTest(text=text):
                if "explique" in text:
                    self.assertIsNone(app_intent(text, ()))
                else:
                    self.assertFalse(plain_writing(text))


class SettleTests(unittest.TestCase):
    def test_a_routed_app_or_file_for_a_text_becomes_an_answer(self) -> None:
        for text in WRITING:
            for routed in (
                Decision(kind="app"),
                Decision(kind="file", file_format="docx"),
                Decision(kind="drawing"),
            ):
                with self.subTest(text=text, kind=routed.kind):
                    settled = settle(text, routed)
                    assert settled is not None
                    self.assertEqual(settled.kind, "answer")
                    self.assertIsNone(settled.file_format)

    def test_the_router_keeps_real_apps_and_files(self) -> None:
        for text in APPS:
            with self.subTest(text=text):
                self.assertEqual(
                    settle(text, Decision(kind="app")), Decision(kind="app")
                )
        for case in FILES:
            routed = Decision(kind="file", file_format=case["format"])
            with self.subTest(text=case["text"]):
                self.assertEqual(settle(case["text"], routed), routed)

    def test_follow_ups_answers_and_failures_are_unchanged(self) -> None:
        change = Decision(kind="app", follow_up=True)
        self.assertEqual(settle("ajoute un message de victoire", change), change)
        research = Decision(kind="research")
        self.assertEqual(settle(WRITING[0], research), research)
        self.assertIsNone(settle(WRITING[0], None))

    def test_machine_management_still_goes_to_the_machine_tools(self) -> None:
        self.assertEqual(
            settle("oublie mon ordi portable", Decision(kind="answer")),
            Decision(kind="remote"),
        )


if __name__ == "__main__":
    unittest.main()
