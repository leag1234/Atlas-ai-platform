"""A chat turn stops researching while its answer can still be written."""

from decimal import Decimal
import json
import unittest

from packages.profiles import PROFILES
from services.orchestrator.closing import (
    TIME_CLOSING_MARK,
    WRITING_RESERVE_SECONDS,
    closing_requested,
    time_closing_instruction,
)
from services.orchestrator.loop import Call, Limits, Message, Query, Turn, run
from tests.test_search_limit_closing import ANSWER, QUESTION, Research, offered, search


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class SlowResearcher:
    """Each model round takes `seconds`; it searches for as long as it may."""

    def __init__(self, clock: Clock, seconds: float) -> None:
        self.clock, self.seconds = clock, seconds
        self.seen: list[list[Message]] = []
        self.timeouts: list[float] = []

    def estimate(self, messages: list[Message]) -> object:
        from services.orchestrator.loop import Reservation

        return Reservation(10, Decimal("0.001"))

    async def complete(self, messages: list[Message], timeout: float) -> Turn:
        self.seen.append(json.loads(json.dumps(messages)))
        self.timeouts.append(timeout)
        self.clock.now += self.seconds
        if not offered(messages):
            return Turn(ANSWER)
        rounds = sum(message.get("role") == "tool" for message in messages)
        if "web_search" in offered(messages):
            return Turn("", (search(f"s{rounds + 1}"),))
        # As on staging: after its searches, the model reads pages.
        page = json.dumps({"url": f"https://example.org/page-{rounds}"})
        return Turn("", (Call(f"f{rounds + 1}", "web_fetch", page),))


async def chat_turn(model: SlowResearcher, clock: Clock, wall_clock: float = 120):
    return await run(
        Query(question=QUESTION, lang="fr"),
        model,
        Research(allowance=100),
        "",
        Limits(profile=PROFILES[0], cost=Decimal("0.05"), wall_clock=wall_clock),
        clock=clock,
        final_only=True,
    )


class TimeClosingTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_closes_before_the_writing_reserve(self) -> None:
        # Measured on staging 2026-11-13: one research round took 15 to 20 s,
        # and the answer started too late in a 120 s turn.
        clock = Clock()
        model = SlowResearcher(clock, 18)
        result = await chat_turn(model, clock)
        self.assertEqual((result.state, result.text), ("done", ANSWER))
        closing = model.seen[-1][-1]
        self.assertEqual(closing["role"], "user")
        self.assertTrue(str(closing["content"]).startswith(TIME_CLOSING_MARK))
        self.assertEqual(offered(model.seen[-1]), [])
        # The writing call kept at least the reserve of a 120 s turn.
        self.assertGreaterEqual(model.timeouts[-1], 120 / 3 - 0.001)
        self.assertLessEqual(clock.now, 120)
        # Every earlier call was offered its tools.
        for seen in model.seen[:-1]:
            self.assertTrue(offered(seen))

    async def test_a_quick_turn_is_not_closed(self) -> None:
        clock = Clock()

        class Quick(SlowResearcher):
            async def complete(self, messages: list[Message], timeout: float) -> Turn:
                self.seen.append(json.loads(json.dumps(messages)))
                self.clock.now += self.seconds
                if any(message.get("role") == "tool" for message in messages):
                    return Turn(ANSWER)
                return Turn("", (search("s1"),))

        model = Quick(clock, 5)
        result = await chat_turn(model, clock)
        self.assertEqual(result.text, ANSWER)
        self.assertFalse(any(closing_requested(seen) for seen in model.seen))

    async def test_no_research_no_closing(self) -> None:
        # A slow first call with no tool result yet is not a research turn.
        clock = Clock()
        clock.now = 0

        class Direct(SlowResearcher):
            async def complete(self, messages: list[Message], timeout: float) -> Turn:
                self.seen.append(json.loads(json.dumps(messages)))
                self.clock.now += self.seconds
                return Turn(ANSWER)

        model = Direct(clock, 100)
        result = await chat_turn(model, clock)
        self.assertEqual(result.text, ANSWER)
        self.assertEqual(len(model.seen), 1)

    def test_the_instruction_closes_the_tools_and_keeps_the_language(self) -> None:
        text = time_closing_instruction("fr")
        self.assertTrue(text.startswith(TIME_CLOSING_MARK))
        self.assertIn("Answer in French", text)
        self.assertIn("Do not invent it", text)
        closed: list[Message] = [
            {"role": "user", "content": QUESTION},
            {"role": "tool", "tool_call_id": "s1", "content": "{}"},
            {"role": "user", "content": text},
        ]
        self.assertTrue(closing_requested(closed))
        self.assertEqual(offered(closed), [])
        self.assertGreater(WRITING_RESERVE_SECONDS, 0)


if __name__ == "__main__":
    unittest.main()
