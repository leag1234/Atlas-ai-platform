"""Attachment names with spaces or accents must survive the terminal upload integrity check."""

import asyncio
import base64
import unittest
from unittest.mock import patch

from services.orchestrator.documents import Attachment
from services.orchestrator.terminal_client import TerminalClient, safe_filename


class SafeFilenameTests(unittest.TestCase):
    def test_spaces_accents_and_punctuation_become_ascii(self) -> None:
        self.assertEqual(safe_filename("Accord été (v2).pdf"), "Accord_ete_v2.pdf")
        self.assertEqual(safe_filename("Two Words.PDF"), "Two_Words.PDF")
        self.assertEqual(safe_filename("plain-name_1.docx"), "plain-name_1.docx")

    def test_paths_and_empty_stems_are_contained(self) -> None:
        self.assertEqual(safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(safe_filename("C:\\\\dir\\\\file name.xlsx"), "file_name.xlsx")
        self.assertEqual(safe_filename("   .pdf"), "attachment.pdf")
        self.assertEqual(safe_filename("…"), "attachment")


class UploadTests(unittest.TestCase):
    def test_upload_sends_and_checks_the_safe_name(self) -> None:
        raw = b"%PDF-1.4 sample"
        attachment = Attachment.model_validate(
            {
                "filename": "Rapport annuel été.pdf",
                "file_data": base64.b64encode(raw).decode(),
            }
        )
        sent: dict[str, object] = {}

        async def fake_request(self, method, path, timeout, **kwargs):  # type: ignore[no-untyped-def]
            fields = kwargs["data"]._fields
            sent["filename"] = fields[0][0].get("filename")
            return {"path": self.root + "/" + str(sent["filename"]), "size": len(raw)}

        with (
            patch.dict("os.environ", {"ATLAS_TERMINAL_ENABLED": "1"}),
            patch.object(TerminalClient, "request", fake_request),
        ):
            client = TerminalClient("req-1")
            path = asyncio.run(client.upload(attachment, 5))
        self.assertEqual(sent["filename"], "Rapport_annuel_ete.pdf")
        self.assertEqual(path, "/home/user/requests/req-1/Rapport_annuel_ete.pdf")


if __name__ == "__main__":
    unittest.main()
