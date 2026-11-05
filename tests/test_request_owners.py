"""Generated downloads: the owner's identity, the relay's own route, safe headers."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.orchestrator import serving
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.request_owners import may_read, record
from services.orchestrator.terminal_stack import loopback_html_injector

ALICE_REQUEST = "chatcmpl-" + "a" * 32
BOB_REQUEST = "chatcmpl-" + "b" * 32
LEGACY_REQUEST = "chatcmpl-" + "c" * 32
PREFIX = "/api/v1/terminals/atlas-files/files/serve/home/user/requests/"
SECRET = "test-proxy-secret"
KEY = "test-terminal-key"
TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".xml": "application/xml",
    ".js": "text/javascript; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class WebUi(BaseHTTPRequestHandler):
    """Stands in for Open WebUI: downloads must no longer reach it."""

    protocol_version = "HTTP/1.1"
    paths: list[str] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        type(self).paths.append(self.path)
        body = b"webui"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Files(BaseHTTPRequestHandler):
    """Stands in for the files terminal's /files/serve route."""

    protocol_version = "HTTP/1.1"
    seen: list[tuple[str, str, str | None]] = []

    def log_message(self, *args: object) -> None:
        pass

    def answer(self, body: bool) -> None:
        type(self).seen.append(
            (self.command, self.path, self.headers.get("Authorization"))
        )
        suffix = "." + self.path.rsplit(".", 1)[-1]
        if self.headers.get("Authorization") != "Bearer " + KEY or (
            "missing" in self.path
        ):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = b"bytes of " + self.path.encode()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", "upstream=1")
        self.end_headers()
        if body:
            self.wfile.write(data)

    def do_GET(self) -> None:
        self.answer(True)

    def do_HEAD(self) -> None:
        self.answer(False)


def server(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    instance = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    return instance


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {"ATLAS_DATA_DIR": directory.name})
        environment.start()
        self.addCleanup(environment.stop)

    def test_first_owner_wins_and_legacy_requests_stay_readable(self) -> None:
        self.assertTrue(record(ALICE_REQUEST, "Alice@Example.test"))
        self.assertTrue(record(ALICE_REQUEST, "bob@example.test"))
        self.assertTrue(may_read(ALICE_REQUEST, "alice@example.test"))
        self.assertFalse(may_read(ALICE_REQUEST, "bob@example.test"))
        self.assertTrue(may_read(LEGACY_REQUEST, "bob@example.test"))

    def test_empty_front_door_identities_name_nobody(self) -> None:
        for identity in (None, "", "  ", "@example.test"):
            with self.subTest(identity=identity):
                self.assertFalse(record(BOB_REQUEST, identity))
                self.assertFalse(may_read(LEGACY_REQUEST, identity))
        self.assertFalse(record("../escape", "alice@example.test"))

    def test_store_holds_digests_not_addresses(self) -> None:
        record(ALICE_REQUEST, "alice@example.test")
        store = Path(os.environ["ATLAS_DATA_DIR"]) / "request-owners.sqlite"
        self.assertNotIn(b"alice", store.read_bytes().lower())


class RelayTests(unittest.TestCase):
    def setUp(self) -> None:
        WebUi.paths, Files.seen = [], []
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(
            os.environ,
            {
                "ATLAS_DATA_DIR": directory.name,
                "ATLAS_STABLE": "1",
                "ATLAS_PROXY_SECRET": SECRET,
                "ATLAS_PROJECT_MODE": "0",
                "OPEN_TERMINAL_API_KEY": KEY,
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        record(ALICE_REQUEST, "alice@example.test")
        record(BOB_REQUEST, "bob@example.test")
        webui, files = server(WebUi), server(Files)
        for instance in (webui, files):
            self.addCleanup(instance.server_close)
            self.addCleanup(instance.shutdown)
        context = loopback_html_injector(
            "127.0.0.1:0",
            ("127.0.0.1", webui.server_address[1]),
            ("127.0.0.1", files.server_address[1]),
        )
        self.host, self.port = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def fetch(
        self, path: str, user: str, method: str = "GET"
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        connection.request(
            method,
            path,
            headers={
                "X-Atlas-User": user,
                "X-Atlas-Proxy-Secret": SECRET,
                "Cookie": "token=browser-session",
            },
        )
        response = connection.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, headers, body

    def get(self, path: str, user: str) -> int:
        return self.fetch(path, user)[0]

    def test_owner_downloads_from_the_files_terminal_others_get_not_found(
        self,
    ) -> None:
        download = PREFIX + ALICE_REQUEST + "/report.pdf"
        status, headers, body = self.fetch(download, "ALICE@example.test")
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            b"bytes of /files/serve/home/user/requests/"
            + ALICE_REQUEST.encode()
            + b"/report.pdf",
        )
        self.assertEqual(self.get(download, "bob@example.test"), 404)
        self.assertEqual(
            Files.seen,
            [
                (
                    "GET",
                    download.removeprefix("/api/v1/terminals/atlas-files"),
                    "Bearer " + KEY,
                )
            ],
        )
        self.assertEqual(WebUi.paths, [])
        self.assertNotIn("set-cookie", headers)

    def test_legacy_links_without_a_recorded_owner_keep_working(self) -> None:
        self.assertEqual(
            self.get(PREFIX + LEGACY_REQUEST + "/old.docx", "bob@example.test"), 200
        )
        self.assertEqual(
            self.get(PREFIX + LEGACY_REQUEST + "/missing.pdf", "bob@example.test"), 404
        )

    def test_head_is_answered_without_a_body(self) -> None:
        status, headers, body = self.fetch(
            PREFIX + ALICE_REQUEST + "/report.pdf", "alice@example.test", "HEAD"
        )
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["content-type"], "application/pdf")

    def test_path_tricks_resolve_to_the_checked_request(self) -> None:
        for path in (
            PREFIX + ALICE_REQUEST + "/../" + BOB_REQUEST + "/secret.xlsx",
            PREFIX + ALICE_REQUEST + "%2F..%2F" + BOB_REQUEST + "/secret.xlsx",
            PREFIX.replace("/requests/", "/requests//") + BOB_REQUEST + "/secret.xlsx",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, "alice@example.test"), 404)
        self.assertEqual(Files.seen, [])

    def test_a_second_identity_header_is_refused(self) -> None:
        with socket.create_connection((self.host, self.port), timeout=10) as raw:
            raw.sendall(
                (
                    "GET " + PREFIX + BOB_REQUEST + "/secret.xlsx HTTP/1.1\r\n"
                    "Host: atlas.test\r\nX-Atlas-User: alice@example.test\r\n"
                    "X-Atlas-User: bob@example.test\r\n"
                    "X-Atlas-Proxy-Secret: " + SECRET + "\r\n\r\n"
                ).encode()
            )
            self.assertTrue(raw.recv(1024).startswith(b"HTTP/1.1 404"))
        self.assertEqual(Files.seen, [])

    def test_active_content_is_downloaded_and_never_sniffed(self) -> None:
        expected = {
            "report.pdf": "inline",
            "chart.png": "inline",
            "notes.txt": "inline",
            "page.html": "attachment",
            "logo.svg": "attachment",
            "feed.xml": "attachment",
            "app.js": "attachment",
            "letter.docx": "attachment",
            "blob.bin": "attachment",
        }
        for name, disposition in expected.items():
            with self.subTest(name=name):
                status, headers, _ = self.fetch(
                    PREFIX + ALICE_REQUEST + "/" + name, "alice@example.test"
                )
                self.assertEqual(status, 200)
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(
                    headers["content-disposition"],
                    disposition
                    + '; filename="'
                    + name
                    + "\"; filename*=UTF-8''"
                    + name,
                )
                self.assertEqual(headers["cache-control"], "private, no-store")

    def test_other_routes_still_reach_the_web_ui(self) -> None:
        self.assertEqual(self.get("/api/v1/chats", "bob@example.test"), 200)
        self.assertEqual(self.get("/api/v1/terminals", "bob@example.test"), 200)
        self.assertEqual(WebUi.paths, ["/api/v1/chats", "/api/v1/terminals"])
        self.assertEqual(Files.seen, [])


class StableConnectionTests(unittest.TestCase):
    def start(self, stable: bool) -> tuple[str, list[str]]:
        with (
            patch.dict(
                os.environ,
                {"OPEN_TERMINAL_API_KEY": "test-only", "ATLAS_PORT_BASE": "0"},
            ),
            patch(
                "services.orchestrator.terminal_stack.network_gateway",
                return_value="172.30.0.1",
            ),
            patch.object(serving, "stable_mode", return_value=stable),
            patch.object(serving, "stable_webui_options", return_value=[]),
            patch.object(serving, "docker_run") as run,
        ):
            serving.webui("test-ui", True)
            value = os.environ["TERMINAL_SERVER_CONNECTIONS"]
        return value, run.call_args.args[2]

    def test_stable_web_ui_has_no_files_terminal_connection(self) -> None:
        value, options = self.start(True)
        # Admins bypass connection grants: any connection would give them the
        # shared volume, its terminal tools and the key in the admin settings.
        self.assertEqual(json.loads(value), [])
        self.assertNotIn("test-only", value)
        # Passed explicitly, so the connection in infra/chat-ui.env never applies.
        index = options.index("TERMINAL_SERVER_CONNECTIONS")
        self.assertEqual(options[index - 1], "-e")

    def test_other_modes_keep_the_filesystem_connection(self) -> None:
        value, _ = self.start(False)
        connection = json.loads(value)[0]
        self.assertEqual(connection["config"], {"chat_uploads": "filesystem"})
        self.assertEqual(connection["key"], "test-only")


class RecordingTests(unittest.TestCase):
    def test_chat_request_binds_its_directory_to_the_forwarded_user(self) -> None:
        seen: list[str] = []

        async def provider(request: ChatRequest, item: Interaction) -> None:
            seen.append(item.request_id)
            item.state, item.reponse = "done", "ok"

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {"ATLAS_DATA_DIR": root, "ATLAS_INTERACTION_DIR": root},
            ),
            patch("services.orchestrator.chat_api.process", side_effect=provider),
        ):
            from services.orchestrator.chat_api import app

            response = TestClient(app).post(
                "/v1/chat/completions",
                content=json.dumps(
                    {"messages": [{"role": "user", "content": "Bonjour"}]}
                ),
                headers={
                    "Content-Type": "application/json",
                    "X-OpenWebUI-User-Id": "user-1",
                    "X-OpenWebUI-User-Email": "Alice@Example.test",
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(seen), 1)
            self.assertTrue(may_read(seen[0], "alice@example.test"))
            self.assertFalse(may_read(seen[0], "bob@example.test"))


if __name__ == "__main__":
    unittest.main()
