"""REQ-PROJ-08: scoped owned route, fail-closed assets and shell injection."""

import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web
from starlette.requests import Request

from fastapi.testclient import TestClient

from services.orchestrator.chat_api import app
from services.orchestrator.ui_injection import inject_atlas_ui
from services.orchestrator.project_ui import verified_subject


class ProjectSurfaceTests(unittest.TestCase):
    def test_legacy_entry_redirects_only_for_project_mode(self) -> None:
        client = TestClient(app)
        with patch.dict("os.environ", {"ATLAS_PROJECT_MODE": "1"}):
            response = client.get("/project-ui", follow_redirects=False)
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers["location"], "/project-mode")
        with patch.dict(
            "os.environ", {"ATLAS_PROJECT_MODE": "0", "ATLAS_PROJECT_DSN": ""}
        ):
            self.assertEqual(client.get("/project-ui").status_code, 200)

    def test_owned_route_requires_project_installation(self) -> None:
        with patch.dict(
            "os.environ", {"ATLAS_PROJECT_MODE": "0", "ATLAS_PROJECT_DSN": ""}
        ):
            response = TestClient(app).get("/project-mode")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "feature_not_available")

    def test_owned_route_and_closed_asset_allowlist(self) -> None:
        with patch.dict("os.environ", {"ATLAS_PROJECT_MODE": "1"}):
            client = TestClient(app)
            page = client.get("/project-mode")
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.headers["cache-control"], "private, no-store")
            self.assertIn(
                "frame-ancestors 'none'", page.headers["content-security-policy"]
            )
            for asset, mime in (
                ("client.js", "text/javascript"),
                ("locale.js", "text/javascript"),
                ("style.css", "text/css"),
            ):
                response = client.get("/project-mode/" + asset)
                self.assertEqual(response.status_code, 200)
                self.assertIn(mime, response.headers["content-type"])
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertEqual(client.get("/project-mode/project_ui.py").status_code, 404)

    def test_injection_keeps_host_and_stop_assets_and_is_idempotent(self) -> None:
        shell = b'<html><head></head><body><div id="chat-input"></div></body></html>'
        injected = inject_atlas_ui(shell)
        self.assertIn(b"/static/atlas/chat-ui.js", injected)
        self.assertIn(b"/project-mode/client.js", injected)
        self.assertEqual(inject_atlas_ui(injected), injected)
        self.assertEqual(injected.split(b"</head>")[1], shell.split(b"</head>")[1])


class HostIdentityEncodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_compressing_host_auth_works_with_streaming_client(self) -> None:
        import gzip
        import json

        async def auth(request: web.Request) -> web.Response:
            self.assertEqual(request.headers["Authorization"], "Bearer synthetic")
            body = json.dumps({"email": "synthetic@example.test"}).encode()
            if "gzip" in request.headers.get("Accept-Encoding", ""):
                return web.Response(
                    body=gzip.compress(body),
                    headers={"Content-Encoding": "gzip"},
                    content_type="application/json",
                )
            return web.Response(body=body, content_type="application/json")

        application = web.Application()
        application.router.add_get("/api/v1/auths/", auth)
        runner = web.AppRunner(application)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            host = f"http://127.0.0.1:{runner.addresses[0][1]}"
            request = Request(
                {"type": "http", "headers": [(b"cookie", b"token=synthetic")]}
            )
            with patch.dict("os.environ", {"ATLAS_WEBUI_URL": host}):
                async with aiohttp.ClientSession(auto_decompress=False) as client:
                    self.assertEqual(
                        await verified_subject(request, client),
                        "synthetic@example.test",
                    )
        finally:
            await runner.cleanup()
