"""Classifier table and routing decision for chat-to-Work escalation (step 1)."""

import os
import unittest
from unittest.mock import AsyncMock, patch

from packages.work_intent import needs_work
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_work import chat_work_enabled, should_escalate


def request(text: str, **kwargs: object) -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "messages": [{"role": "user", "content": text}],
            "ui_locale": "en",
            **kwargs,
        }
    )


class NeedsWorkTableTests(unittest.TestCase):
    TRUE_CASES = [
        # Running code or tests.
        "Écris un script Python qui calcule les 20 premiers nombres premiers et exécute-le.",
        "Écris un script python qui analyse ce CSV et lance les tests",
        "Run the unit tests and fix the failing ones",
        "Refactor this function and make sure pytest passes",
        "Installe les dépendances avec pip et exécute le programme",
        "Write a snake game in Python and run it",
        "Lance ce code et donne-moi la sortie",
        # A real file: the plain chat only returned the text (staging 2026-11-10).
        "Fais-moi un document Word qui résume en une page les avantages du télétravail.",
        "Rédige une lettre de résiliation d'un abonnement téléphonique au format Word, prête à imprimer.",
        "Écris-moi un fichier Excel avec un budget mensuel.",
        "Prépare une présentation PowerPoint de 5 diapositives sur le recyclage.",
        "Crée un fichier PDF d'une page avec la recette de la pâte à crêpes.",
        "Peux-tu me faire un tableau Excel de suivi des dépenses ?",
        "Create a Word document with a cover letter for an accountant position",
        "Give me this as a PDF",
        "Génère un budget.xlsx avec trois onglets",
        # A program on a runtime other than one web page.
        "Fais-moi un jeu de snake en python avec pygame",
        "Crée une API REST en Flask avec une base de données",
        "Crée un bot Discord en node.js",
        "Scrape ce site et exporte les résultats",
        "Build a CLI tool in Rust that renames files",
    ]
    FALSE_CASES = [
        "Explique-moi comment fonctionne une boucle for en python",
        "What is the difference between a list and a tuple?",
        "Traduis cette phrase en anglais",
        "Résume cet article",
        "Écris-moi un poème sur la mer",
        "Pourquoi le ciel est-il bleu ?",
        "Comment faire une pâte à pizza ?",
        "Crée une petite calculatrice en HTML single file",
        "Dessine un chat en SVG",
        "Fais une landing page en HTML/CSS",
        "Write a short story about a robot",
        "Compare Python and JavaScript",
        "Quelle est la capitale de la France ?",
        "Rédige un email professionnel de relance",
        # Small interactive apps stay in the app panel.
        "Crée-moi une boîte à rythme : kick, snare, hi-hat et clap, une grille de 16 pas.",
        "Fais-moi un jeu de snake jouable au clavier.",
        # A code answer without running it stays in the chat.
        "Écris une fonction Python qui vérifie si une chaîne est un palindrome.",
        # Ordinary sentences with words such as test, code, programme, application.
        "Quel programme ce soir à la télé ?",
        "Analyse ce texte et donne-moi les idées principales",
        "Donne-moi un outil pour apprendre l'anglais",
        "J'ai un fichier Excel qui plante, que faire ?",
        "Comment créer un fichier Excel ?",
        "Comment exécuter un script python ?",
        "How do I run unit tests in Python?",
        "Résume ce document Word",
        "Quel est le meilleur test de grossesse ?",
        "Le code postal de Lyon",
        "Lance une recherche sur les trains pour Paris",
        "Je cherche une application pour méditer",
        "Quelle est la différence entre Word et PDF ?",
        "Teste mes connaissances en histoire",
        "Mon serveur est tombé, pourquoi ?",
    ]

    def test_true_cases(self) -> None:
        for text in self.TRUE_CASES:
            with self.subTest(text=text):
                self.assertTrue(needs_work(text), text)

    def test_false_cases(self) -> None:
        for text in self.FALSE_CASES:
            with self.subTest(text=text):
                self.assertFalse(needs_work(text), text)

    def test_follow_up_requests(self) -> None:
        from packages.work_intent import continues_work

        for text in (
            "Ajoute un test pour les listes vides",
            "Mets le titre en bleu",
            "Relance les tests",
        ):
            with self.subTest(text=text):
                self.assertTrue(continues_work(text), text)
        for text in ("Merci beaucoup", "Explique-moi ce code", "Pourquoi ça plante ?"):
            with self.subTest(text=text):
                self.assertFalse(continues_work(text), text)

    def test_empty_and_attachment_only(self) -> None:
        self.assertFalse(needs_work(""))
        self.assertFalse(needs_work("   "))
        self.assertFalse(needs_work("[Attachment: data.csv]", []))


class RoutingTests(unittest.TestCase):
    def test_flag_default_off(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATLAS_CHAT_WORK", None)
            self.assertFalse(chat_work_enabled())

    def test_flag_on(self) -> None:
        with patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}):
            self.assertTrue(chat_work_enabled())

    def test_should_escalate_requires_flag_and_gate(self) -> None:
        with patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}):
            with patch(
                "services.orchestrator.chat_work.work_gate_open", return_value=False
            ):
                self.assertFalse(
                    should_escalate(request("Write a snake game in Python"))
                )
            with patch(
                "services.orchestrator.chat_work.work_gate_open", return_value=True
            ):
                self.assertTrue(
                    should_escalate(request("Write a snake game in Python"))
                )

    def test_should_escalate_skips_project_and_attachments(self) -> None:
        with patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}):
            with patch(
                "services.orchestrator.chat_work.work_gate_open", return_value=True
            ):
                self.assertFalse(
                    should_escalate(
                        request(
                            "Write a snake game in Python",
                            project_id="123e4567-e89b-12d3-a456-426614174000",
                            conversation_id="123e4567-e89b-12d3-a456-426614174001",
                        )
                    )
                )
                self.assertFalse(
                    should_escalate(
                        request(
                            "Analyse ce document",
                            documents=[
                                {
                                    "filename": "a.pdf",
                                    "file_data": "data:application/pdf;base64,AAAA",
                                }
                            ],
                        )
                    )
                )
                self.assertFalse(should_escalate(request("What is a tuple?")))


class EscalateBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_escalate_creates_scratch_and_calls_process_work(self) -> None:
        from services.orchestrator.chat_work import escalate_to_work
        from services.orchestrator.interactions import Interaction

        req = request("Write a snake game in Python and run it")
        req.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174010"
        req.__dict__["artifact_owner"] = "user@example.com"
        item = Interaction()
        calls: list[dict[str, object]] = []
        bound: list[dict[str, object]] = []

        async def fake_process_work(r, i, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            i.reponse, i.state = "done answer", "done"

        async def fake_bind(*args, **kwargs):  # type: ignore[no-untyped-def]
            bound.append(dict(kwargs))

        with (
            patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
            patch("services.orchestrator.chat_work.work_gate_open", return_value=True),
            patch(
                "services.orchestrator.chat_work._identity",
                new=AsyncMock(
                    return_value={
                        "memberships": [
                            {"org_id": "123e4567-e89b-12d3-a456-426614174000"}
                        ]
                    }
                ),
            ),
            patch(
                "services.orchestrator.chat_work._find_scratch_project",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.orchestrator.chat_work._ensure_scratch_project",
                new=AsyncMock(return_value="123e4567-e89b-12d3-a456-426614174002"),
            ),
            patch(
                "services.orchestrator.chat_work._find_binding",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.orchestrator.chat_work._create_conversation",
                new=AsyncMock(return_value=("123e4567-e89b-12d3-a456-426614174003", 0)),
            ),
            patch(
                "services.orchestrator.chat_work._ensure_work_session",
                new=AsyncMock(return_value="123e4567-e89b-12d3-a456-426614174004"),
            ),
            patch("services.orchestrator.chat_work._bind_conversation", new=fake_bind),
            patch(
                "services.orchestrator.code_mode.process_work", new=fake_process_work
            ),
        ):
            handled = await escalate_to_work(req, item)
        self.assertTrue(handled)
        self.assertEqual(item.state, "done")
        self.assertEqual(item.task_type, "work")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["session_id"], "123e4567-e89b-12d3-a456-426614174004")
        self.assertEqual(len(bound), 1)

    async def test_escalate_reuses_bound_session_on_next_turn(self) -> None:
        from services.orchestrator.chat_work import escalate_to_work
        from services.orchestrator.interactions import Interaction

        req = request("Add a score counter to the snake game and run the tests")
        req.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174010"
        req.__dict__["artifact_owner"] = "user@example.com"
        item = Interaction()
        calls: list[dict[str, object]] = []

        async def fake_process_work(r, i, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            i.reponse, i.state = "done answer", "done"

        with (
            patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
            patch("services.orchestrator.chat_work.work_gate_open", return_value=True),
            patch(
                "services.orchestrator.chat_work._identity",
                new=AsyncMock(
                    return_value={
                        "memberships": [
                            {"org_id": "123e4567-e89b-12d3-a456-426614174000"}
                        ]
                    }
                ),
            ),
            patch(
                "services.orchestrator.chat_work._ensure_scratch_project",
                new=AsyncMock(return_value="123e4567-e89b-12d3-a456-426614174002"),
            ),
            patch(
                "services.orchestrator.chat_work._find_binding",
                new=AsyncMock(
                    return_value={
                        "conversation_id": "123e4567-e89b-12d3-a456-426614174003",
                        "work_session_id": "123e4567-e89b-12d3-a456-426614174004",
                    }
                ),
            ),
            patch(
                "services.orchestrator.chat_work._create_conversation",
                new=AsyncMock(side_effect=AssertionError("must not be called")),
            ) as ensure_conversation,
            patch(
                "services.orchestrator.chat_work._ensure_work_session",
                new=AsyncMock(side_effect=AssertionError("must not be called")),
            ) as ensure_session,
            patch(
                "services.orchestrator.chat_work._bind_conversation", new=AsyncMock()
            ) as bind,
            patch(
                "services.orchestrator.code_mode.process_work", new=fake_process_work
            ),
        ):
            handled = await escalate_to_work(req, item)
        self.assertTrue(handled)
        ensure_conversation.assert_not_awaited()
        ensure_session.assert_not_awaited()
        bind.assert_not_awaited()
        self.assertEqual(calls[0]["session_id"], "123e4567-e89b-12d3-a456-426614174004")
        self.assertEqual(calls[0]["project_id"], "123e4567-e89b-12d3-a456-426614174002")

    async def test_escalate_returns_false_without_subject(self) -> None:
        from services.orchestrator.chat_work import escalate_to_work
        from services.orchestrator.interactions import Interaction

        req = request("Write a snake game in Python")
        with (
            patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
            patch("services.orchestrator.chat_work.work_gate_open", return_value=True),
        ):
            self.assertFalse(await escalate_to_work(req, Interaction()))


if __name__ == "__main__":
    unittest.main()


class FollowUpRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_change_request_without_a_bound_session_stays_in_chat(self) -> None:
        from services.orchestrator.chat_work import escalate_to_work
        from services.orchestrator.interactions import Interaction

        req = request("Ajoute un test pour les listes vides")
        req.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174010"
        req.__dict__["artifact_owner"] = "user@example.com"
        with (
            patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
            patch("services.orchestrator.chat_work.work_gate_open", return_value=True),
            patch(
                "services.orchestrator.chat_work._identity",
                new=AsyncMock(
                    return_value={
                        "memberships": [
                            {"org_id": "123e4567-e89b-12d3-a456-426614174000"}
                        ]
                    }
                ),
            ),
            patch(
                "services.orchestrator.chat_work._find_scratch_project",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.orchestrator.chat_work._ensure_scratch_project",
                new=AsyncMock(side_effect=AssertionError("must not be created")),
            ),
        ):
            self.assertFalse(await escalate_to_work(req, Interaction()))

    async def test_ordinary_question_never_looks_anything_up(self) -> None:
        from services.orchestrator.chat_work import escalate_to_work
        from services.orchestrator.interactions import Interaction

        req = request("Quelle est la capitale de la France ?")
        req.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174010"
        with (
            patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
            patch("services.orchestrator.chat_work.work_gate_open", return_value=True),
            patch(
                "services.orchestrator.chat_work._identity",
                new=AsyncMock(side_effect=AssertionError("no lookup")),
            ),
        ):
            self.assertFalse(await escalate_to_work(req, Interaction()))


class InterfaceTaskTests(unittest.TestCase):
    def test_interface_tasks_never_reach_work(self) -> None:
        # Staging 2026-11-10: the title task quotes the user's request; it was
        # escalated too and collided with the real turn on the same session.
        from services.orchestrator.chat_work import eligible

        task = request(
            "### Task:\nGenerate a concise, 3-5 word title.\n<chat_history>\n"
            "USER: Fais-moi un document Word sur le télétravail\n</chat_history>"
        )
        with (
            patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
            patch("services.orchestrator.chat_work.work_gate_open", return_value=True),
        ):
            self.assertFalse(eligible(task))
            self.assertTrue(eligible(request("Fais-moi un document Word")))


class WorkFailureFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_work_turn_falls_back_to_the_chat(self) -> None:
        from services.orchestrator.chat_work import escalate_to_work
        from services.orchestrator.interactions import Interaction

        req = request("Fais-moi un document Word sur le télétravail")
        req.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174010"
        req.__dict__["artifact_owner"] = "user@example.com"
        item = Interaction()
        state = item.state

        async def failing_work(r, i, **kwargs):  # type: ignore[no-untyped-def]
            i.state, i.reponse = "failed", "partial"
            i.erreurs.append("deliverable_group_required")

        with (
            patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
            patch("services.orchestrator.chat_work.work_gate_open", return_value=True),
            patch(
                "services.orchestrator.chat_work._identity",
                new=AsyncMock(
                    return_value={
                        "memberships": [
                            {"org_id": "123e4567-e89b-12d3-a456-426614174000"}
                        ]
                    }
                ),
            ),
            patch(
                "services.orchestrator.chat_work._ensure_scratch_project",
                new=AsyncMock(return_value="123e4567-e89b-12d3-a456-426614174002"),
            ),
            patch(
                "services.orchestrator.chat_work._find_binding",
                new=AsyncMock(
                    return_value={
                        "conversation_id": "123e4567-e89b-12d3-a456-426614174003",
                        "work_session_id": "123e4567-e89b-12d3-a456-426614174004",
                    }
                ),
            ),
            patch("services.orchestrator.code_mode.process_work", new=failing_work),
        ):
            self.assertFalse(await escalate_to_work(req, item))
        self.assertEqual((item.state, item.erreurs, item.reponse), (state, [], ""))
