"""The person sees a researched answer while it is written (strict L4, 2026-11-21).

A researched turn's answer call was held whole until it ended, and a checked
factual answer until its check: 90 to 140 s with nothing but a status, then the
whole answer at once. Held text that takes the shape of an answer now streams;
an answer held for its check shows its progress in the status line.
"""

from collections.abc import AsyncIterable
import json
import os
import tempfile
import time
import unittest
from typing import Any
from unittest.mock import patch

from services.orchestrator.chat_pipeline import _HeldAnswer
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction
from services.orchestrator.narration import TurnHold, answer_shaped
from services.orchestrator.stream_client import sink_context
from test_working_notes import (
    AFTER_TOOL,
    CALL,
    END_OF_STREAM,
    NOTES,
    model,
    scripted,
    shown,
)

ANSWER = (
    "## Heat pumps for an older house\n\n"
    "An air-to-water pump feeds the existing radiators and the hot water tank. "
    "It suits a house that already has a wet heating circuit, but radiators sized "
    "for a boiler may need a higher water temperature, which lowers its efficiency.\n\n"
    "An air-to-air pump blows warm air into the rooms and can cool them in summer. "
    "It needs no plumbing work, but it does not heat the domestic water."
)


class AnswerShapeTests(unittest.TestCase):
    def test_notes_are_never_answer_shaped(self) -> None:
        for note in (*NOTES, NOTES[0] * 40, NOTES[1] + "\n\n"):
            with self.subTest(note=note[:20], length=len(note)):
                self.assertFalse(answer_shaped(note))

    def test_an_answer_in_paragraphs_or_lists_is(self) -> None:
        self.assertTrue(answer_shaped(ANSWER))
        listed = "The main points:\n" + "".join(
            f"- point {n}: a sentence long enough to be read as part of an answer\n"
            for n in range(8)
        )
        self.assertTrue(answer_shaped(listed))
        self.assertFalse(answer_shaped(ANSWER[:200]))


class HeldTurnTests(unittest.IsolatedAsyncioTestCase):
    async def complete(self, calls: list[dict[str, str]]) -> list[object]:
        events: list[object] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        built = model(sink, "after_tool")
        with patch(
            "services.orchestrator.model.receive",
            new=scripted([(ANSWER, calls)], events),
        ):
            await built.complete(AFTER_TOOL, 100)
        return events

    async def test_an_answer_after_a_tool_result_streams_before_the_call_ends(
        self,
    ) -> None:
        events = await self.complete([])
        before_end = shown(events[: events.index(END_OF_STREAM)])
        self.assertTrue(ANSWER.startswith(before_end))
        # Whole paragraphs stream as soon as each is checked (class C4); the
        # paragraph still being written waits for its end.
        self.assertTrue(before_end.endswith("\n\n"))
        self.assertGreaterEqual(len(before_end), 200)
        self.assertEqual(shown(events), ANSWER)

    async def test_an_answer_shaped_text_before_a_tool_call_is_logged(self) -> None:
        with self.assertLogs("services.orchestrator.narration", "WARNING") as logs:
            await self.complete([CALL])
        self.assertIn("answer_shaped_note", logs.output[0])

    async def test_a_released_hold_passes_every_later_event(self) -> None:
        events: list[object] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        hold = TurnHold(sink)
        await hold({"delta": {"content": ANSWER}})
        await hold({"delta": {"content": " More.\n\n"}})
        self.assertEqual(shown(events), ANSWER + " More.\n\n")


class CheckedAnswerProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_checked_answer_counts_its_words_never_shows_them(self) -> None:
        events: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        holder = _HeldAnswer(sink)
        for word in ("word " * 60).split():
            await holder({"delta": {"content": word + " "}})
        self.assertEqual(
            [e["answer_progress"] for e in events],
            [{"words": 25}, {"words": 50}],
        )
        self.assertFalse(any("delta" in e for e in events))

    async def test_the_status_line_shows_the_words_written(self) -> None:
        async def provider(request: ChatRequest, item: Interaction) -> None:
            stream = sink_context.get()
            assert stream is not None
            await stream({"answer_progress": {"words": 75}})
            item.state, item.reponse = "done", "Checked answer."

        request = ChatRequest.model_validate(
            {
                "stream": True,
                "atlas_ui": True,
                "ui_locale": "en-GB",
                "messages": [{"role": "user", "content": "When was the bridge built?"}],
            }
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ, {"ATLAS_INTERACTION_DIR": directory, "ATLAS_STABLE": "1"}
            ),
        ):
            body: AsyncIterable[Any] = response(
                request, Interaction(), time.monotonic(), provider
            ).body_iterator
            frames = [
                json.loads(str(frame)[6:])
                async for frame in body
                if str(frame).startswith("data: {")
            ]
        statuses = [
            frame["event"]["data"]["description"]
            for frame in frames
            if isinstance(frame.get("event"), dict)
        ]
        self.assertTrue(any("75 words so far" in s for s in statuses), statuses)
        content = "".join(
            str(c["delta"].get("content", ""))
            for f in frames
            for c in f.get("choices", [])
        )
        self.assertIn("Checked answer.", content)


if __name__ == "__main__":
    unittest.main()
