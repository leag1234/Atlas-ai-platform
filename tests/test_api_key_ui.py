"""Browser K-A authorization through a real test-only WebUI session endpoint."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.orchestrator.api_key_ui import router
from services.orchestrator.api_keys import configured_store


class KeyUiAssetTests(unittest.TestCase):
    def test_revocation_row_has_quiet_action_and_localized_mobile_details(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "services/orchestrator/api-key-ui.js").read_text()
        stylesheet = (root / "services/orchestrator/api-key-ui.css").read_text()

        self.assertIn("atlas-key-revoke", script)
        self.assertIn("weeklyCap", script)
        self.assertNotIn("status.textContent = t(48)", script)
        self.assertIn("dateCell(54,key.created_at)", script)
        self.assertIn("dateCell(55,key.last_used_at)", script)
        self.assertIn("dateCell(56,key.expires_at)", script)
        self.assertIn("'Cr\u00e9\u00e9e','Derni\u00e8re utilisation','Expire'", script)
        for labels in (
            "'Created','Last used','Expires'",
            "'Erstellt','Zuletzt verwendet','L\u00e4uft ab'",
            "'Creada','\u00daltimo uso','Caduca'",
            "'Creata','Ultimo utilizzo','Scade'",
        ):
            self.assertIn(labels, script)
        self.assertIn("sort((left, right) => Number(Boolean(left.revoked_at))", script)
        self.assertIn(".atlas-key-revoke", stylesheet)
        self.assertIn(".atlas-key-usage { display:block", stylesheet)
        self.assertNotIn("td:nth-child(7) button { width:100%; }", stylesheet)


@contextmanager
def key_environment() -> Iterator[None]:
    class Session(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            users = {
                name: dict(
                    id=name,
                    email=name + "@example.invalid",
                    role="admin" if name == "admin" else "user",
                )
                for name in ("alice", "bob", "admin")
            }
            user = users.get(token)
            if self.path.startswith("/api/v1/users/") and token == "directory-test":
                data: object = dict(users=list(users.values()), total=3)
                status = 200
            else:
                data, status = user or {}, 200 if user else 401
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "ATLAS_API_KEYS_DB": str(Path(directory) / "keys.sqlite"),
                    "ATLAS_API_KEY_PEPPER": secrets.token_urlsafe(48),
                    "ATLAS_API_KEY_POLICY": "self_service",
                    "ATLAS_API_KEY_ORG_ID": "stable",
                    "ATLAS_PROJECT_ORIGIN": "http://testserver",
                    "ATLAS_WEBUI_URL": f"http://127.0.0.1:{server.server_port}",
                    "ATLAS_WEBUI_ADMIN_TOKEN": "directory-test",
                    "ATLAS_PROJECT_MODE": "0",
                },
            ),
        ):
            yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def client(user: str = "alice") -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(
        app, cookies={"token": user}, headers={"Origin": "http://testserver"}
    )


class BrowserKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = key_environment()
        self.environment.__enter__()
        self.addCleanup(self.environment.__exit__, None, None, None)
        self.api = client()
        self.addCleanup(self.api.close)
        self.admin = client("admin")
        self.addCleanup(self.admin.close)

    def test_own_keys_secret_once_usage_and_revocation(self) -> None:
        reply = self.api.post("/api/atlas/keys", json={"name": "My integration"})
        self.assertEqual(reply.status_code, 201)
        value = reply.json()
        secret = value.pop("key")
        self.assertEqual(value["budget_user"], "alice")
        self.assertEqual(value["subject"], "alice@example.invalid")
        self.assertEqual(value["scopes"], ["assistant"])
        self.assertEqual(value["daily_requests"], 200)
        self.assertEqual(value["daily_micro_eur"], 2000000)
        self.assertEqual(value["expires_at"] - value["created_at"], 90 * 86400)
        store = configured_store()
        store.resolve(secret)
        reservation = store.reserve(value["id"], 10000)
        store.finish(reservation, 9000)
        listing = self.api.get("/api/atlas/keys")
        self.assertEqual(listing.status_code, 200)
        self.assertNotIn(secret, listing.text)
        self.assertNotIn('"hash"', listing.text)
        self.assertIn("no-store", listing.headers["cache-control"])
        row = listing.json()["data"][0]
        self.assertEqual(row["usage"]["requests"], 1)
        self.assertEqual(row["usage"]["charged_micro_eur"], 9000)
        self.assertIsNotNone(row["last_used_at"])
        self.assertEqual(
            self.api.get("/api/atlas/keys/" + value["id"]).status_code, 405
        )
        self.assertEqual(
            self.api.delete("/api/atlas/keys/" + value["id"]).status_code, 204
        )
        with self.assertRaises(PermissionError):
            store.resolve(secret)
        with store.connection() as db:
            audits = str(
                [dict(row) for row in db.execute("SELECT * FROM api_key_audit")]
            )
        self.assertNotIn(secret, audits)
        for event in ("create", "list", "revoke", "management_revoke"):
            self.assertIn(event, audits)

    def test_cross_user_and_cross_org_isolation(self) -> None:
        with client("bob") as bob:
            key = bob.post("/api/atlas/keys", json={"name": "Bob"}).json()
        self.assertEqual(self.api.get("/api/atlas/keys").json()["data"], [])
        self.assertEqual(
            self.api.delete("/api/atlas/keys/" + key["id"]).status_code, 404
        )
        self.assertEqual(self.api.get("/api/atlas/keys?all=true").status_code, 403)
        foreign, _ = configured_store().create(
            org_id="elsewhere",
            member_id="alice",
            subject="alice@example.invalid",
            budget_user="alice",
            name="Foreign",
            scopes=["assistant"],
            created_by="admin",
        )
        self.assertEqual(
            self.admin.delete("/api/atlas/keys/" + foreign["id"]).status_code, 404
        )
        self.assertEqual(
            len(self.admin.get("/api/atlas/keys?all=true").json()["data"]), 1
        )

    def test_policy_modes_and_admin_create_list_revoke(self) -> None:
        policy = self.admin.get("/api/atlas/keys").json()["policy"]
        for mode in ("admin_only", "off", "self_service"):
            policy["mode"] = mode
            self.assertEqual(
                self.admin.put("/api/atlas/keys/policy", json=policy).status_code, 200
            )
            self.assertEqual(
                self.api.put("/api/atlas/keys/policy", json=policy).status_code, 403
            )
            self.assertEqual(
                self.api.get("/api/atlas/keys").status_code,
                403 if mode == "off" else 200,
            )
            own = self.api.post("/api/atlas/keys", json={"name": "Own"})
            self.assertEqual(own.status_code, 201 if mode == "self_service" else 403)
            created = self.admin.post(
                "/api/atlas/keys",
                json={"name": "For Bob", "login": "bob@example.invalid"},
            )
            self.assertEqual(created.status_code, 201)
            key = created.json()
            self.assertEqual(key["budget_user"], "bob")
            if mode != "self_service":
                self.assertEqual(
                    self.api.delete("/api/atlas/keys/" + key["id"]).status_code, 403
                )
            self.assertEqual(
                self.admin.delete("/api/atlas/keys/" + key["id"]).status_code, 204
            )

    def test_cap_is_atomic_and_revocation_frees_capacity(self) -> None:
        def create(_: int) -> int:
            with client() as api:
                return int(
                    api.post("/api/atlas/keys", json={"name": "Concurrent"}).status_code
                )

        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(create, range(8)))
        self.assertEqual(codes.count(201), 5)
        self.assertEqual(codes.count(400), 3)
        key = self.api.get("/api/atlas/keys").json()["data"][0]
        self.api.delete("/api/atlas/keys/" + key["id"])
        self.assertEqual(
            self.api.post("/api/atlas/keys", json={"name": "Replacement"}).status_code,
            201,
        )

    def test_quota_expiry_and_untrusted_fields(self) -> None:
        policy = self.admin.get("/api/atlas/keys").json()["policy"]
        policy.update(max_lifetime_days=30, daily_requests=7, daily_micro_eur=500000)
        self.admin.put("/api/atlas/keys/policy", json=policy)
        self.assertEqual(
            self.api.post("/api/atlas/keys", json={"name": "Too long"}).json()["error"],
            "expiry_exceeds_policy",
        )
        value = self.api.post(
            "/api/atlas/keys", json={"name": "Short", "lifetime_days": 30}
        ).json()
        self.assertEqual(value["daily_requests"], 7)
        self.assertEqual(value["daily_micro_eur"], 500000)
        for data in (
            {"budget_user": "bob"},
            {"org_id": "elsewhere"},
            {"daily_requests": 999},
            {"lifetime_days": 31},
            {"scopes": ["project"]},
        ):
            self.assertEqual(
                self.api.post(
                    "/api/atlas/keys", json={"name": "Invalid", **data}
                ).status_code,
                422,
            )
        self.assertEqual(
            self.api.post(
                "/api/atlas/keys",
                json={"name": "Invalid", "login": "bob@example.invalid"},
            ).status_code,
            403,
        )

    def test_authentication_csrf_and_admin_spoofing(self) -> None:
        with client("invalid") as invalid:
            reply = invalid.get(
                "/api/atlas/keys",
                headers={
                    "X-Atlas-User-Role": "admin",
                    "X-OpenWebUI-User-Id": "admin",
                    "Authorization": "Bearer invalid",
                },
            )
            self.assertEqual(reply.status_code, 401)
        for headers in (
            {"Origin": "https://attacker.invalid"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            self.assertEqual(
                self.api.post(
                    "/api/atlas/keys", json={"name": "CSRF"}, headers=headers
                ).status_code,
                403,
            )
        self.api.headers.pop("Origin")
        self.assertEqual(
            self.api.post("/api/atlas/keys", json={"name": "No origin"}).status_code,
            403,
        )
        self.assertEqual(
            self.api.get(
                "/api/atlas/keys?all=true", headers={"X-Atlas-User-Role": "admin"}
            ).status_code,
            403,
        )

    def test_public_edge_off_policy_and_router_reachable(self) -> None:
        from services.orchestrator.chat_api import app

        key = self.api.post("/api/atlas/keys", json={"name": "Edge"}).json()
        store = configured_store()
        store.set_policy("stable", {**store.policy("stable"), "mode": "off"}, "admin")
        with TestClient(app, cookies={"token": "admin"}) as app_client:
            self.assertEqual(app_client.get("/api/atlas/keys").status_code, 200)
            app_client.cookies.clear()
            self.assertEqual(
                app_client.get(
                    "/api/atlas/v1/models",
                    headers={"Authorization": "Bearer " + key["key"]},
                ).status_code,
                401,
            )
