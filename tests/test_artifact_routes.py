"""Artefact route ownership, modes, and response isolation tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import http.client
import threading
import time
from collections import Counter

from fastapi.testclient import TestClient

from services.orchestrator import artifact_store
from services.orchestrator.artifact_routes import (
    DOWNLOAD_HEADERS,
    PREVIEW_CSP,
    PREVIEW_HEADERS,
    STORAGE_SHIM,
)
from services.orchestrator.artifact_tokens import issue_token
from services.orchestrator.chat_api import app
from services.orchestrator.terminal_stack import loopback_html_injector


CONTENT = b"<html><head><title>probe</title></head><body>app</body></html>"
SIGNING_KEY = "route-signing-key-at-least-32-bytes"


class ArtifactRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = {
            "ATLAS_DATA_DIR": self.directory.name,
            "ATLAS_ARTIFACTS": "1",
            "ATLAS_PREVIEW_SIGNING_KEY": SIGNING_KEY,
            "ATLAS_PREVIEW_TOKEN_SECONDS": "900",
            "ATLAS_PREVIEW_ORIGIN": "",
            "ATLAS_TRUSTED_EMAIL_HEADER": "X-Atlas-User",
            "ATLAS_PROXY_SECRET": "proxy-secret",
            "ATLAS_PROJECT_MODE": "0",
            "ATLAS_PROJECT_DSN": "",
            "ATLAS_PROJECT_ONLY": "0",
        }
        self.patch = patch.dict(os.environ, self.environment)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.owner_headers = {
            "X-Atlas-User": "owner@example.test",
            "X-Atlas-Proxy-Secret": "proxy-secret",
        }
        self.artifact = artifact_store.create(
            "owner@example.test", "Probe", "probe.html", CONTENT, "request-1"
        )
        self.artifact_id = str(self.artifact["id"])
        self.version = int(self.artifact["version"])

    def owner(self, subject: str = "owner@example.test") -> dict[str, str]:
        return {**self.owner_headers, "X-Atlas-User": subject}

    def token(
        self,
        artifact_id: str | None = None,
        version: int = 1,
        now: int | None = None,
    ) -> str:
        return issue_token(artifact_id or self.artifact_id, version, now=now)

    def preview_url(self, token: str, filename: str = "probe.html") -> str:
        return f"/api/atlas/preview/{token}/{quote(filename, safe='')}"

    def assert_preview_headers(self, response: TestClient) -> None:
        self.assertEqual(
            response.headers["content-security-policy"], PREVIEW_CSP + "'self'"
        )
        expected = {
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
            "cache-control": "private, no-store",
            "cross-origin-resource-policy": "same-origin",
            "cross-origin-opener-policy": "same-origin",
            "x-robots-tag": "noindex, nofollow",
        }
        for name, value in expected.items():
            self.assertEqual(response.headers[name], value)
        self.assertNotIn("set-cookie", response.headers)
        self.assertNotIn("access-control-allow-origin", response.headers)
        self.assertNotIn(
            "allow-same-origin", response.headers["content-security-policy"]
        )

    def test_feature_off_answers_404(self) -> None:
        with patch.dict(os.environ, {"ATLAS_ARTIFACTS": "0"}):
            response = self.client.get(
                f"/api/atlas/artifacts/{self.artifact_id}/versions/1/open",
                headers=self.owner(),
            )
        self.assertEqual(response.status_code, 404)

    def test_open_and_preview_mode_a(self) -> None:
        response = self.client.get(
            f"/api/atlas/artifacts/{self.artifact_id}/versions/1/open",
            headers=self.owner(),
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303, response.text)
        self.assertTrue(response.headers["location"].startswith("/api/atlas/preview/"))
        token = response.headers["location"].split("/")[4]
        preview = self.client.get(self.preview_url(token), headers=self.owner())
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b"localStorage", preview.content)
        self.assert_preview_headers(preview)

    def test_storage_shim_replaces_inaccessible_storage(self) -> None:
        script = (
            STORAGE_SHIM.decode("utf-8")
            .removeprefix("<script>")
            .removesuffix("</script>")
        )
        node = shutil.which("node")
        if node is None:
            self.assertIn("Object.defineProperty(window,n,{value:f()})", script)
            return
        harness = """
const vm = require('node:vm');
const context = vm.createContext({window: {}, require});
const window = context.window;
vm.runInContext(`
Object.defineProperty(window, 'localStorage', {configurable: true, get() { throw new Error('SecurityError'); }});
Object.defineProperty(window, 'sessionStorage', {configurable: true, get() { throw new Error('SecurityError'); }});
`, context);
vm.runInContext(process.argv[1], context);
for (const name of ['localStorage', 'sessionStorage']) {
  const storage = window[name];
  storage.setItem('first', '1');
  storage.setItem('second', '2');
  if (storage.getItem('first') !== '1' || storage.length !== 2) throw new Error(name + ': values');
  if (storage.key(1) !== 'second') throw new Error(name + ': key');
  storage.removeItem('first');
  if (storage.getItem('first') !== null || storage.length !== 1) throw new Error(name + ': remove');
  storage.clear();
  if (storage.length !== 0 || storage.key(0) !== null) throw new Error(name + ': clear');
}
"""
        subprocess.run(
            [node, "-"],
            input=harness.replace("process.argv[1]", repr(script)),
            check=True,
            capture_output=True,
            text=True,
        )

    def test_mode_a_requires_owner(self) -> None:
        token = self.token()
        for headers in ({}, self.owner("other@example.test")):
            response = self.client.get(self.preview_url(token), headers=headers)
            self.assertEqual(response.status_code, 404)
            self.assert_preview_headers(response)

    def test_preview_refusals_all_have_headers(self) -> None:
        valid = self.token()
        forged = valid[:-1] + ("A" if valid[-1] != "A" else "B")
        other = self.token("b" * 32)
        urls = [
            self.preview_url(forged),
            self.preview_url(other),
            self.preview_url(valid, "wrong.html"),
            self.preview_url(valid) + "/extra",
            self.preview_url(valid) + "/",
            self.preview_url(valid, "%2e%2e"),
            self.preview_url(valid, "%2f"),
        ]
        for url in urls:
            response = self.client.get(url, headers=self.owner())
            self.assertEqual(response.status_code, 404, url)
            self.assert_preview_headers(response)
        response = self.client.head(self.preview_url(valid), headers=self.owner())
        self.assertEqual(response.status_code, 200)
        self.assert_preview_headers(response)
        response = self.client.post(self.preview_url(valid), headers=self.owner())
        self.assertEqual(response.status_code, 405)
        self.assert_preview_headers(response)

    def test_expired_token_and_store_failure(self) -> None:
        token = self.token(now=int(time.time()) - 900)
        response = self.client.get(self.preview_url(token), headers=self.owner())
        self.assertEqual(response.status_code, 404)
        self.assert_preview_headers(response)
        token = self.token()
        with patch.object(
            artifact_store,
            "read",
            side_effect=artifact_store.ArtifactError("artifact_unavailable"),
        ):
            response = self.client.get(self.preview_url(token), headers=self.owner())
        self.assertEqual(response.status_code, 404)
        self.assert_preview_headers(response)

    def test_mode_b_host_and_identity_rules(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ATLAS_PREVIEW_ORIGIN": "https://preview.example.test",
                "ATLAS_PREVIEW_FRAME_ANCESTORS": "https://atlas.example.test",
            },
        ):
            open_response = self.client.get(
                f"/api/atlas/artifacts/{self.artifact_id}/versions/1/open",
                headers=self.owner(),
                follow_redirects=False,
            )
            self.assertEqual(open_response.status_code, 303)
            self.assertTrue(
                open_response.headers["location"].startswith(
                    "https://preview.example.test/api/atlas/preview/"
                )
            )
            token = open_response.headers["location"].split("/")[6]
            preview = self.client.get(
                self.preview_url(token),
                headers={"host": "preview.example.test"},
            )
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(
                preview.headers["content-security-policy"],
                PREVIEW_CSP + "https://atlas.example.test",
            )
            wrong_host = self.client.get(self.preview_url(token))
            self.assertEqual(wrong_host.status_code, 404)
            atlas_on_preview = self.client.get(
                f"/api/atlas/artifacts/{self.artifact_id}/versions/1/open",
                headers={**self.owner(), "Host": "preview.example.test"},
            )
            self.assertEqual(atlas_on_preview.status_code, 404)

    def test_download_is_original_attachment(self) -> None:
        response = self.client.get(
            f"/api/atlas/artifacts/{self.artifact_id}/versions/1/download",
            headers=self.owner(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, CONTENT)
        self.assertEqual(response.headers["content-type"], "text/html; charset=utf-8")
        self.assertTrue(
            response.headers["content-disposition"].startswith("attachment;")
        )
        self.assertEqual(response.headers["content-security-policy"], "sandbox")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_isolation_headers_are_set_exactly_once(self) -> None:
        open_response = self.client.get(
            f"/api/atlas/artifacts/{self.artifact_id}/versions/1/open",
            headers=self.owner(),
            follow_redirects=False,
        )
        self.assertEqual(open_response.status_code, 303)
        token = open_response.headers["location"].split("/")[4]
        preview = self.client.get(self.preview_url(token), headers=self.owner())
        self.assertEqual(preview.status_code, 200)
        download = self.client.get(
            f"/api/atlas/artifacts/{self.artifact_id}/versions/1/download",
            headers=self.owner(),
        )
        self.assertEqual(download.status_code, 200)

        for response, expected_headers in (
            (preview, PREVIEW_HEADERS),
            (download, DOWNLOAD_HEADERS),
        ):
            raw_headers = response.headers.raw
            counts = Counter(name.lower() for name, _ in raw_headers)
            for name in expected_headers:
                self.assertEqual(counts[name.lower().encode()], 1, name)

    def test_open_foreign_owner_is_404(self) -> None:
        response = self.client.get(
            f"/api/atlas/artifacts/{self.artifact_id}/versions/1/open",
            headers=self.owner("other@example.test"),
        )
        self.assertEqual(response.status_code, 404)

    def test_no_allow_same_origin_in_sources(self) -> None:
        paths = [
            Path("services/orchestrator/chat-ui.js"),
            Path("services/orchestrator/chat-ui.css"),
            *Path("services/orchestrator").glob("artifact_*.py"),
        ]
        for path in paths:
            self.assertNotIn("allow-same-origin", path.read_text())


class RelayBackend(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        body = b"<html><head></head><body>unchanged</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        if self.path.endswith("/attachment"):
            self.send_header("Content-Disposition", "attachment")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ArtifactRelayTests(unittest.TestCase):
    def test_preview_and_attachment_are_byte_identical(self) -> None:
        backend = ThreadingHTTPServer(("127.0.0.1", 0), RelayBackend)
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        self.addCleanup(backend.shutdown)
        self.addCleanup(backend.server_close)
        with patch("services.orchestrator.terminal_stack.atlas_ports") as ports:
            ports.return_value.port.return_value = backend.server_address[1]
            with loopback_html_injector(
                "127.0.0.1:0", ("127.0.0.1", backend.server_address[1])
            ) as (host, port):
                for path in (
                    "/api/atlas/preview/token/probe.html",
                    "/api/atlas/preview/token/probe.html/attachment",
                ):
                    connection = http.client.HTTPConnection(host, port, timeout=5)
                    connection.request("GET", path)
                    response = connection.getresponse()
                    body = response.read()
                    connection.close()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        body, b"<html><head></head><body>unchanged</body></html>"
                    )


if __name__ == "__main__":
    unittest.main()
