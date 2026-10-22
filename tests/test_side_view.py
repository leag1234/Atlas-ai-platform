"""Owned attachment opener and chunk-boundary transport regressions (no paid I/O)."""

import asyncio
import base64
import json
import time
import unittest
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
            "stream": True,
        }
    )
    return ChatRequest.model_validate(value)


class SideViewTests(unittest.TestCase):
    def test_upload_id_and_escaped_title(self) -> None:
        payload = request('report "quoted".pdf')
        self.assertEqual(payload.documents[0].file_id, FILE_ID)
        opener = pdf_placeholders(payload)
        self.assertIn(f'data-src="/api/v1/files/{FILE_ID}/content"', opener)
        self.assertIn('data-title="report &quot;quoted&quot;.pdf"', opener)
        self.assertIn('data-slot="top" hidden></span>', opener)
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
