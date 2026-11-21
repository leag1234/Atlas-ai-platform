"""Server-rendered deliverable previews: gallery page, bounds, fallback, isolation."""

from __future__ import annotations

import io
import re
import shutil
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.retrieval import deliverable_render
from services.retrieval.deliverable_render import (
    GALLERY_CSP,
    MAX_PAGES,
    Page,
    RenderUnavailable,
    Rendered,
    gallery_html,
    render_id,
    render_pages,
    render_preview,
)
from services.retrieval.project_scoped_api import preview_target

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
ROUTE = (
    "/api/atlas/orgs/11111111-1111-1111-1111-111111111111/projects/"
    "22222222-2222-2222-2222-222222222222/deliverables/"
    "33333333-3333-3333-3333-333333333333"
)


def rendered(count: int, total: int | None = None, landscape: bool = False) -> Rendered:
    return Rendered(
        tuple(Page(JPEG, 1280, 720 if landscape else 1810) for _ in range(count)),
        total or count,
        landscape,
    )


def renderer_ready() -> bool:
    return (
        deliverable_render.tools() is not None
        and deliverable_render.isolation() is not None
    )


def temporary_dirs() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("atlas-render-*")}


class GalleryTests(unittest.TestCase):
    def test_slides_are_a_carousel_with_counters_and_download(self) -> None:
        page = gallery_html(
            rendered(3, landscape=True), "deck.pptx", ROUTE + "/download"
        )
        text = page.decode()
        self.assertTrue(text.startswith("<!doctype html>"))
        self.assertIn('class="deck"', text)
        self.assertEqual(text.count("<img "), 3)
        self.assertIn("1 / 3", text)
        self.assertIn("3 / 3", text)
        self.assertIn('href="#p2"', text)
        self.assertIn(f'href="{ROUTE}/download" download', text)
        self.assertIn(">PPTX</a>", text)
        # The battery reads the first 4000 characters for a rendering.
        self.assertRegex(text[:4000], r"<(img|svg)\b")

    def test_documents_are_a_page_list_without_script(self) -> None:
        text = gallery_html(rendered(2), "report.docx", None).decode()
        self.assertIn('class="pages"', text)
        self.assertNotIn("download", text)
        self.assertNotRegex(text.lower(), r"<script|javascript:|\son\w+=")
        self.assertIn("prefers-color-scheme:dark", text)
        self.assertIn('name="viewport"', text)

    def test_names_are_escaped(self) -> None:
        text = gallery_html(rendered(1), '<script>x</script>".docx', None).decode()
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_truncated_gallery_says_how_many_pages_it_shows(self) -> None:
        text = gallery_html(rendered(60, total=75), "long.pdf", ROUTE + "/download")
        self.assertIn("1–60 / 75", text.decode())
        self.assertEqual(text.decode().count("<img "), 60)

    def test_policy_allows_inline_pictures_only(self) -> None:
        directives = dict(
            (part.split(" ", 1) + [""])[:2]
            for part in (item.strip() for item in GALLERY_CSP.split(";"))
        )
        self.assertEqual(directives["default-src"], "'none'")
        self.assertEqual(directives["img-src"], "data:")
        self.assertEqual(directives["sandbox"], "allow-downloads")
        self.assertEqual(directives["frame-ancestors"], "'self'")
        self.assertNotIn("script-src", directives)

    def test_render_identifier_is_stable_per_deliverable(self) -> None:
        identifier = "33333333-3333-3333-3333-333333333333"
        self.assertEqual(render_id(identifier), render_id(identifier))
        self.assertNotEqual(render_id(identifier), identifier)


class FallbackTests(unittest.TestCase):
    def test_absent_renderer_leaves_the_text_preview(self) -> None:
        with patch.object(deliverable_render, "tools", return_value=None):
            with self.assertRaisesRegex(RenderUnavailable, "renderer_absent"):
                render_pages(b"%PDF-1.4", ".pdf", timeout=5)
            self.assertIsNone(
                render_preview(b"%PDF-1.4", ".pdf", "a.pdf", None, timeout=5)
            )

    def test_no_network_isolation_means_no_rendering(self) -> None:
        with (
            patch.object(
                deliverable_render,
                "tools",
                return_value=dict(soffice="x", pdftoppm="x", pdfinfo="x"),
            ),
            patch.object(deliverable_render, "isolation", return_value=None),
        ):
            with self.assertRaisesRegex(RenderUnavailable, "no_network_isolation"):
                render_pages(b"%PDF-1.4", ".pdf", timeout=5)

    def test_text_formats_and_oversized_files_are_not_rendered(self) -> None:
        with self.assertRaisesRegex(RenderUnavailable, "format"):
            render_pages(b"# notes", ".md", timeout=5)
        with self.assertRaisesRegex(RenderUnavailable, "artifact_size"):
            render_pages(b"", ".pdf", timeout=5)

    def test_oversized_page_is_refused(self) -> None:
        with (
            patch.object(deliverable_render, "render_pages", return_value=rendered(3)),
            patch.object(deliverable_render, "RENDER_LIMIT", 100),
        ):
            self.assertIsNone(render_preview(b"x", ".pdf", "a.pdf", None, timeout=5))

    def test_page_bound(self) -> None:
        self.assertEqual(MAX_PAGES, 60)
        self.assertLessEqual(deliverable_render.RENDER_LIMIT, 4 * 1024 * 1024)

    def test_preview_target_only_for_text_previews(self) -> None:
        path = ROUTE.removeprefix("/api/atlas/") + "/preview"
        text = (200, b"x", {"Content-Type": "text/plain"})
        html = (200, b"x", {"Content-Type": "text/html; charset=utf-8"})
        self.assertEqual(
            preview_target("GET", path, text),
            (
                "22222222-2222-2222-2222-222222222222",
                "33333333-3333-3333-3333-333333333333",
            ),
        )
        self.assertIsNone(preview_target("GET", path, html))
        self.assertIsNone(preview_target("POST", path, text))
        self.assertIsNone(
            preview_target("GET", path.replace("preview", "download"), text)
        )


class IsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        if deliverable_render.isolation() is None:
            self.skipTest("no network namespace available")
        self.fake_tools = patch.object(
            deliverable_render,
            "tools",
            return_value=dict(
                soffice="/bin/false", pdftoppm="/bin/false", pdfinfo="/bin/false"
            ),
        )
        self.fake_tools.start()
        self.addCleanup(self.fake_tools.stop)

    def test_deadline_kills_the_whole_process_group(self) -> None:
        # A grandchild stands for LibreOffice: the deadline must not leave it.
        program = (
            "import subprocess, time\n"
            "subprocess.Popen(['sleep', '119.7'])\n"
            "time.sleep(120)\n"
        )
        before = temporary_dirs()
        with patch.object(deliverable_render, "RENDER_PROGRAM", program):
            with self.assertRaisesRegex(RenderUnavailable, "timeout"):
                render_pages(b"%PDF-1.4", ".pdf", timeout=2)
        self.assertEqual(temporary_dirs() - before, set())
        left = []
        for path in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                if b"sleep\x00119.7" in path.read_bytes():
                    left.append(path)
            except OSError:
                continue
        self.assertEqual(left, [])

    def test_child_has_no_network(self) -> None:
        program = (
            "import socket, sys\n"
            "names = [n for _, n in socket.if_nameindex()]\n"
            "sys.exit('interfaces:' + ','.join(names)) if set(names) - {'lo'} else sys.exit('isolated')\n"
        )
        with patch.object(deliverable_render, "RENDER_PROGRAM", program):
            with self.assertRaisesRegex(RenderUnavailable, "^isolated$"):
                render_pages(b"%PDF-1.4", ".pdf", timeout=10)

    def test_external_references_are_not_converted(self) -> None:
        from docx import Document

        buffer = io.BytesIO()
        Document().save(buffer)
        source = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename == "word/_rels/document.xml.rels":
                    data = data.replace(
                        b"</Relationships>",
                        b'<Relationship Id="rIdX" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="http://192.0.2.1/x.png" TargetMode="External"/></Relationships>',
                    )
                target.writestr(item, data)
        with self.assertRaisesRegex(RenderUnavailable, "(?i)external"):
            render_pages(output.getvalue(), ".docx", timeout=20)


@unittest.skipUnless(
    renderer_ready(), "LibreOffice, poppler or network isolation absent"
)
class RealConversionTests(unittest.TestCase):
    def build(self, suffix: str) -> bytes:
        from packages.doc_render import render
        from packages.doc_render.schema import Content

        content = Content.model_validate(
            dict(
                title="Quarterly review",
                sections=[
                    dict(
                        heading=f"Part {index}",
                        paragraphs=[f"Paragraph {index} of the review."],
                        bullets=["First point", "Second point"],
                        tables=[
                            dict(
                                headers=["Item", "Amount"],
                                rows=[["A", "12"], ["B", "30"]],
                            )
                        ],
                    )
                    for index in range(1, 4)
                ],
            )
        )
        directory = Path(tempfile.mkdtemp(prefix="atlas-render-test-"))
        try:
            path = directory / ("review" + suffix)
            render(content, path, image_root=directory)
            return path.read_bytes()
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def check(self, suffix: str, landscape: bool) -> None:
        before = temporary_dirs()
        page = render_preview(
            self.build(suffix),
            suffix,
            "review" + suffix,
            ROUTE + "/download",
            timeout=60,
        )
        self.assertIsNotNone(page)
        assert page is not None
        text = page.decode()
        images = re.findall(r'<img src="data:image/jpeg;base64,', text)
        self.assertGreaterEqual(len(images), 1)
        self.assertEqual(f'class="{"deck" if landscape else "pages"}"' in text, True)
        self.assertLessEqual(len(page), deliverable_render.RENDER_LIMIT)
        self.assertEqual(temporary_dirs() - before, set())

    def test_docx(self) -> None:
        self.check(".docx", landscape=False)

    def test_pptx(self) -> None:
        self.check(".pptx", landscape=True)


class ProxyGalleryTests(unittest.TestCase):
    def test_only_the_preview_route_shows_html_inline_under_the_proxy_policy(
        self,
    ) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from services.orchestrator import project_ui

        preview = ROUTE + "/preview"

        class Backend(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                body = b"<img src=data:image/jpeg;base64,AA>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                # A weaker upstream policy must not reach the browser.
                self.send_header("Content-Security-Policy", "default-src *")
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
                reply = client.get(preview)
                self.assertEqual(reply.status_code, 200)
                self.assertEqual(reply.headers["content-disposition"], "inline")
                self.assertEqual(reply.headers["content-security-policy"], GALLERY_CSP)
                self.assertEqual(reply.headers["x-content-type-options"], "nosniff")
                for other in (
                    ROUTE + "/download",
                    ROUTE.replace("deliverables", "files") + "/preview",
                ):
                    with self.subTest(path=other):
                        reply = client.get(other)
                        self.assertTrue(
                            reply.headers["content-disposition"].startswith(
                                "attachment"
                            )
                        )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
