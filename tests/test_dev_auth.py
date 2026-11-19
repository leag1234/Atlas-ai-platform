"""REQ-DEV-001/002: real SQLite credentials, HTTP identity and durable quotas."""

import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from services.orchestrator.dev_auth import QuotaError, Store, provision
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

    def test_request_cap_is_optional_and_old_stores_are_upgraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "usage.sqlite"
            # A store created before 2026-11-19 required a request-count cap.
            path.touch(mode=0o600)
            with sqlite3.connect(path) as db:
                db.executescript(
                    "CREATE TABLE developers (id TEXT PRIMARY KEY, key_hash TEXT NOT NULL UNIQUE, "
                    "enabled INTEGER NOT NULL CHECK (enabled IN (0,1)), "
                    "daily_requests INTEGER NOT NULL CHECK (daily_requests > 0), "
                    "daily_micro_eur INTEGER NOT NULL CHECK (daily_micro_eur > 0));"
                    "CREATE TABLE requests (id TEXT PRIMARY KEY, developer TEXT NOT NULL REFERENCES developers(id), "
                    "day TEXT NOT NULL, reserved INTEGER NOT NULL CHECK (reserved BETWEEN 1 AND 50000), "
                    "charged INTEGER NOT NULL CHECK (charged >= 0), input_tokens INTEGER, output_tokens INTEGER, "
                    "state TEXT NOT NULL CHECK (state IN ('reserved','complete','unknown')));"
                    "INSERT INTO developers VALUES ('old','hash',1,1,50000);"
                    "INSERT INTO requests VALUES ('r1','old','2026-01-01',10,10,NULL,NULL,'complete');"
                )
            store = Store(path)
            with store.connection() as db:
                columns = {
                    row["name"]: row["notnull"]
                    for row in db.execute("PRAGMA table_info(developers)")
                }
                self.assertEqual(columns["daily_requests"], 0)
                self.assertEqual(
                    dict(db.execute("SELECT * FROM developers").fetchone())[
                        "daily_requests"
                    ],
                    1,
                )
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertIn(
                    "REFERENCES developers(id)",
                    db.execute(
                        "SELECT sql FROM sqlite_master WHERE name='requests'"
                    ).fetchone()[0],
                )
            # The explicit cap of the old key still applies.
            store.reserve("old", 10)
            with self.assertRaises(QuotaError):
                store.reserve("old", 10)
            # A new key without a request cap is refused only by its spending cap.
            provision(store, "alice", root / "alice", None, 50000)
            for _ in range(30):
                store.finish(store.reserve("alice", 1000), 1000)
            with self.assertRaises(QuotaError):
                store.reserve("alice", 30000)
            with self.assertRaises(ValueError):
                provision(store, "bob", root / "bob", 0, 50000)
            self.assertEqual(Store(path).usage("alice")["requests"], 30)
