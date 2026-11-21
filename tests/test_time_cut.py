"""A turn cut by a time limit never ends empty (services/orchestrator/time_cut.py)."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
import json
import os
import tempfile
import time
import unittest
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from packages import profiles
from services.orchestrator import time_cut
from services.orchestrator.chat_api import app
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Message
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.stream_client import Sink, sink_context

ANSWER = (
    "The fictional town of Valmonde has about 12,400 inhabitants.\n\n"
    "Its market opens on Saturdays and its "
)
QUESTION: list[Message] = [{"role": "user", "content": "Tell me about Valmonde"}]
AFTER_TOOL: list[Message] = [
    *QUESTION,
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
SHORT = property(lambda self: 1.0)


def slow_writer(
    text: str,
) -> Callable[[str, dict[str, object], float, Sink], Awaitable[dict[str, object]]]:
    """A gateway stream that writes ``text`` in pieces, then never finishes."""

    async def receive(
        url: str, payload: dict[str, object], timeout: float, sink: Sink
    ) -> dict[str, object]:
        for start in range(0, len(text), 9):
            await sink({"delta": {"content": text[start : start + 9]}})
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    return receive


def writer_model(sink: Sink) -> GatewayModel:
    built = GatewayModel(
        "http://127.0.0.1:1",
        Configuration(
            input_eur_per_mtok=Decimal("0.6"),
            output_eur_per_mtok=Decimal("3.6"),
            max_tokens=2048,
        ),
    )
    # A researched turn: the answer call after a tool result is held whole.
    built.sink, built.note_hold = sink, "after_tool"
    return built


def frames_of(raw: list[str]) -> list[dict[str, Any]]:
    return [json.loads(frame[6:]) for frame in raw if frame.startswith("data: {")]


def text_of(frames: list[dict[str, Any]]) -> str:
    return "".join(
        str(choice["delta"].get("content", ""))
        for frame in frames
        for choice in frame.get("choices", [])
        if isinstance(choice, dict) and isinstance(choice.get("delta"), dict)
    )


class StreamCutTests(unittest.IsolatedAsyncioTestCase):
    async def run_turn(
        self,
        provider: Callable[[ChatRequest, Interaction], Awaitable[None]],
        *,
        atlas_ui: bool = True,
        environ: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], Interaction]:
        item = Interaction()
        request = ChatRequest.model_validate(
            {
                "stream": True,
                "atlas_ui": atlas_ui,
                "ui_locale": "en-GB",
                "messages": [{"role": "user", "content": "Tell me about Valmonde"}],
            }
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ, {"ATLAS_INTERACTION_DIR": directory, **(environ or {})}
            ),
            patch.object(ChatRequest, "timeout_seconds", SHORT),
        ):
            stream: AsyncIterator[str | bytes | memoryview[int]] = response(
                request, item, time.monotonic(), provider
            ).body_iterator.__aiter__()
            raw = [str(frame) async for frame in stream]
        self.assertEqual(raw[-1], "data: [DONE]\n\n")
        return frames_of(raw), item

    async def test_a_held_answer_cut_by_time_is_shown_with_one_sentence(
        self,
    ) -> None:
        async def provider(request: ChatRequest, item: Interaction) -> None:
            model = writer_model(sink_context.get())  # type: ignore[arg-type]
            with patch("services.orchestrator.model.receive", new=slow_writer(ANSWER)):
                await model.complete(AFTER_TOOL, 100)

        frames, item = await self.run_turn(provider)
        shown = text_of(frames)
        self.assertIn("about 12,400 inhabitants", shown)
        self.assertTrue(
            shown.rstrip().endswith(
                "The answer was cut here by the time limit (1 s) before it was finished."
            ),
            shown[-200:],
        )
        self.assertFalse(any("error" in frame for frame in frames))
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "stop")
        self.assertIn("timeout", item.erreurs)
        self.assertIn("about 12,400 inhabitants", item.reponse)
        self.assertNotIn("Timeout:", shown)

    async def test_a_streamed_answer_is_not_repeated(self) -> None:
        async def provider(request: ChatRequest, item: Interaction) -> None:
            model = writer_model(sink_context.get())  # type: ignore[arg-type]
            model.note_hold = "off"
            with patch("services.orchestrator.model.receive", new=slow_writer(ANSWER)):
                await model.complete(QUESTION, 100)

        frames, _ = await self.run_turn(provider)
        shown = text_of(frames)
        self.assertEqual(shown.count("12,400 inhabitants"), 1)
        self.assertIn("Its market opens on Saturdays", shown)
        self.assertIn("cut here by the time limit", shown)

    async def test_nothing_written_on_the_owned_server_says_so(self) -> None:
        async def provider(request: ChatRequest, item: Interaction) -> None:
            await asyncio.Event().wait()

        frames, item = await self.run_turn(
            provider, environ={"ATLAS_PRIVACY_MINIMUM": "strict"}
        )
        shown = text_of(frames)
        self.assertEqual(
            shown.strip(),
            "The private model could not answer within the time limit (1 s). "
            "Ask a shorter or more precise question, or send it again.",
        )
        self.assertEqual(item.reponse, shown.strip())

    async def test_a_loop_clock_stop_reaches_an_api_caller_as_an_answer(self) -> None:
        async def provider(request: ChatRequest, item: Interaction) -> None:
            draft = time_cut.current.get()
            assert draft is not None
            draft.begin()
            draft.add(ANSWER)
            item.state, item.erreurs = "stopped", ["wall_clock"]
            item.reponse = "Explicit stop: wall_clock. Tools executed: 2; limit 8."

        frames, item = await self.run_turn(provider, atlas_ui=False)
        shown = text_of(frames)
        self.assertIn("about 12,400 inhabitants", shown)
        self.assertIn("cut here by the time limit", shown)
        self.assertNotIn("Explicit stop", shown)
        last = frames[-1]
        self.assertEqual(last["choices"][0]["finish_reason"], "length")
        self.assertEqual(last["atlas"]["cut"]["code"], "timeout")

    async def test_a_working_note_is_never_shown_as_the_answer(self) -> None:
        async def provider(request: ChatRequest, item: Interaction) -> None:
            draft = time_cut.current.get()
            assert draft is not None
            draft.begin()
            draft.add("I will search the web for the latest figures.")
            await asyncio.Event().wait()

        frames, _ = await self.run_turn(provider)
        shown = text_of(frames)
        self.assertNotIn("I will search", shown)
        self.assertIn("took too long", shown)


class NonStreamCutTests(unittest.TestCase):
    def test_a_cut_api_turn_answers_with_the_text_and_the_sentence(self) -> None:
        async def provider(request: ChatRequest, item: Interaction) -> None:
            draft = time_cut.current.get()
            assert draft is not None
            draft.add(ANSWER)
            await asyncio.sleep(30)

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch.object(ChatRequest, "timeout_seconds", SHORT),
            patch("services.orchestrator.chat_api.process", side_effect=provider),
            TestClient(app) as client,
        ):
            reply = client.post(
                "/v1/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "Tell me about Valmonde"}]
                },
            )
        self.assertEqual(reply.status_code, 200)
        body = reply.json()
        content = body["choices"][0]["message"]["content"]
        self.assertTrue(content.startswith("The fictional town of Valmonde"))
        self.assertIn("cut here by the time limit", content)
        self.assertEqual(body["choices"][0]["finish_reason"], "length")
        self.assertEqual(body["atlas"]["cut"]["code"], "timeout")


class EndpointLimitTests(unittest.TestCase):
    def test_the_chat_limit_follows_the_endpoint(self) -> None:
        with patch.dict(os.environ, {"ATLAS_OWNED_CHAT_SECONDS": ""}):
            self.assertEqual(profiles.chat_seconds(False), 120.0)
            self.assertEqual(profiles.chat_seconds(True), 240.0)
        with patch.dict(os.environ, {"ATLAS_OWNED_CHAT_SECONDS": "180"}):
            self.assertEqual(profiles.chat_seconds(True), 180.0)
            self.assertEqual(profiles.chat_seconds(False), 120.0)
        for invalid in ("fast", "60", "301"):
            with (
                self.subTest(value=invalid),
                patch.dict(os.environ, {"ATLAS_OWNED_CHAT_SECONDS": invalid}),
                self.assertRaises(ValueError),
            ):
                profiles.chat_seconds(True)

    def test_a_strict_chat_turn_carries_the_owned_limit(self) -> None:
        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "What is the capital of Peru?"}]}
        )
        with patch.dict(
            os.environ, {"ATLAS_OWNED_CHAT_SECONDS": "", "ATLAS_PRIVACY_MINIMUM": ""}
        ):
            self.assertEqual(request.timeout_seconds, 120.0)
        with patch.dict(
            os.environ,
            {"ATLAS_OWNED_CHAT_SECONDS": "", "ATLAS_PRIVACY_MINIMUM": "strict"},
        ):
            self.assertEqual(request.timeout_seconds, 240.0)


if __name__ == "__main__":
    unittest.main()
