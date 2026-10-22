"""M22 required discovery before provider generation, independent of wording."""

import unittest
from services.orchestrator.search_policy import required_research


class SearchPolicyTests(unittest.TestCase):
    def test_changing_facts(self) -> None:
        for text in (
            "quel est le prix de l'IPO de SpaceX",
            "Qui dirige Walmart ?",
            "Who runs Walmart?",
            "Recherche sur le web puis lis une source : quand le CERN a-t-il rendu le Web public ?",
            "Toujours disponible ?",
            "QUEL EST LE PDG DE WALMART",
            "Wer leitet Walmart?",
            "Is this still sold?",
            "quelle est la derniere version de Python",
        ):
            self.assertEqual(required_research(text, "fr")[0].name, "web_search", text)

    def test_practical_source_mismatch(self) -> None:
        for text in (
            "comment fabriquer cette camera",
            "How do I build it?",
            "Wie baue ich das?",
            "Compare ces appareils",
            "COMMENT CONSTRUIRE CET APPAREIL",
            "Recommande une methode",
        ):
            calls = required_research(
                "https://example.org/solargraphy-camera " + text, "fr"
            )
            self.assertEqual([c.name for c in calls], ["web_fetch", "web_search"])
            self.assertIn("solargraphy camera", calls[1].arguments)

    def test_stable_negative_cases(self) -> None:
        for text in (
            "2 + 2",
            "Explain a triangle.",
            "What is the current internal project price?",
            "Quel est le prix du contrat confidentiel ?",
            "Traduis bonjour.",
            "Résume https://example.org/article",
        ):
            self.assertEqual(required_research(text, "fr"), ())


class RequiredResearchLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_is_budgeted_before_generation(self) -> None:
        from decimal import Decimal
        from unittest.mock import AsyncMock, Mock
        from services.orchestrator.loop import Limits, Query, Reservation, Turn, run

        order = []

        async def discover(*args: object) -> dict[str, object]:
            order.append("search")
            return {"trust": "untrusted", "data": {"results": []}}

        async def generate(*args: object) -> Turn:
            order.append("answer")
            return Turn(
                "No verified price was found.", (), Reservation(1, Decimal("0.001"))
            )

        tools = Mock(
            estimate=Mock(return_value=Reservation(0, Decimal("0.002"))),
            execute=AsyncMock(side_effect=discover),
        )
        model = Mock(
            estimate=Mock(return_value=Reservation(1, Decimal("0.001"))),
            complete=AsyncMock(side_effect=generate),
        )
        initial = required_research("Current price?", "en")
        answer = await run(
            Query(question="Current price?", lang="en"),
            model,
            tools,
            "",
            initial_calls=initial,
        )
        self.assertEqual(order, ["search", "answer"])
        self.assertEqual(answer.cost, Decimal("0.003"))
        self.assertEqual(answer.tool_calls, 1)
        order.clear()
        answer = await run(
            Query(question="Current price?", lang="en"),
            model,
            tools,
            "",
            Limits(cost=Decimal("0.001")),
            initial_calls=initial,
        )
        self.assertEqual(order, [])
        self.assertEqual(answer.reason, "cost")
