"""Every limit that ends research reaches the model with its own cause (D5).

The fourth search of one chat answer is refused by a fixed step limit of the
answer. Told only "tool_quota_exhausted", a model wrote "search quota
exhausted" in its study (staging, 2026-11-19): no quota was involved.
"""

from decimal import Decimal
import json
import re
from typing import Literal
import unittest

from packages.profiles import PROFILES
from services.orchestrator.closing import (
    SEARCH_LIMITS,
    STEP_CLOSING_MARK,
    STEP_LIMIT_CODE,
    STEP_LIMITS,
    closing_instruction,
    closing_requested,
    step_closing_instruction,
    step_limit_result,
    time_closing_instruction,
)
from services.orchestrator.loop import Limits, Message, Query, Result, Turn, run
from test_search_limit_closing import (
    ANSWER,
    QUESTION,
    Research,
    Scripted,
    offered,
    search,
)

COUNTS = {"searches": 3, "fetches": 0, "tools": 3}
QUOTA = re.compile(r"\bquota\b", re.IGNORECASE)


async def chat_turn(model: Scripted, lang: Literal["fr", "en"] = "fr") -> Result:
    return await run(
        Query(question=QUESTION, lang=lang),
        model,
        Research(allowance=10),
        "",
        Limits(profile=PROFILES[0], cost=Decimal("0.05")),
        final_only=True,
    )


def told(messages: list[Message]) -> str:
    """Everything the model reads after its own last request for tools."""
    return json.dumps(messages, ensure_ascii=False)


class WordingTests(unittest.TestCase):
    def test_a_step_limit_is_never_called_a_quota(self) -> None:
        for reason in STEP_LIMITS:
            with self.subTest(reason=reason):
                result = step_limit_result(reason, COUNTS)
                self.assertEqual(result["error"], STEP_LIMIT_CODE)
                self.assertNotIn("quota", str(result["error"]))
                message = str(result["message"])
                self.assertIn("the most one answer", message)
                self.assertIn("not a quota", message)
                instruction = step_closing_instruction(reason, COUNTS, "fr")
                self.assertTrue(instruction.startswith(STEP_CLOSING_MARK))
                self.assertIn("search works again in the next message", instruction)
                self.assertIn("Answer in French", instruction)
                # The only "quota" the model reads is the one it must not say.
                for text in (message, instruction):
                    for found in QUOTA.finditer(text):
                        before = text[max(0, found.start() - 30) : found.start()]
                        self.assertRegex(before, r"not a $|call it a $")
        self.assertIn(
            "3 web searches", str(step_limit_result("search_limit", COUNTS)["message"])
        )

    def test_each_limit_has_its_own_cause(self) -> None:
        causes = {
            "daily_search_limit": "allowance of this account",
            "quota_exceeded": "quota of the web search service",
            "search_rate_limited": "limiting requests",
        }
        for code, cause in causes.items():
            self.assertIn(cause, SEARCH_LIMITS[code])
            self.assertIn(cause, closing_instruction(code, "en"))
        time_text = time_closing_instruction("en")
        self.assertIn("it is the time of this answer", time_text)
        self.assertIn("never call it a quota", time_text)
        step = step_closing_instruction("search_limit", COUNTS, "en")
        self.assertIn("this answer has run 3 web searches", step)
        self.assertNotIn("quota of the web search service", step)
        self.assertNotIn("allowance of this account", step)

    def test_the_step_closing_closes_the_tool_set(self) -> None:
        messages: list[Message] = [
            {"role": "user", "content": QUESTION},
            {
                "role": "tool",
                "tool_call_id": "s4",
                "content": json.dumps(step_limit_result("search_limit", COUNTS)),
            },
            {
                "role": "user",
                "content": step_closing_instruction("search_limit", COUNTS, "en"),
            },
        ]
        self.assertTrue(closing_requested(messages))
        self.assertEqual(offered(messages), [])


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_fourth_search_closes_the_answer_with_its_cause(self) -> None:
        model = Scripted(
            [
                Turn("", (search("s1"), search("s2"), search("s3"))),
                Turn("", (search("s4"), search("s5"))),
                Turn(ANSWER),
            ]
        )
        result = await chat_turn(model)
        self.assertEqual((result.state, result.text), ("done", ANSWER))
        self.assertEqual(result.tool_calls, 3)
        closing = model.seen[2]
        refusals = [
            json.loads(str(m["content"])) for m in closing if m.get("role") == "tool"
        ][3:]
        self.assertEqual(len(refusals), 2)
        for refusal in refusals:
            self.assertEqual(refusal["error"], STEP_LIMIT_CODE)
            self.assertEqual(refusal["reason"], "search_limit")
            self.assertIn("3 web searches", refusal["message"])
        self.assertEqual(
            closing[-1]["content"],
            step_closing_instruction("search_limit", COUNTS, "fr"),
        )
        self.assertEqual(offered(closing), [])
        self.assertNotIn("tool_quota_exhausted", told(closing))

    async def test_a_tool_asked_after_the_step_closing_is_never_run(self) -> None:
        model = Scripted(
            [
                Turn("", (search("s1"), search("s2"), search("s3"))),
                Turn("", (search("s4"),)),
                Turn("", (search("s5"),)),
            ]
        )
        result = await chat_turn(model, "en")
        self.assertEqual((result.state, result.reason), ("stopped", STEP_LIMIT_CODE))
        self.assertIn("Searches: 3; limit 3", result.text)
        self.assertNotIn("quota", result.text)


if __name__ == "__main__":
    unittest.main()
