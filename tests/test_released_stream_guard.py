"""Internal text never reaches the person from a released answer (class C4).

An answer-shaped turn streams before its call ends (narration.TurnHold, #605),
so it skips the loop's end-of-call checks. Each paragraph is now checked before
it is sent; the first internal one stops the stream, and the rest of the turn
is sent at its end without its internal paragraphs. The final answer of every
path (UI, API stream, API without stream) drops internal paragraphs too.
"""

from collections.abc import Awaitable, Callable
import json
import os
import tempfile
import time
import unittest
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.orchestrator.chat_api import app
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.diagram import DeliveryBuffer
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Query, run
from services.orchestrator.narration import (
    TurnHold,
    internal_paragraph,
    paragraphs,
    without_internal,
)
from services.orchestrator.stream_client import sink_context
from test_working_notes import CALL, NOTES, Calculator, model, scripted, shown

OPENING = (
    "The fictional port of Valmonde handles about 4,000 ships a year. Its two "
    "basins were dug in different centuries and serve different trades.\n\n"
    "The old basin takes fishing boats and ferries; the new one, deeper, takes "
    "the container ships that call twice a week.\n\n"
    "A ring road keeps lorries out of the old town, and a rail spur takes half "
    "of the containers inland, so the quays stay clear even on busy days.\n\n"
)
CLOSING = "Both basins share one pilot service, which works around the clock."
INTERNAL = {
    "tool_call tag": "<tool_call>\n<function=web_search>\n<parameter=query>Valmonde port</parameter>\n</function>\n</tool_call>\n\n",
    "stop reason": "Explicit stop: loop_detected. Tools executed: 2; limit 10.\n\n",
    "error envelope": '{"error": {"message": "stopped", "type": "request_stopped", "code": "request_stopped"}}\n\n',
}
LEAKS = (
    "tool_call",
    "function=",
    "Explicit stop",
    "Tools executed",
    '"error"',
    "request_stopped",
)


def leaked(text: str) -> list[str]:
    return [leak for leak in LEAKS if leak in text]


class GuardTests(unittest.TestCase):
    def test_internal_paragraphs_are_recognised(self) -> None:
        for name, block in INTERNAL.items():
            with self.subTest(name=name):
                self.assertTrue(internal_paragraph(block))
        self.assertTrue(internal_paragraph("I will search the web for the figures."))
        for part in paragraphs(OPENING + CLOSING):
            self.assertFalse(internal_paragraph(part))

    def test_fenced_code_is_the_answer_s_own(self) -> None:
        code = "Run this:\n\n```python\nraise ValueError('x')\n\n# Traceback (most recent call last)\n```\n\nDone."
        self.assertEqual(without_internal(code), code)
        self.assertEqual(len(paragraphs(code)), 3)

    def test_the_final_answer_drops_internal_paragraphs(self) -> None:
        for name, block in INTERNAL.items():
            with self.subTest(name=name):
                kept = without_internal(OPENING + block + CLOSING)
                self.assertEqual(leaked(kept), [])
                self.assertIn(CLOSING, kept)


class HoldTests(unittest.IsolatedAsyncioTestCase):
    async def run_hold(self, pieces: list[str], *, final: bool = True) -> list[object]:
        events: list[object] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        hold = TurnHold(sink)
        for piece in pieces:
            await hold({"delta": {"content": piece}})
        if final:
            await hold.release()
        return events

    async def test_a_raw_block_midway_stops_the_stream(self) -> None:
        for name, block in INTERNAL.items():
            with self.subTest(name=name):
                text = OPENING + block + CLOSING
                pieces = [text[i : i + 7] for i in range(0, len(text), 7)]
                live = await self.run_hold(pieces, final=False)
                self.assertTrue(shown(live).startswith(OPENING[:100]))
                self.assertEqual(leaked(shown(live)), [])
                events = await self.run_hold(pieces)
                self.assertEqual(leaked(shown(events)), [])
                self.assertTrue(shown(events).rstrip().endswith(CLOSING))

    async def test_a_held_answer_is_released_without_internal_text(self) -> None:
        events = await self.run_hold(["Short answer.\n\n", INTERNAL["stop reason"]])
        self.assertEqual(shown(events).strip(), "Short answer.")


Process = Callable[[ChatRequest, Interaction], Awaitable[None]]


class TurnPathTests(unittest.IsolatedAsyncioTestCase):
    """A researched turn through the loop, the hold and the stream."""

    def process(self, final_text: str) -> Process:
        turns = [(NOTES[0], [CALL]), (final_text, [])]

        async def process(payload: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            assert sink is not None
            buffer = DeliveryBuffer(sink)
            with patch("services.orchestrator.model.receive", new=scripted(turns, [])):
                result = await run(
                    Query(question="Tell me about Valmonde's port", lang="en"),
                    model(buffer, "after_tool"),
                    Calculator(),
                    "",
                    final_only=True,
                )
            item.reponse, item.state = result.text, result.state
            await buffer.finish(item.reponse)

        return process

    async def streamed(self, final_text: str, *, atlas_ui: bool) -> str:
        payload = ChatRequest.model_validate(
            {
                "atlas_ui": atlas_ui,
                "stream": True,
                "ui_locale": "en",
                "messages": [
                    {"role": "user", "content": "Tell me about Valmonde's port"}
                ],
            }
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": directory}),
        ):
            rows: list[dict[str, Any]] = [
                json.loads(str(row).removeprefix("data: "))
                async for row in response(
                    payload, Interaction(), time.monotonic(), self.process(final_text)
                ).body_iterator
                if str(row).startswith("data: {")
            ]
        return "".join(
            str(choice.get("delta", {}).get("content", ""))
            for row in rows
            for choice in row.get("choices", [])
        )

    async def test_the_interface_never_shows_a_raw_block(self) -> None:
        for name in ("tool_call tag", "stop reason"):
            with self.subTest(name=name):
                text = await self.streamed(
                    OPENING + INTERNAL[name] + CLOSING, atlas_ui=True
                )
                self.assertEqual(leaked(text), [])
                self.assertIn(OPENING[:60], text)

    async def test_the_api_stream_never_carries_a_raw_block(self) -> None:
        for name in ("tool_call tag", "stop reason"):
            with self.subTest(name=name):
                text = await self.streamed(
                    OPENING + INTERNAL[name] + CLOSING, atlas_ui=False
                )
                self.assertEqual(leaked(text), [])
                self.assertIn(CLOSING, text)


class NonStreamTests(unittest.TestCase):
    def test_the_api_answer_never_carries_a_raw_block(self) -> None:
        async def provider(request: ChatRequest, item: Interaction) -> None:
            item.state = "done"
            item.reponse = OPENING + INTERNAL["tool_call tag"] + CLOSING

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch("services.orchestrator.chat_api.process", side_effect=provider),
            TestClient(app) as client,
        ):
            reply = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "Valmonde's port?"}]},
            )
        self.assertEqual(reply.status_code, 200)
        content = reply.json()["choices"][0]["message"]["content"]
        self.assertEqual(leaked(content), [])
        self.assertIn(CLOSING, content)


if __name__ == "__main__":
    unittest.main()
