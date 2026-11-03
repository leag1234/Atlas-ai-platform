"""A glm answer that ends in a repetition loop is trimmed to its clean prefix instead of failing."""

from collections.abc import AsyncGenerator
import os
import unittest
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
from quality import repetition_cut, stream_quality
from serverless_support import environment

PROSE = (
    "No French commune called Valbrume-les-Tilleuls appears in the official geographic code. "
    "The name may come from a novel or a game; tell me where you saw it and I can look again. "
)
LOOP = "les Tilleuls, " * 600


def request() -> AgentRequest:
    return AgentRequest.model_validate(
        {
            "messages": [
                {"role": "user", "content": "Where is Valbrume-les-Tilleuls?"}
            ],
            "tools": [],
            "timeout": 5.0,
            "local_enabled": False,
            "observe": True,
            "profile": "atlas-glm",
        }
    )


def final(text: str, finish_reason: str) -> dict[str, object]:
    return {
        "result": {
            "text": text,
            "calls": [],
            "usage": {"prompt_tokens": 20, "completion_tokens": 400},
            "finish_reason": finish_reason,
            "reasoning": "",
            "reasoning_tokens": 0,
        }
    }


def pieces(text: str, size: int = 40) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


class RepetitionCutTests(unittest.TestCase):
    def test_loop_is_cut_after_the_prose(self) -> None:
        clean = repetition_cut(PROSE + LOOP)
        assert clean is not None
        self.assertTrue(PROSE.strip().startswith(clean[:80]))
        self.assertLess(len(clean), len(PROSE) + 600)
        self.assertNotIn("les Tilleuls, les Tilleuls, les Tilleuls", clean[-200:])

    def test_prose_and_short_text_are_kept(self) -> None:
        prose = " ".join(
            f"Sentence {n} explains step {n * 7} of the water cycle."
            for n in range(120)
        )
        self.assertIsNone(repetition_cut(prose))
        self.assertIsNone(repetition_cut(LOOP[:1000]))

    def test_loop_without_prose_is_not_an_answer(self) -> None:
        self.assertIsNone(repetition_cut(LOOP))


class RepetitionStreamTests(unittest.IsolatedAsyncioTestCase):
    async def run_stream(
        self, text: str, finish_reason: str
    ) -> tuple[str, dict[str, object]]:
        self.read = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            for piece in pieces(text):
                self.read += len(piece)
                yield {"delta": {"content": piece}}
            yield final(text, finish_reason)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            events = [e async for e in stream_quality(AgentProvider(), request())]
        streamed = "".join(
            str(delta.get("content", ""))
            for e in events
            if isinstance(delta := e.get("delta"), dict)
        )
        answer = events[-1]["result"]
        assert isinstance(answer, dict)
        return streamed, answer

    async def test_length_loop_returns_the_clean_answer(self) -> None:
        streamed, answer = await self.run_stream(PROSE + LOOP, "length")
        self.assertTrue(PROSE.strip().startswith(str(answer["text"])[:80]))
        self.assertLess(len(str(answer["text"])), len(PROSE) + 600)
        # The loop stops reaching the client soon after it starts.
        self.assertLess(len(streamed), len(PROSE) + 2500)

    async def test_regular_answer_that_looks_repetitive_is_delivered_whole(
        self,
    ) -> None:
        table = "| a | b |\n" * 400
        streamed, answer = await self.run_stream(PROSE + table, "stop")
        self.assertEqual(answer["text"], PROSE + table)
        self.assertEqual(streamed, PROSE + table)

    async def test_confirmed_loop_closes_the_provider_stream(self) -> None:
        # A loop that would run 26 thousand characters is not read to its end;
        # its unknown usage is billed as the full reservation.
        loop = "les Tilleuls, " * 1900
        streamed, answer = await self.run_stream(PROSE + loop, "length")
        self.assertLess(self.read, len(PROSE) + 12000)
        self.assertTrue(PROSE.strip().startswith(str(answer["text"])[:80]))
        self.assertLess(len(streamed), len(PROSE) + 2500)
        self.assertTrue(answer.get("token_split_estimated"))

    async def test_long_regular_table_is_still_delivered_whole(self) -> None:
        table = "| a | b |\n" * 900
        streamed, answer = await self.run_stream(PROSE + table, "stop")
        self.assertEqual(answer["text"], PROSE + table)
        self.assertEqual(self.read, len(PROSE + table))
