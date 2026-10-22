"""Local liveness server; no provider or GPU dependency."""

from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status = 200 if self.path == "/health" else 404
        body = json.dumps(
            {"status": "ok"} if status == 200 else {"error": "not_found"}
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Never log user-controlled paths or headers.
        print(json.dumps({"service": "edge-bff", "event": "http_request"}), flush=True)


@contextmanager
def running_server() -> Iterator[str]:
    with ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/health"
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    with ThreadingHTTPServer(("127.0.0.1", 8080), HealthHandler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
