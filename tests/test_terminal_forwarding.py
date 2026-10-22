"""Exercise the terminal bridge with real sockets, including half-close."""

import socket
import threading
import unittest

from packages.ports import atlas_ports
from services.orchestrator.terminal_stack import loopback_forward


class ForwardingTests(unittest.TestCase):
    def test_response_after_client_half_close(self) -> None:
        with socket.socket() as upstream:
            upstream.bind(("127.0.0.1", 0))
            upstream.listen()

            def respond() -> None:
                with upstream.accept()[0] as connection:
                    chunks = []
                    while chunk := connection.recv(4096):
                        chunks.append(chunk)
                    connection.sendall(b"result:" + b"".join(chunks))

            worker = threading.Thread(target=respond)
            worker.start()
            with loopback_forward("127.0.0.1:0", upstream.getsockname()) as address:
                with socket.create_connection(address, timeout=2) as client:
                    client.sendall(b"original-file-bytes")
                    client.shutdown(socket.SHUT_WR)
                    result = b""
                    while chunk := client.recv(4096):
                        result += chunk
                    self.assertEqual(result, b"result:original-file-bytes")
            worker.join(2)
            self.assertFalse(worker.is_alive())
        with self.assertRaises(OSError):
            socket.create_connection(address, timeout=0.2)

    def test_rejects_non_loopback_binding(self) -> None:
        with self.assertRaises(ValueError):
            with loopback_forward(
                "0.0.0.0:" + str(atlas_ports().port("terminal")), ("127.0.0.1", 1)
            ):
                self.fail("unsafe listener")
