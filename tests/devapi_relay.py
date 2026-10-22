"""Test-container HTTP relay to the sole mounted Unix socket; no external network."""

import select
import socket
import socketserver
import subprocess
import sys
import threading


def run(command: list[str], path: str = "/gateway/api.sock") -> int:
    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            with socket.socket(socket.AF_UNIX) as peer:
                peer.connect(path)
                peer.settimeout(130)
                self.request.settimeout(130)
                while True:
                    ready, _, _ = select.select([peer, self.request], [], [], 130)
                    if not ready:
                        return
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        (peer if source is self.request else self.request).sendall(data)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server(("127.0.0.1", 18030), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            return subprocess.run(command, check=False).returncode
        finally:
            server.shutdown()


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
