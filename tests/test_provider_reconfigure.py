"""Provider switches preserve installed data and verify before replacing configuration."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

from scripts import atlas_setup


class ReconfigureTests(unittest.TestCase):
    def test_preserves_nonprovider_records_including_multiline_and_duplicates(
        self,
    ) -> None:
        preserved = (
            "# installed data\nPOSTGRES_PASSWORD='dollar$quote\\'slash\\\\'\n"
            'WEBUI_SECRET_KEY="unchanged"\nSEARXNG_SECRET=unchanged\n'
            "CUSTOM='line one\nATLAS_TEXT_MODEL=not-an-assignment\nline two'\n"
            "export ATLAS_HTTP_PORT=15500\nCOMPOSE_PROJECT_NAME=atlas-pv-test\n"
            "DATA_PATH=/data/existing # keep this\nDATA_PATH=/data/last\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(preserved + "ATLAS_TEXT_MODEL=old\nATLAS_USD_TO_EUR=0.9\n")
            retained, values = atlas_setup.read_existing(path)
            self.assertEqual(retained, preserved)
            self.assertEqual(values["ATLAS_HTTP_PORT"], "15500")
            atlas_setup.write_private(path, {"ATLAS_PROVIDER": "hetzner"}, retained)
            self.assertIn(preserved, path.read_text())
            self.assertNotIn("ATLAS_USD_TO_EUR", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_symlink_and_malformed_configuration_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            with self.assertRaises(ValueError):
                atlas_setup.read_existing(path)
            path.write_text("not an assignment\n")
            with self.assertRaises(ValueError):
                atlas_setup.read_existing(path)
            path.unlink()
            path.symlink_to(Path(directory) / "missing")
            with self.assertRaises(ValueError):
                atlas_setup.read_existing(path)

    def test_atomic_replace_failure_leaves_old_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("POSTGRES_PASSWORD=retained\n")
            with (
                patch("scripts.atlas_setup.os.replace", side_effect=OSError),
                self.assertRaises(OSError),
            ):
                atlas_setup.write_private(path, {"ATLAS_PROVIDER": "hetzner"})
            self.assertEqual(path.read_text(), "POSTGRES_PASSWORD=retained\n")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_scripted_stream_two_switches_and_failure_preserve_installation(
        self,
    ) -> None:
        calls: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                calls.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"data": [{"id": "scripted"}]}).encode())

            def do_POST(self) -> None:
                calls.append(self.path)
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if not body.get("stream"):
                    self.send_error(400)
                    return
                self.send_response(200)
                self.end_headers()
                frame = {
                    "id": "scripted-completion",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 1,
                        "total_tokens": 5,
                    },
                }
                self.wfile.write(
                    ("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode()
                )

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cwd = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env = root / ".env"
                original = "POSTGRES_PASSWORD=original\nWEBUI_SECRET_KEY=original\nSEARXNG_SECRET=original\nATLAS_HTTP_PORT=15500\nCUSTOM=keep\n"
                env.write_text(original)
                key = root / "key"
                key.write_text("scripted-test-credential")
                output = io.StringIO()
                with (
                    patch.object(atlas_setup, "ROOT", root),
                    patch("scripts.atlas_setup.subprocess.run") as run,
                    patch.object(
                        atlas_setup,
                        "validate_endpoint",
                        return_value=f"http://127.0.0.1:{server.server_port}",
                    ),
                    redirect_stdout(output),
                ):
                    for provider in ("hetzner", "mistral"):
                        with (
                            patch(
                                "sys.argv",
                                [
                                    "atlas",
                                    "setup",
                                    "--reconfigure",
                                    "--provider",
                                    provider,
                                    "--model",
                                    "scripted",
                                    "--api-key-file",
                                    str(key),
                                    "--yes",
                                ],
                            ),
                            patch.dict(
                                os.environ, {"POSTGRES_PASSWORD": "wrong-export"}
                            ),
                        ):
                            self.assertEqual(atlas_setup.main(), 0)
                        self.assertIn(original, env.read_text())
                        command = run.call_args.args[0]
                        self.assertEqual(
                            command[-3:], ["--no-deps", "--force-recreate", "gateway"]
                        )
                        self.assertNotIn(
                            "POSTGRES_PASSWORD", run.call_args.kwargs["env"]
                        )
                    before = env.read_bytes()
                    with patch(
                        "sys.argv", ["atlas", "setup", "--provider", "hetzner", "--yes"]
                    ):
                        self.assertEqual(atlas_setup.main(), 1)
                    self.assertIn("--reconfigure", output.getvalue())
                    with patch(
                        "sys.argv",
                        [
                            "atlas",
                            "setup",
                            "--reconfigure",
                            "--provider",
                            "hetzner",
                            "--model",
                            "absent",
                            "--api-key-file",
                            str(key),
                            "--yes",
                        ],
                    ):
                        self.assertEqual(atlas_setup.main(), 1)
                    self.assertEqual(before, env.read_bytes())
                    self.assertEqual(calls.count("/chat/completions"), 2)
                    self.assertEqual(calls.count("/models"), 3)
                    self.assertNotIn(key.read_text(), output.getvalue())
        finally:
            os.chdir(cwd)
            server.shutdown()
            server.server_close()
            thread.join()
