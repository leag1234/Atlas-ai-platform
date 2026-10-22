"""Exercise the test relay with a real local Unix peer and subprocess HTTP client."""

import socket
import sys
import tempfile
import threading
from pathlib import Path
import unittest
from devapi_relay import run


class DevRelayTests(unittest.TestCase):
    def test_byte_exact_roundtrip_and_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "api.sock")
            with socket.socket(socket.AF_UNIX) as peer:
                peer.bind(path)
                peer.listen()
                received = []

                def handle() -> None:
                    connection, _ = peer.accept()
                    with connection:
                        received.append(connection.recv(4096))
                        connection.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
                        )

                thread = threading.Thread(target=handle)
                thread.start()
                code = run(
                    [
                        sys.executable,
                        "-c",
                        "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:18030/v1/models').read()==b'ok'",
                    ],
                    path,
                )
                thread.join(timeout=2)
                self.assertEqual(code, 0)
                self.assertFalse(thread.is_alive())
                self.assertTrue(received[0].startswith(b"GET /v1/models HTTP/1.1"))
        self.assertEqual(run([sys.executable, "-c", "raise SystemExit(7)"]), 7)
