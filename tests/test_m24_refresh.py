"""Recording preflight cannot spend provider credit on broken file dependencies."""

import base64
from contextlib import contextmanager
from collections.abc import Iterator
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import m24_capture
import m24_dependencies as dependencies
import m24_refresh as refresh
from services.orchestrator.terminal_client import TerminalClient
from services.orchestrator.documents import MAX_BYTES
from services.orchestrator import terminal_stack
from m24_artifacts import validate


class PreflightTests(unittest.TestCase):
    def test_each_failure_blocks_provider_and_success_reopens_real_workbook(
        self,
    ) -> None:
        for failure in (
            "credentials",
            "initialize",
            "upload",
            "execute",
            "publish",
            "authentication",
            "unreachable_ui",
            "download",
            "empty",
            "reopen",
            "cleanup",
            None,
        ):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                directory = Path(root)
                deleted = []
                calls = []

                class Terminal(TerminalClient):
                    def __init__(self, request_id: str) -> None:
                        super().__init__(request_id)
                        self.root = str(directory / request_id)

                    async def initialize(self) -> None:
                        Path(self.root).mkdir()
                        if failure == "initialize":
                            raise OSError("unreachable terminal")

                    async def upload_generated(
                        self,
                        filename: str,
                        content: bytes,
                        timeout: float,
                        *,
                        max_bytes: int = MAX_BYTES,
                    ) -> str:
                        if failure == "upload":
                            raise ValueError("upload integrity")
                        path = Path(self.root) / filename
                        path.write_bytes(content)
                        return str(path)

                    async def execute(
                        self, command: str, timeout: float
                    ) -> dict[str, object]:
                        cleanup = "shutil.rmtree" in command
                        if cleanup:
                            deleted.append(self.root)
                        if failure == ("cleanup" if cleanup else "execute"):
                            return {"exit_code": 1}
                        result = subprocess.run(
                            command, shell=True, capture_output=True, timeout=timeout
                        )
                        return {"exit_code": result.returncode}

                    async def publish(
                        self, path: str, strategy: str, timeout: float
                    ) -> dict[str, object]:
                        if failure == "publish":
                            return {"error": "output_file_missing_or_invalid"}
                        return await super().publish(path, strategy, timeout)

                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, format: str, *args: object) -> None:
                        return

                    def do_POST(self) -> None:
                        calls.append("signin")
                        self.send_response(401 if failure == "authentication" else 200)
                        self.end_headers()
                        self.wfile.write(
                            json.dumps({"token": "preflight-session"}).encode()
                        )

                    def do_GET(self) -> None:
                        calls.append("download")
                        if (
                            self.headers.get("Authorization")
                            != "Bearer preflight-session"
                        ):
                            self.send_response(401)
                            self.end_headers()
                            return
                        self.send_response(503 if failure == "download" else 200)
                        self.end_headers()
                        if failure == "empty":
                            return
                        content = next(directory.glob("*/preflight.xlsx")).read_bytes()
                        self.wfile.write(
                            b"not a workbook" if failure == "reopen" else content
                        )

                @contextmanager
                def preflight_only(attempt: Path) -> Iterator[None]:
                    dependencies.preflight()
                    yield

                with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
                    worker = threading.Thread(target=server.serve_forever, daemon=True)
                    worker.start()
                    try:
                        with (
                            patch.dict(
                                os.environ,
                                {
                                    "M24_CASE": "J44",
                                    "ATLAS_TERMINAL_ENABLED": "1",
                                    "OPEN_TERMINAL_API_KEY": ""
                                    if failure == "credentials"
                                    else "preflight-key",
                                },
                            ),
                            patch.object(dependencies, "TerminalClient", Terminal),
                            patch.object(
                                m24_capture,
                                "atlas_ports",
                                return_value=SimpleNamespace(
                                    url=lambda _: f"http://127.0.0.1:{server.server_port}"
                                ),
                            ),
                            patch.object(refresh, "file_dependencies", preflight_only),
                            patch.object(refresh, "CPUModels"),
                            patch.object(
                                dependencies,
                                "download_files",
                                wraps=m24_capture.download_files,
                                side_effect=OSError
                                if failure == "unreachable_ui"
                                else None,
                            ),
                            patch.object(
                                refresh,
                                "serve",
                                side_effect=RuntimeError("provider_reached"),
                            ) as provider,
                        ):
                            stage = (
                                "download"
                                if failure
                                in {"authentication", "empty", "unreachable_ui"}
                                else failure
                            )
                            expected = (
                                "m24_dependency_" + stage
                                if stage
                                else "provider_reached"
                            )
                            with self.assertRaisesRegex(RuntimeError, expected):
                                refresh.main()
                            self.assertEqual(provider.call_count, 0 if failure else 1)
                        self.assertEqual(
                            len(deleted), 0 if failure == "credentials" else 1
                        )
                        self.assertTrue(
                            all(Path(p).parent == directory for p in deleted)
                        )
                        if failure not in {"credentials", "cleanup"}:
                            self.assertFalse(any(directory.iterdir()))
                        if failure is None:
                            self.assertEqual(calls, ["signin", "download"])
                    finally:
                        server.shutdown()
                        worker.join()

    def test_existing_resources_and_shared_offsets_are_never_touched(self) -> None:
        for base, error in (
            ("0", "dedicated_port"),
            ("100", "dedicated_port"),
            ("24000", "already_exists"),
        ):
            with (
                self.subTest(base=base),
                tempfile.TemporaryDirectory() as root,
                patch.dict(os.environ, {"ATLAS_PORT_BASE": base}),
                patch.object(dependencies, "exists", return_value=True),
                patch.object(terminal_stack, "terminal") as terminal,
                patch.object(dependencies, "docker") as docker,
            ):
                with self.assertRaisesRegex(dependencies.DependencyError, error):
                    with dependencies.file_dependencies(Path(root)):
                        self.fail("provider must not be reached")
                terminal.assert_not_called()
                docker.assert_not_called()

    def test_owned_stack_is_cleaned_on_partial_start_preflight_and_body_failure(
        self,
    ) -> None:
        for failure in ("startup", "preflight", "capture", None):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as root,
                patch.dict(
                    os.environ,
                    {"ATLAS_PORT_BASE": "24000", "OPEN_TERMINAL_API_KEY": "inherited"},
                ),
                patch.object(
                    dependencies, "exists", side_effect=[False] * 4 + [True] * 4
                ),
                patch.object(dependencies, "docker") as docker,
                patch.object(
                    dependencies,
                    "preflight",
                    side_effect=dependencies.DependencyError("preflight")
                    if failure == "preflight"
                    else None,
                ),
            ):

                @contextmanager
                def terminal(name: str, binding: str) -> Iterator[None]:
                    self.assertTrue(name.startswith("atlas-refresh-terminal-"))
                    self.assertEqual(binding, "127.0.0.1:32000")
                    self.assertNotEqual(
                        os.environ["OPEN_TERMINAL_API_KEY"], "inherited"
                    )
                    yield

                @contextmanager
                def ui(name: str, persistent: bool) -> Iterator[None]:
                    self.assertFalse(persistent)
                    if failure == "startup":
                        raise OSError
                    yield

                def run() -> None:
                    with dependencies.file_dependencies(Path(root)):
                        if failure == "capture":
                            raise ValueError("capture_failed")

                with (
                    patch.object(terminal_stack, "terminal", terminal),
                    patch.object(terminal_stack, "user_interface", ui),
                ):
                    if failure:
                        with self.assertRaisesRegex(
                            Exception, "ui_start|preflight|capture_failed"
                        ):
                            run()
                    else:
                        run()
                self.assertEqual(docker.call_count, 4)
                self.assertTrue(
                    all(
                        call.args[-1].endswith("-24000")
                        for call in docker.call_args_list
                    )
                )
                self.assertEqual(os.environ["OPEN_TERMINAL_API_KEY"], "inherited")


class PromotionTests(unittest.TestCase):
    def test_capture_and_validation_failures_preserve_canonical_bytes(self) -> None:
        original = Path("tests/cassettes/m24.json.gz").read_bytes()
        case = json.loads(gzip.decompress(original))["J44"]
        for failure in ("download", "validation", None):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                folder = Path(root)
                archive = folder / "canonical.json.gz"
                archive.write_bytes(original)
                request_path, response_path = (
                    folder / "request.json",
                    folder / "response.json",
                )
                request_path.write_text(json.dumps(case["request"]))
                response_path.write_text(json.dumps(case["response"]))
                validator = Mock(wraps=validate)
                if failure == "validation":
                    validator.side_effect = AssertionError("rejected_artifact")
                with (
                    patch.object(
                        m24_capture,
                        "download_files",
                        return_value=case["artifacts"],
                        side_effect=OSError if failure == "download" else None,
                    ),
                    patch.object(refresh, "validate", validator),
                ):
                    if failure:
                        with self.assertRaises((OSError, AssertionError)):
                            refresh.promote(
                                archive,
                                folder,
                                "J44",
                                request_path,
                                response_path,
                                case["exchanges"],
                            )
                        self.assertEqual(archive.read_bytes(), original)
                    else:
                        refresh.promote(
                            archive,
                            folder,
                            "J44",
                            request_path,
                            response_path,
                            case["exchanges"],
                        )
                        validator.assert_called_once()
                        refreshed = json.loads(gzip.decompress(archive.read_bytes()))
                        self.assertEqual(refreshed["J44"], case)
                        self.assertTrue(
                            base64.b64decode(refreshed["J44"]["artifacts"][0]["bytes"])
                        )
                self.assertTrue((folder / "J44.json.gz").exists())
