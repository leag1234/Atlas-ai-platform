"""Option A attachment feedback: localized notes and visible document activity."""

import base64
from decimal import Decimal
import json
import os
import tempfile
import time
from typing import cast
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator.attachment_reading import failure_notice
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.documents import Attachment
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Reservation, Turn
from services.orchestrator.model import Configuration, GatewayModel


def scanned_error(page: int, total: int = 48) -> str:
    return f"PDF page {page}/{total} has no text layer; OCR is required (not available)"


class ReadingNoteTests(unittest.TestCase):
    def test_localized_notes_group_one_two_and_ranges(self) -> None:
        cases = [
            ("fr", [1], "page 1"),
            ("en", [1, 2], "Pages 1–2"),
            ("de", [1, 2, 5, 6, 7], "1–2 und 5–7"),
            ("es", [3, 5], "3 y 5"),
            ("it", [3], "pagina 3"),
        ]
        for language, pages, expected in cases:
            with self.subTest(language=language, pages=pages):
                note = failure_notice(
                    [
                        {
                            "filename": "Rapport annuel été (v2).pdf",
                            "errors": [scanned_error(page) for page in pages],
                        }
                    ],
                    language,
                )
                self.assertIn(expected, note)
                self.assertNotIn("OCR is required", note)
                self.assertNotIn("text layer", note)

    def test_non_page_diagnostic_is_never_exposed(self) -> None:
        note = failure_notice(
            [
                {
                    "filename": "broken.pdf",
                    "errors": ["PdfReadError: private parser detail"],
                }
            ],
            "fr",
        )
        # D5: the cause is named in plain words with its short code, never the parser detail.
        self.assertEqual(
            note,
            "Le document broken.pdf n'a pas pu être lu : "
            "le fichier PDF est endommagé ou mal formé (PdfReadError).",
        )
        self.assertNotIn("private parser detail", note)


class DocumentActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_note_follows_answer_and_status_reaches_read_then_synthesis(
        self,
    ) -> None:
        request = ChatRequest(
            stream=True,
            ui_locale="fr",
            messages=[ChatMessage(role="user", content="Résume ce document.")],
            documents=[
                Attachment(
                    filename="Rapport annuel été (v2).pdf",
                    file_data=base64.b64encode(self._partial_pdf()).decode(),
                )
            ],
        )
        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )

        async def complete(*_args: object, **_kwargs: object) -> Turn:
            from services.orchestrator.stream_client import sink_context

            sink = sink_context.get()
            assert sink is not None
            await sink({"delta": {"content": "Réponse utile."}})
            return Turn("Réponse utile.", usage=Reservation(1, Decimal(0)))

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(model, "complete", AsyncMock(side_effect=complete)),
        ):
            rows = [
                json.loads(str(row)[6:])
                async for row in response(
                    request,
                    Interaction(),
                    time.monotonic() - 5,
                    __import__(
                        "services.orchestrator.chat_pipeline", fromlist=["process"]
                    ).process,
                ).body_iterator
                if str(row).startswith("data: {")
            ]
        statuses = [row["event"]["data"] for row in rows if "event" in row]
        descriptions = [str(status["description"]) for status in statuses]
        self.assertTrue(
            any("Document lu : 1 pages" in description for description in descriptions)
        )
        self.assertTrue(
            any("Synthèse en cours" in description for description in descriptions)
        )
        self.assertTrue(all("/ 300s" in description for description in descriptions))
        text = "".join(
            row.get("choices", [{}])[0].get("delta", {}).get("content", "")
            for row in rows
        )
        self.assertLess(text.index("Réponse utile."), text.index("La page 1"))
        self.assertNotIn("OCR is required", text)

    async def test_whole_document_failure_is_localized_answer_and_finishes_status(
        self,
    ) -> None:
        request = ChatRequest(
            stream=True,
            ui_locale="en",
            messages=[ChatMessage(role="user", content="Summarize this.")],
            documents=[
                Attachment(
                    filename="scan.pdf",
                    file_data=base64.b64encode(self._blank_pdf()).decode(),
                )
            ],
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {"ATLAS_INTERACTION_DIR": root, "ATLAS_TERMINAL_ENABLED": "0"},
            ),
        ):
            rows = [
                json.loads(str(row)[6:])
                async for row in response(
                    request,
                    Interaction(),
                    time.monotonic(),
                    __import__(
                        "services.orchestrator.chat_pipeline", fromlist=["process"]
                    ).process,
                ).body_iterator
                if str(row).startswith("data: {")
            ]
        text = "".join(
            row.get("choices", [{}])[0].get("delta", {}).get("content", "")
            for row in rows
        )
        self.assertIn("scanned image", text)
        self.assertNotIn("OCR is required", text)
        self.assertTrue(rows[-1]["event"]["data"]["done"])

    @staticmethod
    def _blank_pdf() -> bytes:
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        buffer = __import__("io").BytesIO()
        writer.write(buffer)
        return cast(bytes, buffer.getvalue())

    def _partial_pdf(self) -> bytes:
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        page = writer.add_blank_page(width=100, height=100)
        font = writer._add_object(
            DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 10 Tf 10 10 Td (Readable evidence.) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content)
        buffer = __import__("io").BytesIO()
        writer.write(buffer)
        return cast(bytes, buffer.getvalue())


class PasswordNoteTests(unittest.TestCase):
    def test_password_protected_pdf_names_the_cause(self) -> None:
        note = failure_notice(
            [
                {
                    "filename": "protected.pdf",
                    "errors": [
                        "Password-protected PDF: provide an unencrypted document"
                    ],
                }
            ],
            "fr",
        )
        self.assertIn("mot de passe", note)
        self.assertIn("protected.pdf", note)


class EditIntentTests(unittest.TestCase):
    def test_editing_an_attached_file_is_file_delivery(self) -> None:
        from packages.file_intent import edits_attachment

        self.assertTrue(
            edits_attachment("Prends ce fichier word et corrige les fautes.")
        )
        self.assertTrue(edits_attachment("Nettoie la structure de ce document."))
        self.assertTrue(edits_attachment("Please fix the typos in this file."))
        self.assertTrue(
            edits_attachment("Dans cette feuille de calcul, crée un nouvel onglet.")
        )
        self.assertFalse(edits_attachment("Résume ce document en cinq points."))
        self.assertFalse(edits_attachment("What does page 12 say?"))


class OpenDocumentTests(unittest.TestCase):
    def test_odt_ods_odp_are_read_in_process(self) -> None:
        import base64
        import io
        import zipfile

        from services.orchestrator.documents import Attachment, extract_document

        def odf(body: str) -> bytes:
            content = (
                '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
                ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
                ' xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
                ' xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0">'
                "<office:body>" + body + "</office:body></office:document-content>"
            )
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("content.xml", content)
            return buffer.getvalue()

        cases = {
            "lettre été.odt": ("<text:p>Article aubergine 37</text:p>", "aubergine 37"),
            "inventaire.ods": (
                '<table:table table:name="Stock"><table:table-row><table:table-cell><text:p>abricot</text:p>'
                "</table:table-cell><table:table-cell><text:p>43</text:p></table:table-cell></table:table-row></table:table>",
                "abricot | 43",
            ),
            "diapos.odp": (
                "<draw:page><text:p>Cerise 59</text:p></draw:page>",
                "[Slide 1]",
            ),
        }
        for name, (body, expected) in cases.items():
            raw = odf(body)
            document = extract_document(
                Attachment(filename=name, file_data=base64.b64encode(raw).decode())
            )
            self.assertIn(expected, document.text, name)
