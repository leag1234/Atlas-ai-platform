"""The UI relay must stream large uploads and inject the shell on every page load."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import os
import threading
import unittest
from unittest.mock import patch

from services.orchestrator.terminal_stack import loopback_html_injector

SHELL = b"<html><head><title>x</title></head><body>shell</body></html>"


class Backend(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[tuple[str, bytes]] = []
    headers_seen: list[dict[str, str]] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        type(self).headers_seen.append(
            {key.lower(): value for key, value in self.headers.items()}
        )
        body = SHELL if self.path == "/" else b"plain"
        self.send_response(200)
        self.send_header(
            "Content-Type", "text/html" if self.path == "/" else "text/plain"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(SHELL)))
        self.end_headers()

    def do_POST(self) -> None:
        size = int(self.headers["Content-Length"])
        data = self.rfile.read(size)
        type(self).requests.append((self.path, data))
        body = str(len(data)).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class UiProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        Backend.requests = []
        Backend.headers_seen = []
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.addCleanup(self.backend.shutdown)
        self.addCleanup(self.backend.server_close)
        context = loopback_html_injector(
            "127.0.0.1:0", ("127.0.0.1", self.backend.server_address[1])
        )
        self.host, self.port = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def test_stable_relay_requires_nginx_identity_and_shared_secret(self) -> None:
        with patch.dict(
            os.environ,
            {"ATLAS_STABLE": "1", "ATLAS_PROXY_SECRET": "test-proxy-secret"},
            clear=False,
        ):
            context = loopback_html_injector(
                "127.0.0.1:0", ("127.0.0.1", self.backend.server_address[1])
            )
            host, port = context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)
            connection = http.client.HTTPConnection(host, port, timeout=10)
            connection.request("GET", "/", headers={"X-Atlas-User": "guest"})
            self.assertEqual(connection.getresponse().status, 403)
            connection.close()
            connection = http.client.HTTPConnection(host, port, timeout=10)
            connection.request(
                "GET",
                "/",
                headers={
                    "X-Atlas-User": "guest",
                    "X-Atlas-User-Role": "admin",
                    "X-Atlas-Proxy-Secret": "test-proxy-secret",
                },
            )
            self.assertEqual(connection.getresponse().status, 200)
            connection.close()
        headers = Backend.headers_seen[-1]
        self.assertEqual(headers["x-atlas-user"], "guest")
        self.assertEqual(headers["x-atlas-user-role"], "user")
        self.assertNotIn("x-atlas-proxy-secret", headers)

    def test_large_upload_is_streamed_to_the_backend(self) -> None:
        for size in (10_000, 300_000, 3_000_000):
            status, data = self.request("POST", "/upload", b"x" * size)
            self.assertEqual((status, data), (200, str(size).encode()))

    def test_file_upload_processing_is_disabled_without_changing_body(self) -> None:
        body = (
            b"--boundary\r\nprocess=true&keep=this-exact-body\x00\r\n--boundary--\r\n"
        )
        status, response = self.request(
            "POST",
            "/api/v1/files/?process=true&process_in_background=true&keep=1",
            body,
        )
        self.assertEqual((status, response), (200, str(len(body)).encode()))
        self.assertEqual(
            Backend.requests,
            [
                (
                    "/api/v1/files/?process=false&process_in_background=false&keep=1",
                    body,
                )
            ],
        )

    def test_default_file_upload_query_gets_both_processing_switches(self) -> None:
        self.assertEqual(
            self.request("POST", "/api/v1/files/", b"unchanged"),
            (200, b"9"),
        )
        self.assertEqual(
            Backend.requests,
            [
                (
                    "/api/v1/files/?process=false&process_in_background=false",
                    b"unchanged",
                )
            ],
        )

    def test_every_page_load_gets_the_assets(self) -> None:
        for _ in range(3):
            status, data = self.request("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn(b"/static/atlas/chat-ui.js", data)
            self.assertTrue(data.startswith(b"<html><head><title>x</title>"))

    def test_other_responses_and_head_requests_pass_through(self) -> None:
        self.assertEqual(self.request("GET", "/api"), (200, b"plain"))
        status, data = self.request("HEAD", "/")
        self.assertEqual((status, data), (200, b""))


if __name__ == "__main__":
    unittest.main()
