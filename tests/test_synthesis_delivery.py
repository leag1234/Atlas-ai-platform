"""Task F: attachment input names must not trigger file-output recovery."""

import base64
from decimal import Decimal
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from test_m23_documents import pdf
from services.orchestrator.chat_pipeline import ChatTools, process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.image_store import prepare_uploads
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Result
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.terminal_client import TerminalClient


def synthesis_request(question: str) -> ChatRequest:
    normalized, _, _ = prepare_uploads(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        *(
                            {
                                "type": "file",
                                "file": {
                                    "filename": name,
                                    "file_data": base64.b64encode(
                                        pdf(1) if name.endswith(".pdf") else b"test"
                                    ).decode(),
                                },
                            }
                            for name in ("report.pdf", "notes.txt")
                        ),
                    ],
                }
            ]
        }
    )
    return ChatRequest.model_validate(normalized)


class SynthesisDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_synthesis_never_recovers_or_renders_files(self) -> None:
        answer = "The opening defines the goal, the middle measures it, and the end gives the result."
        for commands in (0, 2):
            with self.subTest(terminal_commands=commands):
                model = GatewayModel(
                    "http://127.0.0.1",
                    Configuration(
                        input_eur_per_mtok=Decimal("1"),
                        output_eur_per_mtok=Decimal("1"),
                        max_tokens=2048,
                    ),
                )

                async def runner(*args: object, **kwargs: object) -> Result:
                    runtime = args[2]
                    assert isinstance(runtime, ChatTools)
                    assert runtime.terminal is not None
                    runtime.terminal.commands = commands
                    return Result(
                        text=answer,
                        state="done",
                        tool_calls=commands,
                        cost=Decimal("0.02"),
                    )

                query = synthesis_request("Write a summary of these two attachments.")
                with (
                    tempfile.TemporaryDirectory() as root,
                    patch.dict(
                        os.environ,
                        {
                            "ATLAS_TERMINAL_ENABLED": "1",
                            "ATLAS_WEB_CACHE": root + "/cache",
                        },
                    ),
                    patch.object(
                        GatewayModel, "connect", AsyncMock(return_value=model)
                    ),
                    patch.object(TerminalClient, "initialize", AsyncMock()),
                    patch.object(
                        TerminalClient, "upload", AsyncMock(return_value="input")
                    ),
                    patch.object(ChatTools, "execute", AsyncMock()) as execute,
                    patch(
                        "services.orchestrator.chat_pipeline.run", side_effect=runner
                    ) as run,
                    patch(
                        "services.orchestrator.file_content.render_command",
                        return_value=("", []),
                    ) as render,
                    patch(
                        "services.orchestrator.file_results.inline_fallback",
                        return_value=answer,
                    ) as fallback,
                ):
                    item = Interaction()
                    await process(query, item)
                self.assertEqual(run.call_count, 1)
                render.assert_not_called()
                fallback.assert_not_called()
                execute.assert_not_awaited()
                self.assertEqual(item.reponse, answer)
                self.assertEqual(item.files, [])
                self.assertEqual(item.terminal_commands, commands)
                self.assertEqual(Decimal(str(item.cout_eur)), Decimal("0.02"))
                self.assertTrue(model.has_attachments)
                self.assertEqual(query.timeout_seconds, 300)
                self.assertEqual(item.state, "done")
                self.assertNotIn("file_not_produced", item.erreurs)


class DeliveryIntentTests(unittest.TestCase):
    def test_synthesis_phrasings_ignore_both_attachment_representations(self) -> None:
        from packages.file_intent import requests_file_delivery

        for question in (
            "Fais une synthèse d’une page de ces deux pièces jointes, couvrant le début, le milieu et la fin.",
            "Write a summary of the attachments",
            "PREPARE A SUMMARY",
            "prepare une synthese",
            "Schreib eine Zusammenfassung",
            "Haz un resumen",
        ):
            for text in (
                synthesis_request(question).messages[-1].text,
                '<attached_files>\n<file type="filesystem" url="/home/user/report.pdf"/>'
                '<file type="filesystem" url="/home/user/notes.txt"/>\n</attached_files>\n'
                + question,
            ):
                with self.subTest(question=question, text=text):
                    self.assertFalse(requests_file_delivery(text))

    def test_explicit_downloads_survive_attachment_metadata_removal(self) -> None:
        from packages.file_intent import requests_file_delivery

        for question in (
            "Crée une présentation PPTX et PDF sur /e/OS et Murena.",
            "Crée un DOCX contenant trois conseils pour organiser une réunion.",
            "Create a downloadable presentation from these attachments",
            "Write the summary as a PDF",
            "Export these results to a file",
        ):
            with self.subTest(question=question):
                self.assertTrue(
                    requests_file_delivery(
                        synthesis_request(question).messages[-1].text
                    )
                )


class SynthesisStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthesis_deltas_remain_visible_during_generation(self) -> None:
        from services.orchestrator.chat_stream import response
        from services.orchestrator.stream_client import sink_context

        query = synthesis_request("Write a summary of these two attachments.")
        answer = (
            "The report measures progress from the opening goals to the final outcome."
        )
        release = asyncio.Event()

        async def draft(request: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            assert sink is not None
            await sink({"delta": {"content": answer}})
            await release.wait()
            item.reponse = answer
            item.state = "done"

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
        ):
            stream = response(query, Interaction(), time.monotonic(), draft)
            iterator = stream.body_iterator
            try:
                async with asyncio.timeout(2):
                    async for event in iterator:
                        if str(event).startswith("data: {"):
                            data = json.loads(str(event)[6:])
                            if (
                                data.get("choices", [{}])[0]
                                .get("delta", {})
                                .get("content")
                            ):
                                self.assertIn(answer, str(event))
                                break
            finally:
                release.set()
                async for _ in iterator:
                    pass
