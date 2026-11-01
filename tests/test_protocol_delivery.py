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
