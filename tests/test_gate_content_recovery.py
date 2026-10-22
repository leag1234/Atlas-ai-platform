"""Task G: gate-4 public replies, independent of search provider or model compliance."""

from collections.abc import AsyncGenerator
from contextlib import nullcontext
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agent_provider import AgentProvider, AgentRequest
from quality import stream_quality
from serverless_support import environment
from test_quality_gateway import request, result
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call, Message, Reservation, Result, Turn
from services.orchestrator.model import Configuration, GatewayError, GatewayModel
from services.orchestrator.terminal_client import TerminalClient

CAPTURE = json.loads(Path("tests/fixtures/m26_gate4_replies.json").read_text())
CONTENT = """\
# Le syst\u00e8me mobile
- /e/OS repose sur Android et remplace les services Google pour limiter la collecte de données personnelles.
- La compatibilité des applications dépend de leurs besoins en services propriétaires.
# Les services associ\u00e9s
- Murena propose un ensemble de services pour synchroniser les contacts et les fichiers des utilisateurs.
- Il faut distinguer le système installé sur le téléphone des services accessibles avec un compte.
# Choisir son installation
- Vérifiez la compatibilité exacte du téléphone avant une installation et sauvegardez vos données personnelles.
- Comparez les besoins de vos applications avec les fonctions disponibles sur le système choisi.
"""


class GateContentTests(unittest.IsolatedAsyncioTestCase):
    async def recover(
        self,
        replies: list[str],
        *,
        forced_error: bool = False,
        through_search: bool = False,
        first_answer: str | None = None,
    ) -> tuple[
        Interaction,
        list[tuple[list[Message], float, list[Message], Decimal]],
        AsyncMock,
    ]:
        model = GatewayModel(
            "http://127.0.0.1",
            Configuration(
                input_eur_per_mtok=Decimal("1"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
                gateway_reserves_quality=True,
            ),
        )
        observed: list[tuple[list[Message], float, list[Message], Decimal]] = []

        initial = (
            [
                Turn(
                    "",
                    (
                        Call(
                            "search",
                            "web_search",
                            '{"query":"e/OS Murena Foundation history","lang":"en","n":5}',
                        ),
                    ),
                ),
                Turn(CAPTURE["J54"][0]),
                Turn(CAPTURE["J54"][0]),
            ]
            if through_search
            else []
        )

        async def complete(messages: list[Message], timeout: float) -> Turn:
            if initial:
                model.spent += Decimal("0.01")
                turn = initial.pop(0)
                return Turn(turn.text, turn.calls, Reservation(10, Decimal("0.01")))
            observed.append((messages, timeout, list(model.tools), model.allowance))
            model.spent += Decimal("0.01")
            return Turn(
                replies[len(observed) - 1], usage=Reservation(10, Decimal("0.01"))
            )

        async def publish(
            client: TerminalClient, path: str, strategy: str, timeout: float
        ) -> Message:
            client.files.append("/files/" + path)
            client.strategies.append(strategy)
            return {"download_url": client.files[-1]}

        first = Result(
            text=first_answer or CAPTURE["J54"][0],
            state="done",
            cost=Decimal("0.04"),
            tool_calls=3,
        )
        second = (
            GatewayError("provider_error", 502)
            if forced_error
            else Result(text=CAPTURE["J54"][0], state="done", cost=Decimal("0.02"))
        )
        runner = AsyncMock(side_effect=[first, second])
        execute = AsyncMock(return_value={"exit_code": 0})
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_TERMINAL_ENABLED": "1",
                    "ATLAS_SEARCH_PROVIDER": "serpapi",
                    "ATLAS_WEB_CACHE": root + "/cache",
                },
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(model, "complete", complete),
            patch.object(TerminalClient, "initialize", AsyncMock()),
            patch.object(TerminalClient, "execute", execute),
            patch.object(TerminalClient, "publish", publish),
            (
                nullcontext()
                if through_search
                else patch("services.orchestrator.chat_pipeline.run", runner)
            ),
            patch(
                "services.orchestrator.chat_pipeline.Runtime.execute",
                AsyncMock(return_value={"data": CAPTURE["search"][0]}),
            ),
        ):
            item = Interaction()
            await process(
                ChatRequest.model_validate(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": "Crée une présentation PPTX et PDF sur /e/OS et Murena.",
                            }
                        ]
                    }
                ),
                item,
            )
        return item, observed, execute

    async def test_legacy_plan_is_an_honest_presentation_failure(self) -> None:
        item, seen, execute = await self.recover([CONTENT])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][2], [])
        self.assertEqual(item.files, [])
        execute.assert_not_awaited()
        self.assertIn("Aucun fichier", item.reponse)
        self.assertNotIn("J'ai créé", item.reponse)
        self.assertAlmostEqual(item.cout_eur, 0.05)

    async def test_captured_irrelevant_search_through_real_loop(self) -> None:
        item, seen, execute = await self.recover([CONTENT], through_search=True)
        self.assertEqual(len(seen), 0)
        self.assertEqual(item.files, [])
        execute.assert_not_awaited()
        self.assertIn("https://www.eon.com/en.html", item.retrieved_urls)
        self.assertIn("Aucun fichier", item.reponse)

    async def test_failed_forced_turn_reserves_content_budget(self) -> None:
        item, seen, _ = await self.recover([CONTENT], forced_error=True)
        self.assertEqual(len(seen), 1)
        self.assertEqual(item.files, [])
        self.assertIn("Aucun fichier", item.reponse)
        self.assertLessEqual(item.cout_eur, 0.30)

    async def test_captured_substantive_answer_retains_content_budget_after_tool_error(
        self,
    ) -> None:
        item, seen, _ = await self.recover(
            [CONTENT], forced_error=True, first_answer=CAPTURE["J54_substantive"]
        )
        self.assertEqual(len(seen), 1)
        self.assertEqual(item.files, [])
        self.assertIn("Aucun fichier", item.reponse)
        self.assertLessEqual(item.cout_eur, 0.30)

    async def test_outline_repair_is_bounded_and_rejects_legacy_plan(self) -> None:
        item, seen, execute = await self.recover([CAPTURE["J54"][0], CONTENT])
        self.assertEqual(len(seen), 1)
        self.assertEqual(item.files, [])
        execute.assert_not_awaited()
        self.assertIn("Aucun fichier", item.reponse)

    async def test_two_invalid_content_turns_leave_honest_inline_answer(self) -> None:
        item, seen, execute = await self.recover([CAPTURE["J54"][0]] * 2)
        self.assertEqual(len(seen), 1)
        execute.assert_not_awaited()
        self.assertEqual(item.files, [])
        self.assertIn("Aucun fichier", item.reponse)


class GatePromiseTests(unittest.IsolatedAsyncioTestCase):
    async def test_substantive_captured_answer_retries_then_strips_future_action(
        self,
    ) -> None:
        seen = []

        async def transport(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            seen.append(req)
            text = CAPTURE["J57_answer"]
            yield {"delta": {"content": text}}
            yield result(text, 25)

        original = request(
            messages=[
                {
                    "role": "user",
                    "content": "Crée un DOCX contenant trois conseils pour organiser une réunion.",
                },
                {"role": "tool", "content": '{"error":"terminal_operation_failed"}'},
            ],
            produces_files=True,
        )
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
        ):
            events = [e async for e in stream_quality(AgentProvider(), original)]
        self.assertEqual(len(seen), 2)
        final = events[-1]["result"]
        assert isinstance(final, dict)
        answer = final["text"]
        self.assertIn("3.  **Désigner", answer)
        self.assertIn("a échoué", answer)
        self.assertNotIn("Je vais réessayer", answer)
        visible = "".join(
            str(delta.get("content", ""))
            for e in events
            if isinstance(delta := e.get("delta"), dict)
        )
        self.assertEqual(visible, answer)

    def test_future_file_actions_are_not_ordinary_future_explanations(self) -> None:
        from answer_content import strip_file_promises

        for promise in [
            "Je vais réessayer.",
            "Je vais créer le fichier.",
            "Nous allons publier le document.",
            "I will retry.",
            "I'll generate the PDF.",
            "I am going to save the file.",
            "Ich werde die Datei erstellen.",
            "Voy a crear el archivo.",
            "Procederò a creare il documento.",
        ]:
            with self.subTest(promise=promise):
                self.assertEqual(
                    strip_file_promises("The file operation failed. " + promise),
                    "The file operation failed.",
                )
        text = "This will reduce meeting time. I will explain the three options."
        self.assertEqual(strip_file_promises(text), text)


class GateFallbackTests(unittest.TestCase):
    def test_rejected_gate_plan_cannot_claim_a_created_file(self) -> None:
        from services.orchestrator.file_results import inline_fallback

        answer = inline_fallback(CAPTURE["J54"][0], "fr")
        self.assertNotIn("Je procède à la génération", answer)
        self.assertNotIn("J'ai créé", answer)
        self.assertNotIn("je vais intégrer", answer)

    def test_final_assembled_content_removes_future_retry_and_keeps_disclosure(
        self,
    ) -> None:
        from services.orchestrator.file_content import answer_content

        answer = answer_content(CAPTURE["J57_answer"])
        self.assertNotIn("Je vais réessayer", answer)
        self.assertIn("a échoué", answer)
        self.assertIn("3.  **Désigner", answer)
