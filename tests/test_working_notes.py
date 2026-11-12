"""Text a model writes before a tool call is a working note, never answer text."""

from collections.abc import Awaitable, Callable
from decimal import Decimal
import json
from typing import Literal
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.diagram import DeliveryBuffer
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call, Message, Query, Reservation, run
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.narration import TurnHold
from services.orchestrator.stream_client import Sink, sink_context

NOTES = (
    "I found relevant results. I will fetch the most recent article for "
    "detailed reading.",
    "La page est derrière un mur payant. Je vais lire le second article.",
    "検索結果を確認しました。次に公式ページを読みます。",
)
ANSWER = "As of this autumn, the fictional Harbour Act applies in three stages."
END_OF_STREAM = {"marker": "end_of_stream"}
CALL = {"id": "c1", "name": "calculator", "arguments": '{"expr":"1+1"}'}
FIRST: list[Message] = [{"role": "user", "content": "What changed?"}]
AFTER_TOOL: list[Message] = [
    *FIRST,
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "c0",
                "type": "function",
                "function": {"name": "calculator", "arguments": '{"expr":"2"}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "c0", "content": '{"value": "2"}'},
]


def scripted(
    turns: list[tuple[str, list[dict[str, str]]]], seen: list[object]
) -> Callable[[str, dict[str, object], float, Sink], Awaitable[dict[str, object]]]:
    """A gateway stream: each turn's text in small pieces, then its result."""
    pending = list(turns)

    async def receive(
        url: str, payload: dict[str, object], timeout: float, sink: Sink
    ) -> dict[str, object]:
        text, calls = pending.pop(0)
        for start in range(0, len(text), 7):
            await sink({"delta": {"content": text[start : start + 7]}})
        seen.append(END_OF_STREAM)
        return {
            "text": text,
            "calls": calls,
            "usage": {"prompt_tokens": 20, "completion_tokens": 20},
        }

    return receive


def model(
    sink: Sink, note_hold: Literal["off", "after_tool", "turn"] = "after_tool"
) -> GatewayModel:
    built = GatewayModel(
        "http://127.0.0.1:1",
        Configuration(
            input_eur_per_mtok=Decimal("0.6"),
            output_eur_per_mtok=Decimal("3.6"),
            max_tokens=2048,
        ),
    )
    built.sink, built.note_hold = sink, note_hold
    return built


def shown(events: list[object]) -> str:
    return "".join(
        str(event["delta"].get("content", ""))
        for event in events
        if isinstance(event, dict) and isinstance(event.get("delta"), dict)
    )


class TurnHoldTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_dropped_turn_never_reaches_the_sink(self) -> None:
        events: list[object] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        hold = TurnHold(sink)
        for note in NOTES:
            await hold({"delta": {"content": note * 40}})
        hold.drop()
        await hold.release()
        self.assertEqual(events, [])

    async def test_a_released_turn_is_delivered_in_order(self) -> None:
        events: list[object] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        hold = TurnHold(sink)
        await hold({"delta": {"content": "One. "}})
        await hold({"delta": {"content": "Two."}})
        self.assertEqual(events, [])
        await hold.release()
        self.assertEqual(shown(events), "One. Two.")


class ModelTurnTests(unittest.IsolatedAsyncioTestCase):
    async def complete(
        self,
        text: str,
        calls: list[dict[str, str]],
        messages: list[Message],
        note_hold: Literal["off", "after_tool", "turn"] = "after_tool",
        *,
        tools: bool = True,
    ) -> tuple[list[object], str]:
        events: list[object] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        built = model(sink, note_hold)
        if not tools:
            built.tools = []
        with patch(
            "services.orchestrator.model.receive", new=scripted([(text, calls)], events)
        ):
            turn = await built.complete(messages, 100)
        return events, turn.text

    async def test_a_note_after_a_tool_result_is_not_streamed(self) -> None:
        for note in (*NOTES, NOTES[0] * 40):
            with self.subTest(note=note[:20], length=len(note)):
                events, text = await self.complete(note, [CALL], AFTER_TOOL)
                self.assertEqual(shown(events), "")
                # The loop still records what the model said with its call.
                self.assertEqual(text, note)
                phase = events[-1]
                assert isinstance(phase, dict)
                self.assertEqual(phase["phase"], "intermediate")

    async def test_a_research_turn_holds_its_first_turn_too(self) -> None:
        for note in NOTES:
            with self.subTest(note=note[:20]):
                events, _ = await self.complete(note, [CALL], FIRST, "turn")
                self.assertEqual(shown(events), "")
        events, _ = await self.complete(ANSWER, [], FIRST, "turn")
        self.assertEqual(shown(events[: events.index(END_OF_STREAM)]), "")
        self.assertEqual(shown(events), ANSWER)

    async def test_the_answer_after_a_tool_result_is_delivered_whole(self) -> None:
        events, _ = await self.complete(ANSWER * 20, [], AFTER_TOOL)
        self.assertEqual(shown(events), ANSWER * 20)
        # Nothing before the turn was known to be the answer.
        self.assertEqual(shown(events[: events.index(END_OF_STREAM)]), "")
        phase = events[-1]
        assert isinstance(phase, dict)
        self.assertEqual(phase["phase"], "final")

    async def test_a_plain_answer_streams_live(self) -> None:
        events, _ = await self.complete(ANSWER, [], FIRST)
        self.assertEqual(shown(events[: events.index(END_OF_STREAM)]), ANSWER)

    async def test_a_turn_without_tools_streams_live(self) -> None:
        for note_hold in ("after_tool", "turn"):
            with self.subTest(note_hold=note_hold):
                events, _ = await self.complete(
                    ANSWER, [], AFTER_TOOL, note_hold, tools=False
                )
                live = shown(events[: events.index(END_OF_STREAM)])
                self.assertEqual(live, ANSWER)

    async def test_other_callers_are_unchanged(self) -> None:
        events, _ = await self.complete(NOTES[0], [CALL], AFTER_TOOL, "off")
        self.assertEqual(shown(events), NOTES[0])


class Calculator:
    """Announces its step as the chat tools do."""

    def estimate(self, call: Call) -> Reservation:
        return Reservation(0, Decimal(0))

    async def execute(self, call: Call, timeout: float) -> Message:
        sink = sink_context.get()
        assert sink is not None
        await sink({"phase": "tool_started", "tool": call.name, "urls": []})
        await sink({"phase": "tool_finished", "tool": call.name, "urls": []})
        return {"value": "2"}


class TranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def transcript(
        self,
        turns: list[tuple[str, list[dict[str, str]]]],
        note_hold: Literal["off", "after_tool", "turn"] = "turn",
    ) -> str:
        async def process(payload: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            assert sink is not None
            buffer = DeliveryBuffer(sink)
            result = await run(
                Query(question="What changed?", lang="en"),
                model(buffer, note_hold),
                Calculator(),
                "",
                final_only=True,
            )
            item.reponse, item.state = result.text, result.state
            await buffer.finish(item.reponse)

        payload = ChatRequest.model_validate(
            {
                "atlas_ui": True,
                "stream": True,
                "ui_locale": "en",
                "messages": [{"role": "user", "content": "What changed?"}],
            }
        )
        item = Interaction()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
            patch("services.orchestrator.model.receive", new=scripted(turns, [])),
        ):
            rows = [
                json.loads(str(row).removeprefix("data: "))
                async for row in response(
                    payload, item, time.monotonic(), process
                ).body_iterator
                if str(row).startswith("data: {")
            ]
        self.assertEqual(item.reponse, turns[-1][0])
        return "".join(
            row.get("choices", [{}])[0].get("delta", {}).get("content", "")
            for row in rows
        )

    async def test_a_research_turn_shows_steps_and_the_answer_only(self) -> None:
        text = await self.transcript(
            [
                (NOTES[0], [CALL]),
                (NOTES[1], [dict(CALL, id="c2", arguments='{"expr":"2+2"}')]),
                (NOTES[2], [dict(CALL, id="c3", arguments='{"expr":"3+3"}')]),
                (ANSWER, []),
            ]
        )
        for note in NOTES:
            self.assertNotIn(note[:20], text)
        self.assertEqual(text.count(ANSWER), 1)
        self.assertTrue(text.rstrip().endswith(ANSWER))
        # Steps stay visible as steps.
        self.assertEqual(text.count("<details>"), 6)

    async def test_an_answer_with_a_link_bracket_is_complete(self) -> None:
        # The delivery buffer holds text after a bracket until the final text is
        # validated against what it sent: a note must not count as sent text.
        answer = "The rule has [three] stages. " + ANSWER
        text = await self.transcript(
            [
                (NOTES[0], [CALL]),
                (
                    "Checking `the` [figures] first. " * 30,
                    [dict(CALL, id="c2", arguments='{"expr":"2+2"}')],
                ),
                (answer, []),
            ]
        )
        self.assertNotIn("Checking", text)
        self.assertTrue(text.rstrip().endswith(answer))

    async def test_prose_shown_before_a_first_call_keeps_the_answer_whole(self) -> None:
        # A first turn nobody expected to call a tool streams live: its text is
        # already shown. The final answer is still delivered once, in full.
        answer = "The rule has [three] stages. " + ANSWER
        text = await self.transcript(
            [("An [opening] passage. ", [CALL]), (answer, [])], "after_tool"
        )
        self.assertIn("An ", text)
        self.assertEqual(text.count(answer), 1)
        self.assertTrue(text.rstrip().endswith(answer))


if __name__ == "__main__":
    unittest.main()
