"""The project UI renders private content as text and rejects foreign origins."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.orchestrator.chat_api import app
from services.retrieval.api import app as retrieval


class ProjectUITests(unittest.TestCase):
    def test_ui_controls_and_content_policy(self) -> None:
        client = TestClient(app)
        page = client.get("/project-ui")
        self.assertEqual(page.status_code, 200)
        self.assertIn("script-src 'self'", page.headers["content-security-policy"])
        for control in (
            "projects",
            "conversations",
            "facts",
            "erase",
            "instructions",
            "add-document",
        ):
            self.assertIn('id="' + control + '"', page.text)
        script = client.get("/project-ui.js").text
        self.assertIn("textContent", script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("eval(", script)

    def test_external_browser_origins_rejected(self) -> None:
        for application in (app, retrieval):
            client = TestClient(application)
            reply = client.post(
                "/projects",
                headers={"Origin": "https://external.invalid"},
                json={"name": "Should not exist"},
            )
            self.assertEqual(reply.status_code, 403)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {"ATLAS_INTERACTION_DIR": directory}),
            patch("services.orchestrator.chat_api.process"),
        ):
            reply = TestClient(app).post(
                "/v1/chat/completions",
                headers={"Origin": "https://external.invalid"},
                json={"messages": [{"role": "user", "content": "Question"}]},
            )
            self.assertEqual(reply.status_code, 403)
            self.assertFalse(list(Path(directory).glob("*")))

    def test_retrieval_body_limit_and_private_validation_errors(self) -> None:
        client = TestClient(retrieval)
        self.assertEqual(
            client.post("/projects", content=b"x" * 160001).status_code, 413
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {"ATLAS_PROJECT_DB": str(Path(directory) / "projects.sqlite")},
            ),
        ):
            reply = client.post(
                "/projects", json={"name": "", "instructions": "private-sentinel"}
            )
            self.assertEqual(reply.status_code, 422)
            self.assertNotIn("private-sentinel", reply.text)

    def test_proxy_forwards_only_local_project_paths(self) -> None:
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        captured: list[tuple[str, bytes]] = []

        class Backend(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                captured.append((self.path, body))
                response = json.dumps({"id": "synthetic-project"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(
                "os.environ",
                {"ATLAS_RETRIEVAL_URL": f"http://127.0.0.1:{server.server_port}"},
            ):
                client = TestClient(app)
                response = client.post("/projects", json={"name": "Synthétique"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["id"], "synthetic-project")
                self.assertEqual(captured[0][0], "/projects")
                self.assertEqual(json.loads(captured[0][1]), {"name": "Synthétique"})
                self.assertEqual(
                    client.post("/projects", content=b"x" * 160001).status_code, 413
                )
                self.assertEqual(
                    client.get("/projects/invalid%3Fpath").status_code, 400
                )
                self.assertEqual(len(captured), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        with patch.dict(
            "os.environ",
            {"ATLAS_RETRIEVAL_URL": f"http://127.0.0.1:{server.server_port}"},
        ):
            self.assertEqual(TestClient(app).get("/projects").status_code, 503)
