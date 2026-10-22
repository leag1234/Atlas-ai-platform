import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from server import running_server

ROOT = Path(__file__).resolve().parents[1]


class HealthTests(unittest.TestCase):
    def test_contract_and_healthcheck(self) -> None:
        contract = json.loads((ROOT / "contracts/edge-bff.openapi.json").read_text())
        schema = contract["paths"]["/health"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        with running_server() as url:
            with urlopen(url, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    response.headers.get_content_type(), "application/json"
                )
                body = json.load(response)
            self.assertEqual(set(body), set(schema["required"]))
            self.assertEqual(body, {"status": "ok"})
            self.assertIn(body["status"], schema["properties"]["status"]["enum"])
            subprocess.run(
                ["bash", "services/edge-bff/healthcheck.sh"],
                cwd=ROOT,
                env={**os.environ, "BFF_URL": url},
                check=True,
            )
        with self.assertRaises(OSError):
            urlopen(url, timeout=1)

    def test_unknown_paths(self) -> None:
        with running_server() as url:
            for path in ("/", "/missing", "/health/extra", "/%C3%A9", "/" + "a" * 8192):
                with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                    urlopen(url.removesuffix("/health") + path, timeout=5)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()


class ToolTests(unittest.TestCase):
    def test_representative_smoke_and_invalid_mode(self) -> None:
        smoke = subprocess.run(
            ["bash", "scripts/eval.sh", "--smoke"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        report = json.loads(smoke.stdout)
        self.assertEqual(report["executed_cases"], 15)
        self.assertGreaterEqual(len(report["languages"]), 3)
        self.assertEqual(report["live_calls"], 0)
        for mode in ("--invalid",):
            result = subprocess.run(
                ["bash", "scripts/eval.sh", mode], cwd=ROOT, capture_output=True
            )
            self.assertNotEqual(result.returncode, 0)

    def test_quality_tools_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "python3"
            executable.write_text("#!/bin/sh\nexit 23\n")
            executable.chmod(0o755)
            for script in ("lint", "typecheck", "test"):
                result = subprocess.run(
                    ["bash", f"scripts/{script}.sh"],
                    cwd=ROOT,
                    env={**os.environ, "PATH": directory + ":" + os.environ["PATH"]},
                )
                self.assertEqual(result.returncode, 23)
