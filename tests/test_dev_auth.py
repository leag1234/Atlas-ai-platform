"""REQ-DEV-001/002: real SQLite credentials, HTTP identity and durable quotas."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from services.orchestrator.dev_auth import Store, provision
from services.orchestrator.devapi import app


class DevAuthTests(unittest.TestCase):
    def test_credentials_are_private_revocable_and_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "usage.sqlite")
            keyfile = root / "key"
            provision(store, "alice", keyfile, 4, 50000)
            key = keyfile.read_text().strip()
            self.assertGreaterEqual(len(key), 43)
            self.assertEqual(keyfile.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(key.encode(), store.path.read_bytes())
            self.assertEqual(store.authenticate(key), "alice")
            with self.assertRaises(FileExistsError):
                provision(store, "bob", keyfile, 4, 50000)
            self.assertEqual(keyfile.read_text().strip(), key)
            store.revoke("alice")
            with self.assertRaises(PermissionError):
                store.authenticate(key)
            with self.assertRaises(ValueError):
                provision(store, "../invalid", root / "bad", 4, 50000)
            self.assertFalse((root / "bad").exists())
            unsafe = root / "unsafe"
            unsafe.touch(mode=0o644)
            with self.assertRaises(PermissionError):
                Store(unsafe)
            link = root / "link"
            link.symlink_to(store.path)
            with self.assertRaises(OSError):
                Store(link)

    def test_http_authentication_and_identity_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "usage.sqlite")
            for developer in ("alice", "bob"):
                provision(store, developer, root / developer, 4, 50000)
            store.reserve("alice", 1000)
            alice = (root / "alice").read_text().strip()
            bob = (root / "bob").read_text().strip()
            with patch.dict(os.environ, {"ATLAS_DEVAPI_DB": str(store.path)}):
                with TestClient(app) as client:
                    for headers in (
                        {},
                        {"Authorization": "Bearer invalid"},
                        {"Authorization": "Basic " + alice},
                        {"Authorization": "Bearer " + alice, "x-api-key": bob},
                    ):
                        for route in ("/v1/models", "/v1/usage", "/openapi.json"):
                            response = client.get(route, headers=headers)
                            self.assertEqual(response.status_code, 401)
                            self.assertNotIn(alice, response.text)
                            self.assertNotIn(bob, response.text)
                    for key, developer in ((alice, "alice"), (bob, "bob")):
                        response = client.get(
                            "/v1/usage?developer=alice",
                            headers={"x-api-key": key, "x-user-id": "alice"},
                        )
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["developer"], developer)
                        self.assertEqual(
                            response.json()["requests"], int(developer == "alice")
                        )
                    response = client.get(
                        "/v1/models", headers={"Authorization": "Bearer " + alice}
                    )
                    self.assertEqual(response.json()["data"][0]["id"], "atlas-code")
                    store.revoke("alice")
                    self.assertEqual(
                        client.get(
                            "/v1/models", headers={"x-api-key": alice}
                        ).status_code,
                        401,
                    )
