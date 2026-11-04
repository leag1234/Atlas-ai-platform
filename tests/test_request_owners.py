"""A generated download is served only to the user who made the request."""

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

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.request_owners import owns, record
from services.orchestrator.terminal_stack import loopback_html_injector

ALICE_REQUEST = "chatcmpl-" + "a" * 32
BOB_REQUEST = "chatcmpl-" + "b" * 32
LEGACY_REQUEST = "chatcmpl-" + "c" * 32
PREFIX = "/api/v1/terminals/atlas-files/files/serve/home/user/requests/"
SECRET = "test-proxy-secret"


class Backend(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    paths: list[str] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        type(self).paths.append(self.path)
        body = b"file bytes"
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {"ATLAS_DATA_DIR": directory.name})
        environment.start()
        self.addCleanup(environment.stop)

    def test_first_owner_wins_and_unknown_requests_are_refused(self) -> None:
        self.assertTrue(record(ALICE_REQUEST, "Alice@Example.test"))
        self.assertTrue(record(ALICE_REQUEST, "bob@example.test"))
        self.assertTrue(owns(ALICE_REQUEST, "alice@example.test"))
        self.assertFalse(owns(ALICE_REQUEST, "bob@example.test"))
        self.assertFalse(owns(LEGACY_REQUEST, "alice@example.test"))

    def test_empty_front_door_identities_name_nobody(self) -> None:
        for identity in (None, "", "  ", "@example.test"):
            with self.subTest(identity=identity):
                self.assertFalse(record(BOB_REQUEST, identity))
                self.assertFalse(owns(BOB_REQUEST, identity))
        self.assertFalse(record("../escape", "alice@example.test"))

    def test_store_holds_digests_not_addresses(self) -> None:
        record(ALICE_REQUEST, "alice@example.test")
        store = Path(os.environ["ATLAS_DATA_DIR"]) / "request-owners.sqlite"
        raw = store.read_bytes().lower()
        self.assertNotIn(b"alice", raw)


class RelayTests(unittest.TestCase):
    def setUp(self) -> None:
        Backend.paths = []
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(
            os.environ,
            {
                "ATLAS_DATA_DIR": directory.name,
                "ATLAS_STABLE": "1",
                "ATLAS_PROXY_SECRET": SECRET,
                "ATLAS_PROJECT_MODE": "0",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        record(ALICE_REQUEST, "alice@example.test")
        record(BOB_REQUEST, "bob@example.test")
        backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        self.addCleanup(backend.server_close)
        self.addCleanup(backend.shutdown)
        context = loopback_html_injector(
            "127.0.0.1:0", ("127.0.0.1", backend.server_address[1])
        )
        self.host, self.port = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def get(self, path: str, user: str) -> int:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        connection.request(
            "GET", path, headers={"X-Atlas-User": user, "X-Atlas-Proxy-Secret": SECRET}
        )
        status = connection.getresponse().status
        connection.close()
        return status

    def test_owner_downloads_and_other_users_get_not_found(self) -> None:
        download = PREFIX + ALICE_REQUEST + "/report.pdf"
        self.assertEqual(self.get(download, "alice@example.test"), 200)
        self.assertEqual(self.get(download, "ALICE@example.test"), 200)
        self.assertEqual(self.get(download, "bob@example.test"), 404)
        self.assertEqual(Backend.paths, [download, download])

    def test_requests_without_a_recorded_owner_are_not_served(self) -> None:
        self.assertEqual(
            self.get(PREFIX + LEGACY_REQUEST + "/old.docx", "alice@example.test"), 404
        )
        self.assertEqual(Backend.paths, [])

    def test_path_tricks_resolve_to_the_checked_request(self) -> None:
        for path in (
            PREFIX + ALICE_REQUEST + "/../" + BOB_REQUEST + "/secret.xlsx",
            PREFIX + ALICE_REQUEST + "%2F..%2F" + BOB_REQUEST + "/secret.xlsx",
            PREFIX.replace("/requests/", "/requests//") + BOB_REQUEST + "/secret.xlsx",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.get(path, "alice@example.test"), 404)
        self.assertEqual(Backend.paths, [])

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
        self.assertEqual(Backend.paths, [])

    def test_other_routes_are_unchanged(self) -> None:
        self.assertEqual(self.get("/api/v1/chats", "bob@example.test"), 200)
        self.assertEqual(self.get("/api/v1/terminals", "bob@example.test"), 200)


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
            self.assertTrue(owns(seen[0], "alice@example.test"))
            self.assertFalse(owns(seen[0], "bob@example.test"))


if __name__ == "__main__":
    unittest.main()
