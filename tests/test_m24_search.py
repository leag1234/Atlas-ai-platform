"""Device timing research and quota disclosure through the public HTTP boundary."""

from decimal import Decimal
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from services.orchestrator.chat_api import app
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.tools import Runtime
from services.orchestrator.search_policy import required_research, corpus_allowed


class SearchJourneyTests(unittest.TestCase):
    def test_research_does_not_replace_user_question_with_a_case_template(self) -> None:
        questions = (
            "What is the IPO price of this company?",
            "Sur un Amstrad CPC, quels sont les timings de OUTI et OTIR ?",
            "Quel est le prix de ce traitement ?",
            "Compare the current sensor specifications.",
        )
        for question in questions:
            with self.subTest(question=question):
                calls = required_research(question, "fr", device_timings=True)
                self.assertEqual(len(calls), 1)
                self.assertEqual(json.loads(calls[0].arguments)["query"], question)

    def test_explicit_corpus_exclusions_are_enforced_across_phrasings(self) -> None:
        for question in (
            "Tu ne fais pas appel à la mémoire présente dans notre environnement.",
            "Sans mémoire interne.",
            "N'UTILISE AUCUN CORPUS",
            "Do not use internal documents.",
            "Create this without querying internal memory.",
            "Erstelle die Folien ohne interne Quellen.",
        ):
            with self.subTest(question=question):
                self.assertFalse(corpus_allowed(question))
        self.assertTrue(corpus_allowed("Retrouve les documents dans notre mémoire."))

    def test_device_timings_require_sources_across_natural_phrasings(self) -> None:
        for question in (
            "Sur un Amstrad CPC, quels sont les timings de OUTI et OTIR en microsecondes ?",
            "OUTI sur CPC ?",
            "QUELLE DUREE POUR CES INSTRUCTIONS SUR CPC ?",
            "Combien de cycles consomment ces instructions du Z80 ?",
            "How long does OUTI take on this machine?",
            "Wie viele Mikrosekunden braucht OTIR auf dem CPC?",
        ):
            with self.subTest(question=question):
                calls = required_research(question, "fr", device_timings=True)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].name, "web_search")
        self.assertEqual(
            required_research(
                "Explique comment une recherche fonctionne.", "fr", device_timings=True
            ),
            (),
        )

    def test_quota_failure_is_visible_without_a_memory_answer(self) -> None:
        model = GatewayModel(
            "http://127.0.0.1",
            Configuration(
                input_eur_per_mtok=Decimal("1"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_SEARCH_PROVIDER": "tavily",
                    "ATLAS_TERMINAL_ENABLED": "0",
                    "ATLAS_WEB_CACHE": root + "/cache.sqlite",
                    "ATLAS_INTERACTION_DIR": root + "/logs",
                },
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(
                model,
                "complete",
                AsyncMock(side_effect=AssertionError("memory_answer")),
            ) as complete,
            patch.object(
                Runtime,
                "execute",
                AsyncMock(
                    return_value={"error": "quota_exceeded", "provider_status": 432}
                ),
            ),
            TestClient(app) as client,
        ):
            reply = client.post(
                "/v1/chat/completions",
                json={
                    "messages": [
                        {
                            "role": "user",
                            "content": "Sur un Amstrad CPC, quels sont les timings de OUTI et OTIR en microsecondes ?",
                        }
                    ]
                },
            )
            self.assertEqual(reply.status_code, 503)
            self.assertIn("quota exhausted", reply.json()["error"]["message"])
            self.assertEqual(reply.json()["error"]["code"], "quota_exceeded")
            self.assertIn("432", reply.json()["error"]["message"])
            complete.assert_not_called()
            self.assertIn(
                "web_fetch",
                [
                    function.get("name")
                    for tool in model.tools
                    if isinstance(function := tool.get("function"), dict)
                ],
            )
