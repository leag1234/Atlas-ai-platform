"""K-A administrator CLI over real loopback HTTP; no inference or credentials."""

from contextlib import contextmanager
from collections.abc import Iterator
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import uvicorn

from services.orchestrator.api_key_admin import app
from services.orchestrator.api_keys import configured_store


@contextmanager
def listener() -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        address = sock.getsockname()
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", access_log=False)
        )
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.02)
            if not server.started:
                raise RuntimeError("administrator_listener_failed")
            yield f"http://127.0.0.1:{address[1]}"
        finally:
            server.should_exit = True
            thread.join(5)


class AdminHTTPTests(unittest.TestCase):
    def test_cli_secret_file_listing_revocation_and_no_overwrite(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "ATLAS_API_KEYS_DB": directory + "/keys.sqlite",
                    "ATLAS_API_KEY_PEPPER": secrets.token_urlsafe(48),
                    "ATLAS_API_KEY_ADMIN_TOKEN": secrets.token_urlsafe(48),
                },
            ),
            listener() as url,
        ):
            command = [sys.executable, "-m", "services.orchestrator.api_key_admin"]
            common = ["--admin-url", url]
            keyfile = Path(directory) / "tester.key"
            args = [
                "create",
                *common,
                "--org-id",
                "test-org",
                "--member-id",
                "test-member",
                "--subject",
                "test-subject",
                "--budget-user",
                "webui-test-id",
                "--name",
                "external",
                "--scope",
                "assistant",
                "--own-budget",
                "--key-file",
                str(keyfile),
            ]
            created = subprocess.run(command + args, capture_output=True, text=True)
            self.assertEqual(created.returncode, 0, created.stderr)
            secret = keyfile.read_text().strip()
            self.assertEqual(keyfile.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(secret, created.stdout + created.stderr)
            metadata = json.loads(created.stdout)
            self.assertEqual(
                metadata["expires_at"] - metadata["created_at"], 90 * 86400
            )
            self.assertIsNone(metadata["daily_requests"])
            self.assertIsNone(metadata["daily_micro_eur"])
            self.assertEqual(metadata["weekly_micro_eur"], 100000000)
            self.assertTrue(metadata["own_budget"])
            listed = subprocess.run(
                command + ["list", *common], capture_output=True, text=True
            )
            self.assertEqual(listed.returncode, 0)
            self.assertNotIn(secret, listed.stdout + listed.stderr)
            repeated = subprocess.run(command + args, capture_output=True, text=True)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertNotIn(secret, repeated.stdout + repeated.stderr)
            self.assertEqual(len(configured_store().list_keys()), 1)
            revoked = subprocess.run(
                command + ["revoke", *common, "--id", metadata["id"]],
                capture_output=True,
                text=True,
            )
            self.assertEqual(revoked.returncode, 0)
            with self.assertRaises(PermissionError):
                configured_store().resolve(secret)
            with httpx.Client() as client:
                response = client.get(
                    url + "/keys", headers={"Authorization": "Bearer " + secret}
                )
                self.assertEqual(response.status_code, 403)
