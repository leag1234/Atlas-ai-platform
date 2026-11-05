"""Text project deliverables (MD, CSV, TXT, HTML): validation, tools and headers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import AsyncMock, patch

from packages.text_deliverables import (
    TEXT_MIMES,
    TextDeliverableError,
    validate_text,
    visible_text,
)
from services.retrieval.project_identity import ProjectError


class TextValidationTests(unittest.TestCase):
    def test_valid_text_formats(self) -> None:
        for suffix, raw in (
            (".md", "# Résumé\n\n- point\n".encode()),
            (".csv", b'name,note\r\n"Doe, Jane","said ""yes"""\r\n\r\n'),
            (".txt", "Plain text été\n".encode()),
            (".html", b"<!doctype html><p>Body</p><script>x()</script>"),
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual(validate_text(raw, suffix), raw.decode())

    def test_named_failures(self) -> None:
        for suffix, raw, code in (
            (".csv", b"a,b\n1,2,3\n", "csv_inconsistent_columns"),
            (".csv", b'a,b\n"1,2\n', "csv_parse_error"),
            (".csv", b"\n\n", "text_empty"),
            (".md", "café".encode("latin-1"), "text_not_utf8"),
            (".html", b"\xff\xfe<p>x</p>", "text_not_utf8"),
            (".txt", b" \n\t\n", "text_empty"),
            (".txt", b"", "text_empty"),
            (".json", b"{}", "unsupported_text_format"),
        ):
            with self.subTest(suffix=suffix, code=code):
                with self.assertRaises(TextDeliverableError) as caught:
                    validate_text(raw, suffix)
                self.assertEqual(caught.exception.code, code)

    def test_visible_text_for_source_coverage(self) -> None:
        self.assertEqual(
            visible_text("<p>Fish &amp; chips</p>", ".html"), "Fish & chips"
        )
        self.assertEqual(visible_text('a,"b ""c"""\n', ".csv"), 'a\nb "c"')
        self.assertEqual(visible_text("# T", ".md"), "# T")


class RetrievalReopenTests(unittest.TestCase):
    def test_reopen_returns_text_bytes_and_names_failures(self) -> None:
        from services.retrieval.project_files import PUBLICATION_MIMES, reopen_artifact

        self.assertEqual(
            {k: PUBLICATION_MIMES[k] for k in TEXT_MIMES},
            {
                ".md": "text/markdown",
                ".csv": "text/csv",
                ".txt": "text/plain",
                ".html": "text/html",
            },
        )
        self.assertEqual(reopen_artifact(b"a,b\n1,2\n", ".csv"), b"a,b\n1,2\n")
        for raw, suffix, code in (
            (b"a,b\n1\n", ".csv", "csv_inconsistent_columns"),
            (b"\xc3(", ".txt", "text_not_utf8"),
            (b"   ", ".md", "text_empty"),
        ):
            with self.assertRaises(ProjectError) as caught:
                reopen_artifact(raw, suffix)
            self.assertEqual(caught.exception.status, 422)
            self.assertIn(code, str(caught.exception.body["message"]))


class WorkToolTests(unittest.TestCase):
    def test_text_document_preview_sources_and_failures(self) -> None:
        from services.orchestrator.project_document_tools import text_document

        document = text_document("# Notes\n\nOwn text\n", ".md", ["First  evidence"])
        self.assertEqual(document.content, document.preview)
        self.assertEqual(hashlib.sha256(document.content).hexdigest(), document.sha256)
        self.assertEqual(
            document.content, b"# Notes\n\nOwn text\n\n## Sources\n\nFirst evidence\n"
        )
        plain = text_document("Own text", ".txt", ["First evidence"])
        self.assertEqual(plain.content, b"Own text\n\nSources\n\nFirst evidence\n")
        covered = text_document("item\nFirst evidence\n", ".csv", ["First evidence"])
        self.assertEqual(covered.content, b"item\nFirst evidence\n")
        html = text_document(
            "<p>First &amp; evidence</p>", ".html", ["First & evidence"]
        )
        self.assertEqual(html.content, b"<p>First &amp; evidence</p>")
        for text, suffix, sources, code in (
            ("item\nOther\n", ".csv", ["First evidence"], "Source coverage"),
            ("<p>Other</p>", ".html", ["First evidence"], "Source coverage"),
            ("a,b\n1\n", ".csv", [], "csv_inconsistent_columns"),
            ("   ", ".txt", [], "text_empty"),
            ("bad \ud800", ".md", [], "text_not_utf8"),
            ("x" * 1048577, ".txt", [], "text_size_limit"),
        ):
            with self.subTest(suffix=suffix, code=code):
                with self.assertRaisesRegex(ValueError, code):
                    text_document(text, suffix, sources)

    def test_execute_routes_text_paths_without_office_rendering(self) -> None:
        from services.orchestrator import project_document_tools as tools_module

        captured: list[dict[str, Any]] = []

        async def publish(tools: Any, args: dict[str, Any], timeout: float) -> Any:
            captured.append(args)
            return dict(ok=True)

        tools = AsyncMock()
        with patch.object(tools_module, "publish", side_effect=publish):
            for path in ("notes.md", "table.CSV", "log.txt", "page.html"):
                reply = asyncio.run(
                    tools_module.execute(
                        tools,
                        "publish_document",
                        json.dumps(
                            dict(
                                path=path, title="Ignored", content="a,b\n", sources=[]
                            )
                        ),
                        30,
                    )
                )
                self.assertEqual(reply, dict(ok=True))
            self.assertEqual([a["content"] for a in captured], ["a,b\n"] * 4)
            for bad in (
                dict(path="notes.md", content="x", sources=[], slides=[]),
                dict(path="notes.md", content="x", sources=[], formulas=True),
                dict(path="notes.md", content="", sources=[]),
                dict(path="notes.md", content=dict(title="T", sections=[]), sources=[]),
            ):
                with self.assertRaises(ValueError):
                    asyncio.run(
                        tools_module.execute(
                            tools, "publish_document", json.dumps(bad), 30
                        )
                    )
        self.assertEqual(len(captured), 4)

    def test_declaration_names_text_formats_and_office_stays_unchanged(self) -> None:
        from services.orchestrator.project_document_tools import (
            DESCRIPTIONS,
            ModelPublish,
            text_output,
        )

        description = DESCRIPTIONS["publish_document"]
        self.assertIn("DOCX, XLSX, PPTX, PDF, MD, CSV, TXT or HTML", description)
        self.assertIn("For PPTX send title and slides", description)
        self.assertEqual(ModelPublish.model_json_schema()["title"], "ModelPublish")
        self.assertTrue(text_output(dict(path="a/b.HTML")))
        for path in ("a.docx", "a.xlsx", "a.pptx", "a.pdf", "a.htm", "a"):
            self.assertFalse(text_output(dict(path=path)))

    def test_markdown_and_text_requests_select_documents_group(self) -> None:
        from services.orchestrator.work_tool_selection import GROUPS, select_group

        for question in (
            "Write the summary as a Markdown file",
            "Save it as notes.md",
            "Give me a .txt export",
        ):
            group = select_group(question, [])
            self.assertEqual(group, "documents", question)
            self.assertIn("publish_document", GROUPS[group])
        self.assertEqual(select_group("Export this as a CSV", []), "spreadsheets")
        self.assertIn("publish_document", GROUPS["spreadsheets"])


class ProxyHeaderTests(unittest.TestCase):
    def test_proxy_keeps_html_as_nosniff_attachment(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from services.orchestrator import project_ui

        responses = {
            "/api/atlas/html-inline": (
                "text/html",
                {"Content-Disposition": "inline", "Content-Security-Policy": "sandbox"},
            ),
            "/api/atlas/html-bare": ("text/html; charset=utf-8", {}),
            "/api/atlas/markdown": (
                "text/markdown; charset=utf-8",
                {"Content-Disposition": "attachment; filename*=UTF-8''n.md"},
            ),
        }

        class Backend(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                mime, headers = responses[self.path]
                body = b"<p>x</p>"
                self.send_response(200)
                self.send_header("Content-Type", mime)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        app = FastAPI()
        app.include_router(project_ui.router)
        try:
            with (
                patch.object(project_ui, "project_mode", return_value=True),
                patch.object(project_ui, "browser_origin"),
                patch.object(
                    project_ui, "verified_subject", AsyncMock(return_value="subject")
                ),
                patch.object(project_ui, "selection", return_value=(None, None)),
                patch.object(
                    project_ui,
                    "broker_identity",
                    AsyncMock(return_value=dict(identity="token")),
                ),
                patch.object(
                    project_ui,
                    "retrieval_url",
                    return_value=f"http://127.0.0.1:{server.server_port}",
                ),
            ):
                client = TestClient(app)
                for path in ("html-inline", "html-bare", "markdown"):
                    with self.subTest(path=path):
                        reply = client.get("/api/atlas/" + path)
                        self.assertEqual(reply.status_code, 200)
                        self.assertEqual(
                            reply.headers["x-content-type-options"], "nosniff"
                        )
                        self.assertTrue(
                            reply.headers["content-disposition"].startswith(
                                "attachment"
                            )
                        )
                self.assertEqual(
                    client.get("/api/atlas/html-inline").headers[
                        "content-security-policy"
                    ],
                    "sandbox",
                )
                self.assertEqual(
                    client.get("/api/atlas/markdown").headers["content-disposition"],
                    "attachment; filename*=UTF-8''n.md",
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
