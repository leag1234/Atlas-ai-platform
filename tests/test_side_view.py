"""Owned attachment opener and chunk-boundary transport regressions (no paid I/O)."""

import asyncio
import base64
import json
import time
import unittest
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.image_store import prepare_uploads
from services.orchestrator.interactions import Interaction
from services.orchestrator.narration import NarrationFilter, ProtocolFilter
from services.orchestrator.side_view import pdf_placeholders
from services.orchestrator.stream_client import sink_context

FILE_ID = "12345678-1234-1234-1234-123456789abc"


def request(filename: str = "notes.pdf", file_id: str | None = FILE_ID) -> ChatRequest:
    file = {"filename": filename, "file_data": base64.b64encode(b"fixture").decode()}
    if file_id:
        file["file_id"] = file_id
    value, _, _ = prepare_uploads(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Summarize this"},
                        {"type": "file", "file": file},
                    ],
                }
            ],
            "atlas_ui": True,
            "stream": True,
        }
    )
    return ChatRequest.model_validate(value)


Process = Callable[[ChatRequest, Interaction], Awaitable[None]]


def frames(process: Process, payload: ChatRequest) -> list[dict[str, Any]]:
    """Every data frame of one streamed turn, in order."""

    async def collect() -> list[str]:
        stream = response(payload, Interaction(), time.monotonic(), process)
        chunks = []
        with patch("services.orchestrator.chat_stream.write_interaction"):
            async for chunk in stream.body_iterator:
                chunks.append(
                    chunk if isinstance(chunk, str) else bytes(chunk).decode()
                )
        return chunks

    return [
        json.loads(line[6:])
        for line in "".join(asyncio.run(collect())).splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def text_of(frame: dict[str, Any]) -> str:
    return str(frame.get("choices", [{}])[0].get("delta", {}).get("content", ""))


def status_of(frame: dict[str, Any]) -> dict[str, Any] | None:
    event = frame.get("event")
    return event["data"] if isinstance(event, dict) else None


def keepalives(sent: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        frame
        for frame in sent
        if isinstance(frame.get("atlas"), dict)
        and frame["atlas"].get("phase") == "answer_keepalive"
    ]


class StatusUntilTheAnswerTests(unittest.TestCase):
    """The interface shows a turn as interrupted after 45 s without an event."""

    def test_the_opener_does_not_end_the_status_line(self) -> None:
        # Staging 2026-11-12: the link to the attached PDF counted as the start of
        # the answer, the per-second status stopped, the document took more than
        # 45 s to read and the interface stopped the turn as interrupted.
        answer = "The initial budget is 730 euros."

        async def process(payload: ChatRequest, item: Interaction) -> None:
            await asyncio.sleep(2.4)
            item.reponse, item.state = answer, "done"

        sent = frames(process, request())
        opener = next(i for i, frame in enumerate(sent) if text_of(frame))
        self.assertEqual(text_of(sent[opener]), pdf_placeholders(request()))
        answered = next(i for i, frame in enumerate(sent) if answer in text_of(frame))
        opener_status = status_of(sent[opener])
        self.assertTrue(opener_status is None or opener_status["done"] is False)
        waiting = [
            status
            for frame in sent[opener + 1 : answered]
            if (status := status_of(frame)) is not None
        ]
        # One status a second while the document is read, none of them "done".
        self.assertGreaterEqual(len(waiting), 2)
        self.assertTrue(all(status["done"] is False for status in waiting))
        self.assertTrue(all(status["hidden"] is False for status in waiting))
        done = status_of(sent[answered])
        assert done is not None
        self.assertTrue(done["done"])
        self.assertTrue(done["hidden"])

    def test_a_started_answer_that_goes_silent_still_sends_events(self) -> None:
        async def process(payload: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            assert sink
            await sink({"delta": {"content": "First part.\n"}})
            await asyncio.sleep(2.6)
            await sink({"delta": {"content": "Second part.\n"}})
            item.reponse, item.state = "First part.\nSecond part.\n", "done"

        plain = ChatRequest.model_validate(
            {
                "messages": [{"role": "user", "content": "Tell me a story"}],
                "atlas_ui": True,
                "stream": True,
            }
        )
        with patch("services.orchestrator.chat_stream.ANSWER_SILENCE_SECONDS", 1.0):
            sent = frames(process, plain)
        first = next(i for i, frame in enumerate(sent) if "First" in text_of(frame))
        second = next(i for i, frame in enumerate(sent) if "Second" in text_of(frame))
        between = sent[first + 1 : second]
        self.assertTrue(keepalives(between))
        for frame in between:
            # Nothing visible: no text, and the status stays the hidden one.
            self.assertEqual(text_of(frame), "")
            status = status_of(frame)
            if status is not None:
                self.assertTrue(status["done"])
                self.assertTrue(status["hidden"])
        # A short pause sends nothing more than before.
        self.assertFalse(keepalives(frames(process, plain)))


class SideViewTests(unittest.TestCase):
    def test_upload_id_and_escaped_title(self) -> None:
        payload = request('report "quoted".pdf')
        self.assertEqual(payload.documents[0].file_id, FILE_ID)
        opener = pdf_placeholders(payload)
        self.assertIn(f"(/api/v1/files/{FILE_ID}/content#atlas-view=pdf", opener)
        self.assertIn("title=report+%22quoted%22.pdf", opener)
        self.assertIn("slot=top)", opener)
        # No raw HTML marker: it showed as markup in sidebar previews.
        self.assertNotIn("<span", opener)
        self.assertEqual(pdf_placeholders(request(file_id=None)), "")
        self.assertEqual(pdf_placeholders(request("notes.md")), "")

    def test_id_cannot_escape_content_path(self) -> None:
        for invalid in [
            "../../admin",
            'id" onclick="alert(1)',
            "https://example.org/file",
        ]:
            with self.assertRaises(ValueError):
                request(file_id=invalid)

    def test_filters_preserve_every_chunk_boundary(self) -> None:
        text = pdf_placeholders(request()) + "The initial budget is 730 euros."
        for split in range(len(text) + 1):
            narration, protocol = NarrationFilter(), ProtocolFilter()
            output = "".join(
                protocol.feed(narration.feed(chunk))
                for chunk in [text[:split], text[split:]]
            )
            output += protocol.feed(narration.finish()) + protocol.finish()
            self.assertEqual(output, text)

    def test_real_stream_emits_before_generation(self) -> None:
        async def check() -> str:
            async def process(payload: ChatRequest, item: Interaction) -> None:
                sink = sink_context.get()
                assert sink
                await sink({"delta": {"content": "The initial budget is 730 euros."}})
                item.reponse, item.state = "The initial budget is 730 euros.", "done"

            stream = response(request(), Interaction(), time.monotonic(), process)
            chunks = []
            with patch("services.orchestrator.chat_stream.write_interaction"):
                async for chunk in stream.body_iterator:
                    chunks.append(
                        chunk if isinstance(chunk, str) else bytes(chunk).decode()
                    )
            return "".join(chunks)

        stream = asyncio.run(check())
        content = "".join(
            json.loads(line[6:])
            .get("choices", [{}])[0]
            .get("delta", {})
            .get("content", "")
            for line in stream.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        )
        self.assertEqual(
            content, pdf_placeholders(request()) + "The initial budget is 730 euros."
        )

    def test_answer_without_streamed_text_follows_the_opener(self) -> None:
        # Document answers are completed in one call and never streamed: the opener
        # is Atlas's own text and must not stand in for the answer (2026-11-03 tour).
        async def check() -> str:
            async def process(payload: ChatRequest, item: Interaction) -> None:
                item.reponse, item.state = "The initial budget is 730 euros.", "done"

            stream = response(request(), Interaction(), time.monotonic(), process)
            chunks = []
            with patch("services.orchestrator.chat_stream.write_interaction"):
                async for chunk in stream.body_iterator:
                    chunks.append(
                        chunk if isinstance(chunk, str) else bytes(chunk).decode()
                    )
            return "".join(chunks)

        stream = asyncio.run(check())
        content = "".join(
            json.loads(line[6:])
            .get("choices", [{}])[0]
            .get("delta", {})
            .get("content", "")
            for line in stream.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        )
        self.assertEqual(
            content, pdf_placeholders(request()) + "The initial budget is 730 euros."
        )
