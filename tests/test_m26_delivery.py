"""M26 attachment discovery, inline content and contained publication."""

import unittest
from services.orchestrator.chat_schema import ChatRequest


class InlineCodeTests(unittest.TestCase):
    def test_program_requests_do_not_require_a_file(self) -> None:
        from packages.file_intent import produces_file

        for text in (
            "Écris une fonction de tri",
            "Un script Python",
            "CODE SQL",
            "ecris une fonction",
            "Write a function to sort numbers",
            "Schreibe eine Funktion",
        ):
            with self.subTest(text=text):
                self.assertFalse(produces_file(text))
        self.assertTrue(
            produces_file("Create a downloadable file containing the Python script")
        )


class PublicationTests(unittest.TestCase):
    def test_relative_publication_stays_in_request_directory(self) -> None:
        from services.orchestrator.terminal_client import output_path

        root = "/home/user/requests/abc"
        self.assertEqual(output_path(root, "notes.docx"), root + "/notes.docx")
        for path in (
            "../notes.docx",
            "/home/user/notes.docx",
            ".hidden.docx",
            "../other/notes.docx",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                output_path(root, path)


class PublishedFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_presentation_inline_answer_fails_honestly_without_terminal_code(
        self,
    ) -> None:
        """J54: an inline answer cannot silently satisfy a requested download."""
        from decimal import Decimal
        import os
        import tempfile
        from unittest.mock import AsyncMock, patch

        from services.orchestrator.chat_pipeline import process
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Result
        from services.orchestrator.model import Configuration, GatewayModel
        from services.orchestrator.terminal_client import TerminalClient

        model = GatewayModel(
            "http://127.0.0.1",
            Configuration(
                input_eur_per_mtok=Decimal("1"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )
        request = ChatRequest.model_validate(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Create a downloadable presentation about recycling",
                    }
                ]
            }
        )
        inline = "Here is a complete presentation outline with enough detail to use."
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_TERMINAL_ENABLED": "1",
                    "ATLAS_WEB_CACHE": root + "/cache.sqlite",
                },
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(TerminalClient, "initialize", AsyncMock()),
            patch(
                "services.orchestrator.chat_pipeline.run",
                AsyncMock(
                    side_effect=[
                        Result(text=inline, state="done"),
                        Result(
                            text="I could not create the presentation file.",
                            state="done",
                        ),
                    ]
                ),
            ) as run,
        ):
            item = Interaction()
            await process(request, item)

        self.assertEqual(run.await_count, 1)
        self.assertIn(inline, item.reponse)
        self.assertIn("No file was produced", item.reponse)
        self.assertEqual(item.files, [])

    async def test_published_files_survive_failed_closing_inference(self) -> None:
        from decimal import Decimal
        import os
        import tempfile
        from unittest.mock import AsyncMock, patch
        from services.orchestrator.chat_pipeline import process
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Result
        from services.orchestrator.model import (
            Configuration,
            GatewayError,
            GatewayModel,
        )
        from services.orchestrator.terminal_client import TerminalClient

        model = GatewayModel(
            "http://127.0.0.1",
            Configuration(
                input_eur_per_mtok=Decimal("1"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )
        url = "/api/v1/terminals/atlas-files/files/serve/home/user/requests/test/report.docx"

        async def published(client: TerminalClient) -> None:
            client.files = [url]
            client.strategies = ["created"]

        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "Create a DOCX document"}]}
        )
        for outcome in (
            Result(
                state="stopped",
                reason="provider_error",
                text="Closing inference failed",
            ),
            GatewayError(
                "cost_budget", 429, "measured 120 microEUR; limit 100 microEUR"
            ),
        ):
            with (
                tempfile.TemporaryDirectory() as root,
                patch.dict(
                    os.environ,
                    {
                        "ATLAS_TERMINAL_ENABLED": "1",
                        "ATLAS_WEB_CACHE": root + "/cache.sqlite",
                    },
                ),
                patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
                patch.object(TerminalClient, "initialize", published),
                patch(
                    "services.orchestrator.chat_pipeline.run",
                    AsyncMock(side_effect=[outcome]),
                ),
            ):
                item = Interaction()
                await process(request, item)
                self.assertEqual(item.state, "done")
                self.assertIn(url, item.reponse)
                self.assertTrue(item.erreurs)
                self.assertEqual(item.files, [url])


class DownloadIntentTests(unittest.TestCase):
    def test_explicit_downloads_are_file_requests_without_creation_verbs(self) -> None:
        from packages.file_intent import produces_file

        for text in (
            "CSV téléchargeable",
            "CSV telechargeable",
            "CSV DOWNLOAD",
            "A CSV to download",
            "CSV zum Herunterladen",
            "CSV descargable",
        ):
            self.assertTrue(produces_file(text), text)
        self.assertFalse(produces_file("Explique le format CSV"))
