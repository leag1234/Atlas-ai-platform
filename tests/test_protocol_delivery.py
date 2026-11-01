"""Synthetic protocol-leak regressions for the bounded agent loop."""

from decimal import Decimal
import unittest

from services.orchestrator.loop import Call, Limits, Query, Reservation, Turn, run


class FixtureModel:
    def __init__(self, turns: list[Turn]) -> None:
        self.turns = turns
        self.tools = [
            {"type": "function", "function": {"name": "web_search"}},
        ]
        self.messages: list[list[dict[str, object]]] = []

    def estimate(self, messages: list[dict[str, object]]) -> Reservation:
        self.messages.append(messages.copy())
        return Reservation(0, Decimal(0))

    async def complete(self, messages: list[dict[str, object]], timeout: float) -> Turn:
        return self.turns.pop(0)


class FixtureTools:
    def __init__(self) -> None:
        self.calls: list[Call] = []

    def estimate(self, call: Call) -> Reservation:
        return Reservation(0, Decimal(0))

    async def execute(self, call: Call, timeout: float) -> dict[str, object]:
        self.calls.append(call)
        return {"data": {"results": []}}


class ProtocolDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_offered_tool_call_executes_before_a_real_answer(self) -> None:
        model = FixtureModel(
            [
                Turn('{"name":"web_search","arguments":{"query":"weather"}}'),
                Turn("The retrieved result is unavailable."),
            ]
        )
        tools = FixtureTools()
        result = await run(
            Query(question="Weather?", lang="en"), model, tools, "system", Limits()
        )
        self.assertEqual(result.state, "done")
        self.assertEqual(result.text, "The retrieved result is unavailable.")
        self.assertEqual([call.name for call in tools.calls], ["web_search"])

    async def test_scope_text_gets_one_bounded_recovery_completion(self) -> None:
        model = FixtureModel(
            [
                Turn('Before answering: {"scope":"CURRENT REQUEST: hidden"}'),
                Turn("The answer is bounded and complete."),
            ]
        )
        result = await run(
            Query(question="Question", lang="en"),
            model,
            FixtureTools(),
            "system",
            Limits(),
        )
        self.assertEqual(result.state, "done")
        self.assertEqual(result.text, "The answer is bounded and complete.")
        self.assertEqual(len(model.messages), 2)
        self.assertNotIn("CURRENT REQUEST", str(model.messages[1]))

    async def test_search_announcement_gets_one_bounded_recovery_completion(
        self,
    ) -> None:
        model = FixtureModel(
            [
                Turn("I need to search for current sources."),
                Turn("No current source was found."),
            ]
        )
        result = await run(
            Query(question="Question", lang="en"),
            model,
            FixtureTools(),
            "system",
            Limits(),
        )
        self.assertEqual(result.state, "done")
        self.assertEqual(result.text, "No current source was found.")

    async def test_second_protocol_completion_stops_honestly(self) -> None:
        model = FixtureModel(
            [
                Turn('{"untrusted_tool_history":{"role":"assistant"}}'),
                Turn('{"untrusted_tool_history":{"role":"assistant"}}'),
            ]
        )
        result = await run(
            Query(question="Question", lang="en"),
            model,
            FixtureTools(),
            "system",
            Limits(),
        )
        self.assertEqual(result.state, "stopped")
        self.assertEqual(result.reason, "protocol_completion_limit")
        self.assertIn("protocol_completion_limit", result.text)


class WorkFixtureTools(FixtureTools):
    def __init__(self) -> None:
        super().__init__()
        self.deliverables: list[dict[str, object]] = []


class WorkFilePromiseTests(unittest.IsolatedAsyncioTestCase):
    def limits(self) -> Limits:
        return Limits(
            profile="atlas-glm",
            tokens=None,
            tool_calls=40,
            wall_clock=600,
            cost=Decimal("1"),
            code_mode=True,
            model_calls=41,
        )

    async def run_work(self, turns: list[Turn], tools: FixtureTools) -> tuple:
        import os
        from unittest.mock import patch

        model = FixtureModel(turns)
        with patch.dict(os.environ, {"ATLAS_CODE_MODE": "1", "ATLAS_STABLE": "0"}):
            result = await run(
                Query(
                    question="Produis un document Word compte-rendu.docx.", lang="fr"
                ),
                model,
                tools,
                "system",
                self.limits(),
            )
        return result, model

    async def test_announced_file_without_deliverable_gets_one_retry(self) -> None:
        result, model = await self.run_work(
            [
                Turn("Je vais créer le document Word compte-rendu.docx."),
                Turn("Le document est prêt."),
            ],
            WorkFixtureTools(),
        )
        self.assertEqual(result.text, "Le document est prêt.")
        self.assertIn("did not create it", str(model.messages[1][-1]))

    async def test_second_announcement_is_not_retried_again(self) -> None:
        result, model = await self.run_work(
            [
                Turn("Je vais créer le document Word."),
                Turn("Je vais créer le document Word maintenant."),
            ],
            WorkFixtureTools(),
        )
        self.assertEqual(len(model.messages), 2)
        self.assertNotEqual(result.text, "")

    async def test_tools_without_deliverables_never_retry(self) -> None:
        result, model = await self.run_work(
            [Turn("Je vais créer le document Word compte-rendu.docx.")],
            FixtureTools(),
        )
        self.assertEqual(len(model.messages), 1)
