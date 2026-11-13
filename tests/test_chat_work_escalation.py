"""Classifier table and routing decision for chat-to-Work escalation (step 1)."""

import os

import aiohttp
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

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
        # The bare kind right after the verb (staging 2026-11-10: these stayed in the chat).
        "Prépare un PDF de deux pages sur le marché des véhicules électriques en Europe.",
        "Rédige une note Word comparant trois offres de fibre.",
        "Fais-moi un Excel de suivi de dépenses",
        "Peux-tu me faire un PDF de ce texte ?",
        "Make me a PowerPoint about our roadmap",
        "Create an Excel with monthly sales",
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
        "Fais un résumé du PDF que je t'ai envoyé hier",
        "Écris une fonction qui lit un PDF page par page",
        "Rédige un mail qui explique la différence entre Word et Pages",
        "Write a summary of the Excel shortcuts I should know",
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


class AttachedDocumentTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_question_about_an_attached_document_never_reaches_work(
        self,
    ) -> None:
        # Work does not receive the attachment: even a decision that names a
        # run or a file (kept from an earlier gate, or the lexical test) leaves
        # the turn to the chat, without any call to the project service.
        from services.orchestrator.chat_work import eligible, escalate_to_work
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.request_router import DECISION_KEY, Decision

        document = {
            "filename": "note.pdf",
            "file_data": "data:application/pdf;base64,AAAA",
        }
        tag = (
            '<attached_files>\n<file type="filesystem" url="f1" '
            'content_type="application/pdf" name="note.pdf"/>\n</attached_files>\n'
        )
        questions = [
            "Résume ce document en 5 points et donne son numéro de référence officiel.",
            "resume moi ca stp",
            "Summarise this file and list the key dates",
            "what does it say about the budget?",
            "Avec le terminal, compte le nombre de pages du PDF joint",
            "Run a Python script that counts the words of this file",
            "Fasse dieses Dokument zusammen",
            "Resume este archivo en cinco puntos",
            "Riassumi il file allegato",
        ]
        identity = AsyncMock()
        work = AsyncMock()
        with (
            patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1", "ATLAS_MODEL_ROUTER": "1"}),
            patch("services.orchestrator.chat_work.work_gate_open", return_value=True),
            patch("services.orchestrator.chat_work._identity", new=identity),
            patch("services.orchestrator.code_mode.process_work", new=work),
        ):
            for question in questions:
                for req in (
                    request(question, documents=[document]),
                    request(tag + question),
                ):
                    for kind in ("run", "file", "remote"):
                        with self.subTest(text=req.user_text, kind=kind):
                            req.__dict__[DECISION_KEY] = Decision(kind=kind)
                            req.__dict__["native_chat_id"] = (
                                "123e4567-e89b-12d3-a456-426614174010"
                            )
                            req.__dict__["artifact_owner"] = "user@example.com"
                            item = Interaction()
                            self.assertFalse(eligible(req))
                            self.assertFalse(should_escalate(req))
                            self.assertFalse(await escalate_to_work(req, item))
                            self.assertEqual(item.erreurs, [])
        identity.assert_not_awaited()
        work.assert_not_awaited()


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
        scoped: list[ChatRequest] = []

        async def fake_process_work(r, i, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs)
            scoped.append(r)
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
        # The hidden chat project numbers a taken name (chat_file_names.py).
        self.assertTrue(scoped[0].__dict__["numbered_copies"])
        self.assertEqual(scoped[0].__dict__["earlier_answers"], [])

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

    async def test_the_hand_over_is_announced_before_the_setup(self) -> None:
        from services.orchestrator.chat_work import escalate_to_work
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.stream_client import sink_context

        req = request("Write a snake game in Python and run it")
        req.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174010"
        req.__dict__["artifact_owner"] = "user@example.com"
        seen: list[object] = []

        async def sink(event: dict[str, object]) -> None:
            seen.append(event)

        async def identity(*args: object) -> dict[str, object]:
            seen.append("identity")
            return {"memberships": []}

        token = sink_context.set(sink)
        try:
            with (
                patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
                patch(
                    "services.orchestrator.chat_work.work_gate_open", return_value=True
                ),
                patch("services.orchestrator.chat_work._identity", new=identity),
            ):
                self.assertFalse(await escalate_to_work(req, Interaction()))
        finally:
            sink_context.reset(token)
        # No organization: the chat answers, and the status says so.
        self.assertEqual(
            seen,
            [
                {"work_step": {"phase": "preparing"}},
                "identity",
                {"work_step": {"phase": "declined"}},
            ],
        )

    async def test_a_turn_that_stays_in_the_chat_announces_nothing(self) -> None:
        from services.orchestrator.chat_work import escalate_to_work
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.stream_client import sink_context

        seen: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            seen.append(event)

        token = sink_context.set(sink)
        try:
            with (
                patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
                patch(
                    "services.orchestrator.chat_work.work_gate_open", return_value=True
                ),
            ):
                req = request("Write a snake game in Python")
                self.assertFalse(await escalate_to_work(req, Interaction()))
                req = request("What is the capital of Portugal?")
                req.__dict__["artifact_owner"] = "user@example.com"
                self.assertFalse(await escalate_to_work(req, Interaction()))
        finally:
            sink_context.reset(token)
        self.assertEqual(seen, [])

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
            self.assertLogs("services.orchestrator.chat_work", "WARNING") as logs,
        ):
            self.assertFalse(await escalate_to_work(req, item))
        self.assertEqual((item.state, item.erreurs, item.reponse), (state, [], ""))
        # The line says what the person got: here the chat answers by itself.
        self.assertIn("errors=deliverable_group_required", logs.output[-1])
        self.assertIn("outcome=chat_answers", logs.output[-1])

    async def test_a_delivered_file_survives_a_leftover_tool_error(self) -> None:
        from services.orchestrator.chat_work import escalate_to_work
        from services.orchestrator.interactions import Interaction

        req = request("Fais-moi un document Word sur le télétravail")
        req.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174010"
        req.__dict__["artifact_owner"] = "user@example.com"
        item = Interaction()

        async def failing_work(r, i, **kwargs):  # type: ignore[no-untyped-def]
            i.state, i.reponse = "failed", "Done.\n\nWork failed: read refused"
            i.erreurs.append("read refused")
            i.work_failure = {
                "category": "tool_error",
                "answer": "Done.\n\n[Download](/api/atlas/orgs/o/projects/p/deliverables/d/download)",
            }

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
            self.assertLogs("services.orchestrator.chat_work", "WARNING") as logs,
        ):
            self.assertTrue(await escalate_to_work(req, item))
        self.assertEqual(
            (item.state, item.erreurs, item.task_type), ("done", [], "work")
        )
        self.assertIn("/deliverables/d/download", item.reponse)
        self.assertNotIn("Work failed", item.reponse)
        self.assertIn("errors=read refused", logs.output[-1])
        self.assertIn("outcome=file_delivered", logs.output[-1])

    async def test_a_failed_machine_task_is_explained_never_answered_by_the_chat_alone(
        self,
    ) -> None:
        # The chat has no machine tools: falling back to it produced "I cannot
        # access your server" after a runner limit (staging 2026-11-11).
        from services.orchestrator.chat_work import escalate_to_work
        from services.orchestrator.code_mode import work_failure_category
        from services.orchestrator.interactions import Interaction

        req = request(
            "Connecte-toi en SSH à ma machine Test box et dis-moi l'espace disque restant"
        )
        req.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174011"
        req.__dict__["artifact_owner"] = "user@example.com"
        item = Interaction()
        category = work_failure_category("runner_concurrency_limit", tool_failure=True)
        self.assertEqual(category, "machine_busy")

        async def failing_work(r, i, **kwargs):  # type: ignore[no-untyped-def]
            i.state, i.reponse = "failed", "Work failed: runner_concurrency_limit"
            i.erreurs.append("runner_concurrency_limit")
            i.work_failure = {"category": category, "answer": ""}

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
            self.assertTrue(await escalate_to_work(req, item))
        self.assertEqual((item.state, item.task_type), ("done", "work"))
        self.assertIn("votre machine est déjà utilisée", item.reponse)
        self.assertNotIn("Work failed", item.reponse)

    async def test_a_stopped_run_is_explained_never_answered_by_the_chat_alone(
        self,
    ) -> None:
        # Without a known category (time limit on the engine path) the tool-less chat
        # used to answer, and invented test output (staging 2026-11-12).
        from services.orchestrator.chat_work import escalate_to_work
        from services.orchestrator.interactions import Interaction

        req = request(
            "Write a function in Go that reverses a string, with unit tests, and run the tests to show they pass"
        )
        req.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174012"
        req.__dict__["artifact_owner"] = "user@example.com"
        item = Interaction()

        async def stopped_work(r, i, **kwargs):  # type: ignore[no-untyped-def]
            i.state, i.reponse = "failed", "Work failed: time_limit"
            i.erreurs.append("time_limit")
            i.work_failure = {}

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
            patch("services.orchestrator.code_mode.process_work", new=stopped_work),
        ):
            self.assertTrue(await escalate_to_work(req, item))
        self.assertEqual((item.state, item.task_type), ("done", "work"))
        self.assertIn("la limite de temps a été atteinte", item.reponse)
        self.assertNotIn("PASS", item.reponse)

    def test_machine_limits_have_their_own_category_in_every_language(self) -> None:
        from services.orchestrator.code_mode import WORK_FAILURE, work_failure_category

        self.assertEqual(
            work_failure_category("runner_minutes_limit", tool_failure=True),
            "machine_minutes",
        )
        self.assertEqual(
            work_failure_category("read refused", tool_failure=True), "tool_error"
        )
        for words in WORK_FAILURE.values():
            self.assertIn("machine_busy", words)
            self.assertIn("machine_minutes", words)


class DeliveredFileHintTests(unittest.TestCase):
    def test_a_change_request_names_the_kind_delivered_last(self) -> None:
        from services.orchestrator.chat_schema import ChatMessage
        from services.orchestrator.chat_work import delivered_file_hint

        req = request("Ajoute une phrase et mets à jour le document")
        req = req.model_copy(
            update={
                "messages": [
                    ChatMessage(role="user", content="Fais-moi une lettre Word"),
                    ChatMessage(role="assistant", content="Le document a été créé."),
                    *req.messages,
                ]
            }
        )
        self.assertIn(".docx file", delivered_file_hint(req))
        self.assertEqual(delivered_file_hint(request("Relance le script")), "")


class ChatDeadlineTests(unittest.TestCase):
    def test_a_turn_the_chat_answers_keeps_the_chat_deadline(self) -> None:
        with patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}):
            req = request("Génère un PowerPoint de 5 diapositives")
            self.assertEqual(req.timeout_seconds, 300.0)
            req.__dict__["work_declined"] = True
            self.assertEqual(req.model_copy().timeout_seconds, 120.0)


class DroppedConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_dropped_connection_before_the_turn_is_retried_once(self) -> None:
        from services.orchestrator.chat_work import escalate_to_work
        from services.orchestrator.interactions import Interaction

        req = request("Fais-moi un document Word sur le télétravail")
        req.__dict__["native_chat_id"] = "123e4567-e89b-12d3-a456-426614174010"
        req.__dict__["artifact_owner"] = "user@example.com"
        item = Interaction()

        calls: list[object] = []

        async def failing_work(r, i, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(kwargs.get("turn_request_id"))
            if len(calls) == 1:
                raise aiohttp.ClientOSError("connection reset")
            i.state, i.reponse, i.task_type = "done", "Script ran.", "work"

        with (
            patch.dict(os.environ, {"ATLAS_CHAT_WORK": "1"}),
            patch("services.orchestrator.chat_work.asyncio.sleep", new=AsyncMock()),
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
            self.assertTrue(await escalate_to_work(req, item))
        self.assertEqual(len(calls), 2)
        self.assertEqual((item.state, item.reponse), ("done", "Script ran."))
        # The first run is the chat request; the second is a new run with its
        # own id (lease and reservation), never the failed run's id again.
        self.assertIsNone(calls[0])
        self.assertEqual(str(UUID(str(calls[1]))), calls[1])

    async def test_a_new_run_acquires_under_its_own_id(self) -> None:
        from services.orchestrator import code_mode
        from services.orchestrator.interactions import Interaction

        acquired: list[str] = []

        class Broker:
            def __init__(self, *args: object) -> None:
                pass

            async def public(self, method: str, path: str) -> tuple[int, dict]:
                return 200, {
                    "conversation_id": "123e4567-e89b-12d3-a456-426614174003",
                    "revision": 1,
                    "effective_limits": {"expanded_bytes": 1},
                }

            async def call(self, operation: str, data: dict) -> dict:
                acquired.append(data["request_id"])
                raise RuntimeError("stop after acquire")

        req = request("Fais-moi un script")
        req = req.model_copy(
            update={"conversation_id": "123e4567-e89b-12d3-a456-426614174003"}
        )
        item = Interaction()
        fresh = "123e4567-e89b-12d3-a456-426614174099"
        with (
            patch("services.orchestrator.code_api.ExecutionBroker", Broker),
            patch("services.orchestrator.code_mode.settings", return_value=None),
        ):
            for run_id in (None, fresh):
                with self.assertRaises(RuntimeError):
                    await code_mode.process_work(
                        req,
                        item,
                        subject="user@example.com",
                        org_id="123e4567-e89b-12d3-a456-426614174000",
                        project_id="123e4567-e89b-12d3-a456-426614174002",
                        session_id="123e4567-e89b-12d3-a456-426614174004",
                        turn_request_id=run_id,
                    )
        self.assertEqual(
            acquired, [str(UUID(item.request_id.removeprefix("chatcmpl-"))), fresh]
        )


class NamedOfficeFormatTests(unittest.TestCase):
    def test_one_named_format_constrains_the_file(self) -> None:
        from packages.work_intent import named_office_format

        self.assertEqual(
            named_office_format("Rédige une note Word sur les aides"), "docx"
        )
        self.assertEqual(named_office_format("Fais-moi un Excel de budget"), "xlsx")
        self.assertEqual(
            named_office_format("Génère un PowerPoint de 5 diapositives"), "pptx"
        )
        self.assertEqual(named_office_format("Prépare un PDF de deux pages"), "pdf")
        self.assertIsNone(named_office_format("Fais-moi un Word et un PDF du rapport"))
        self.assertIsNone(named_office_format("Écris un script et exécute-le"))
        self.assertIsNone(named_office_format("In a word, what is a PDF?"))


class RemoteIntentTests(unittest.TestCase):
    def test_requests_on_a_personal_machine_reach_work(self) -> None:
        from packages.work_intent import work_reason

        for text in (
            "Connecte-toi à mon serveur et regarde l'espace disque",
            "Regarde les logs nginx sur mon serveur",
            "Lance les tests sur ma machine de build",
            "Check the disk usage on my server",
            "Redémarre le service en ssh",
            "ssh into the build box and run make",
        ):
            with self.subTest(text=text):
                self.assertEqual(work_reason(text), "remote")

    def test_questions_about_ssh_stay_in_the_chat(self) -> None:
        from packages.work_intent import work_reason

        for text in (
            "Comment se connecter en ssh à un serveur ?",
            "Explique-moi ce qu'est un serveur web",
            "What is the difference between a VPS and a dedicated server?",
            "Écris un mail pour demander un nouveau serveur",
        ):
            with self.subTest(text=text):
                self.assertIsNone(work_reason(text))
