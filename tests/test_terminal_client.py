"""Terminal responses are untrusted; credentials never enter model history."""

import os
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator.terminal_client import safe_result, output_path, Publish
from services.orchestrator.file_results import render_files
from services.orchestrator.terminal_client import TerminalClient


class CompleteOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_command_output_and_honest_truncation(self) -> None:
        with patch.dict(os.environ, {"ATLAS_TERMINAL_ENABLED": "1"}):
            client = TerminalClient("output-test")
        for truncated in (False, True):
            with patch.object(
                client,
                "request",
                AsyncMock(
                    return_value={
                        "status": "done",
                        "exit_code": 0,
                        "output": [{"type": "output", "data": "full document"}],
                        "truncated": truncated,
                    }
                ),
            ) as transport:
                result = await client.execute("cat document.txt", 15)
                self.assertNotIn("tail", transport.call_args.kwargs["params"])
                if truncated:
                    self.assertEqual(result["error"], "terminal_output_truncated")
                    self.assertNotIn("output", result)
                else:
                    self.assertIn("full document", str(result["output"]))


class TerminalClientTests(unittest.TestCase):
    def test_publication_requires_strategy_and_renders_verified_link(self) -> None:
        with self.assertRaises(ValueError):
            Publish.model_validate({"path": "/home/user/requests/a/report.docx"})
        url = (
            "/api/v1/terminals/atlas-files/files/serve/home/user/requests/a/report.docx"
        )
        answer = render_files(
            "[Report](https://invented.invalid/report.docx)",
            [url],
            ["regenerated"],
            "fr",
        )
        self.assertNotIn("invented.invalid", answer)
        self.assertIn("Document régénéré", answer)
        self.assertIn("mise en forme", answer)
        self.assertIn(url, answer)

    def test_removes_terminal_key_before_model_history(self) -> None:
        with patch.dict(os.environ, {"OPEN_TERMINAL_API_KEY": "private-test-key"}):
            result = safe_result({"output": [{"data": "key=private-test-key"}]})
        self.assertNotIn("private-test-key", str(result))
        self.assertIn("[REDACTED]", str(result))

    def test_publication_requires_request_directory(self) -> None:
        root = "/home/user/requests/abc"
        self.assertEqual(
            output_path(root, root + "/slides.pptx"), root + "/slides.pptx"
        )
        for path in (
            "/etc/hostname",
            root + "/../another/a.pdf",
            root + "/.hidden",
            root + "/script.py",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                output_path(root, path)
