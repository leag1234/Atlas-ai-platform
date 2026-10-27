"""REQ-KEY-01..06: credentials, atomic quotas, membership and browser separation."""

import concurrent.futures
from collections.abc import AsyncIterator
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from typing import Any

from fastapi.testclient import TestClient
from packages.profiles import PROFILES
from services.orchestrator.api_keys import Store, KeyQuotaError
from services.orchestrator.api_key_edge import key_call, ModelAccounting
from services.orchestrator.api_key_admin import app as admin_app
from services.orchestrator.chat_api import app
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.daily_budget import DailyLedger
from services.orchestrator.dev_auth import Store as DeveloperStore, provision
from services.orchestrator.devapi import app as dev_app
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context
from services.orchestrator.tools import declarations


class KeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = [1800000000.0]
        self.store = Store(
            self.root / "keys.sqlite", "test-pepper-" * 4, lambda: self.now[0]
        )
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_API_KEYS_DB": str(self.store.path),
                    "ATLAS_API_KEY_PEPPER": "test-pepper-" * 4,
                    "ATLAS_API_KEY_ADMIN_TOKEN": "test-admin-" * 4,
                    "ATLAS_DATA_DIR": str(self.root),
                    "ATLAS_INTERACTION_DIR": str(self.root / "chat-log"),
                    "ATLAS_USER_DAILY_EUR": "10",
                    "ATLAS_GLOBAL_DAILY_EUR": "20",
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                },
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=self.store,
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_admin.configured_store",
                return_value=self.store,
            )
        )

    def create(self, **overrides: Any) -> tuple[dict[str, Any], str]:
        return self.store.create(
            **(
                dict(
                    org_id="test-org",
                    member_id="test-member",
                    subject="tester@example.invalid",
                    budget_user="webui-test-id",
                    name="integration",
                    scopes=["assistant"],
                    created_by="operator",
                )
                | overrides
            )
        )

    def test_hash_expiry_revocation_metadata_and_permissions(self) -> None:
        row, key = self.create()
        self.assertRegex(row["prefix"], r"^atl_[0-9a-f]{8}$")
        self.assertTrue(key.startswith(row["prefix"] + "_"))
        self.assertNotIn(key.encode(), self.store.path.read_bytes())
        self.assertNotIn("hash", row)
        self.assertEqual(row["expires_at"] - row["created_at"], 90 * 86400)
        principal = self.store.resolve(key, "192.0.2.123")
        self.assertEqual(principal.budget_user, "webui-test-id")
        updated = self.store.get(principal.id)
        self.assertEqual(updated["last_used_ip_prefix"], "192.0.2.0/24")
        self.assertEqual(updated["last_used_at"], int(self.now[0]))
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        wrong = Store(self.store.path, "other-test-pepper-" * 3)
        with self.assertRaises(PermissionError):
            wrong.resolve(key)
        for kwargs in (
            {"lifetime_days": 0},
            {"lifetime_days": 366},
            {"scopes": ["connectors"]},
            {"daily_requests": 0},
        ):
            with self.assertRaises(ValueError):
                self.create(**kwargs)
        self.now[0] = float(row["expires_at"])
        with self.assertRaises(PermissionError):
            self.store.resolve(key)
        self.now[0] -= 1
        self.store.revoke(principal.id)
        with self.assertRaises(PermissionError):
            self.store.resolve(key)
        with self.store.connection() as db:
            audit = [dict(r) for r in db.execute("SELECT * FROM api_key_audit")]
        self.assertTrue(
            {"create", "revoke", "failure"}.issubset({r["event"] for r in audit})
        )
        self.assertNotIn(key, json.dumps(audit))

    def test_atomic_request_and_money_quotas_unknown_and_next_day(self) -> None:
        row, _ = self.create(daily_requests=1, daily_micro_eur=100000)

        def reserve(_: int) -> str:
            try:
                return self.store.reserve(row["id"], 100000)
            except KeyQuotaError:
                return "rejected"

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(reserve, range(6)))
        self.assertEqual(results.count("rejected"), 5)
        request_id = next(r for r in results if r != "rejected")
        self.store.finish(request_id, None)
        self.assertEqual(self.store.usage(row["id"])["unknown_micro_eur"], 100000)
        with self.assertRaises(ValueError):
            self.store.finish(request_id, 0)
        self.now[0] += 86400
        request_id = self.store.reserve(row["id"], 100000)
        self.store.finish(request_id, 500)
        with self.store.connection() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM api_key_daily_summary").fetchone()[0],
                2,
            )
        row, _ = self.create(daily_micro_eur=99999)
        with self.assertRaises(KeyQuotaError) as caught:
            self.store.reserve(row["id"], 100000)
        self.assertEqual(caught.exception.values["daily_micro_eur"], 99999)

    def test_legacy_import_and_both_entries_share_revoke_and_usage(self) -> None:
        legacy = DeveloperStore(self.root / "legacy.sqlite")
        provision(legacy, "service-test", self.root / "legacy.key", 3, 50000)
        secret = (self.root / "legacy.key").read_text().strip()
        legacy.finish(legacy.reserve("service-test", 1000), 50)
        row = self.store.import_developer(
            legacy,
            "service-test",
            org_id="test-org",
            member_id="test-member",
            subject="service-test",
            budget_user="service-budget",
            created_by="operator",
        )
        self.assertTrue(row["rotation_required"])
        self.assertEqual(row["scopes"], ["model"])
        principal = self.store.resolve(secret)
        accounting = ModelAccounting(self.store, principal)
        accounting.finish(accounting.reserve(row["id"], 1000), 100)
        self.assertEqual(self.store.get(row["id"])["daily_requests"], 3)
        with (
            patch.dict(os.environ, {"ATLAS_DEVAPI_DB": str(legacy.path)}),
            TestClient(dev_app) as client,
            patch(
                "services.orchestrator.api_keys.configured_store",
                return_value=self.store,
            ),
        ):
            headers = {"Authorization": "Bearer " + secret}
            reply = client.get("/v1/usage", headers=headers)
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.json()["key_id"], row["id"])
            self.store.revoke(row["id"])
            self.assertEqual(client.get("/v1/models", headers=headers).status_code, 401)

    def test_admin_requires_loopback_token_and_rejects_self_service(self) -> None:
        headers = {"Authorization": "Bearer " + "test-admin-" * 4}
        with TestClient(admin_app, client=("192.0.2.1", 10000)) as client:
            self.assertEqual(client.get("/keys", headers=headers).status_code, 403)
        with TestClient(admin_app, client=("127.0.0.1", 10000)) as client:
            self.assertEqual(client.get("/keys").status_code, 403)
            for extra in (
                {"Origin": "http://localhost"},
                {"Cookie": "token=x"},
                {"X-Forwarded-For": "127.0.0.1"},
            ):
                self.assertEqual(
                    client.get("/keys", headers=headers | extra).status_code, 403
                )
            reply = client.post(
                "/keys",
                headers=headers,
                json=dict(
                    org_id="test-org",
                    member_id="test-member",
                    subject="tester",
                    budget_user="test-id",
                    name="key",
                    scopes=["assistant"],
                ),
            )
            self.assertEqual(reply.status_code, 201)
            self.assertIn("key", reply.json())
            self.assertNotIn(
                reply.json()["key"], client.get("/keys", headers=headers).text
            )
            self.assertEqual(
                client.delete(
                    "/keys/" + reply.json()["id"], headers=headers
                ).status_code,
                204,
            )

    def test_edge_assistant_streams_scope_origin_identity_and_member_budget(
        self,
    ) -> None:
        row, secret = self.create(daily_requests=2)
        headers = {
            "Authorization": "Bearer " + secret,
            "X-OpenWebUI-User-Id": "forged",
            "X-Atlas-User": "forged",
        }

        async def respond(payload: ChatRequest, item: Interaction) -> None:
            self.assertEqual(payload.model, PROFILES[0])
            self.assertFalse(payload.atlas_ui)
            self.assertEqual(payload.__dict__["daily_user"], "webui-test-id")
            self.assertTrue(key_call.get())
            self.assertNotIn("mcp_call", json.dumps(declarations()))
            item.reponse, item.state, item.cout_eur = "7 times 8 is 56.", "done", 0.001
            sink = sink_context.get()
            if sink:
                await sink({"delta": {"content": item.reponse}})

        body = dict(
            model="atlas",
            atlas_ui=True,
            messages=[dict(role="user", content="Calculate 7 times 8")],
        )
        with (
            TestClient(app) as client,
            patch("services.orchestrator.chat_api.process", side_effect=respond),
            patch.dict(
                os.environ, {"ATLAS_MCP_CONFIG": "unused", "ATLAS_PROJECT_MODE": "1"}
            ),
            patch(
                "services.orchestrator.api_key_edge.check_member",
                return_value="broker-token",
            ) as broker,
        ):
            for extra in ({"Origin": "http://testserver"}, {"Cookie": "token=value"}):
                self.assertEqual(
                    client.get(
                        "/api/atlas/v1/models", headers=headers | extra
                    ).status_code,
                    401,
                )
            self.assertEqual(
                client.post(
                    "/v1/chat/completions", headers=headers, json=body
                ).status_code,
                403,
            )
            reply = client.get("/api/atlas/v1/models", headers=headers)
            self.assertEqual(reply.status_code, 200)
            self.assertIn("atlas", [m["id"] for m in reply.json()["data"]])
            reply = client.post(
                "/api/atlas/v1/chat/completions",
                headers=headers,
                json=body | {"model": "atlas-code"},
            )
            self.assertEqual(reply.status_code, 403)
            self.assertEqual(reply.json()["error"]["missing_scope"], "model")
            self.assertEqual(
                client.post(
                    "/api/atlas/v1/chat/completions",
                    headers=headers,
                    json=body | {"org_id": "forged"},
                ).status_code,
                403,
            )
            reply = client.post(
                "/api/atlas/v1/chat/completions", headers=headers, json=body
            )
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertIn("56", reply.json()["choices"][0]["message"]["content"])
            reply = client.post(
                "/api/atlas/v1/chat/completions",
                headers=headers,
                json=body | {"stream": True},
            )
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertIn("56", reply.text)
            self.assertIn("data: [DONE]", reply.text)
            reply = client.post(
                "/api/atlas/v1/chat/completions", headers=headers, json=body
            )
            self.assertEqual(reply.status_code, 429, reply.text)
            self.assertEqual(reply.json()["error"]["requests"], 2)
            self.assertEqual(reply.json()["error"]["daily_requests"], 2)
            broker.side_effect = PermissionError("inactive_member")
            self.assertEqual(
                client.get("/api/atlas/v1/models", headers=headers).status_code, 401
            )
        self.assertEqual(self.store.usage(row["id"])["charged_micro_eur"], 2000)
        ledger = DailyLedger().summary()
        self.assertEqual(ledger[0]["user"], "webui-test-id")
        self.assertEqual(Decimal(str(ledger[0]["measured_eur"])), Decimal("0.002"))
        self.assertFalse(key_call.get())

    def test_raw_model_scope_and_reservations_use_the_same_member_ledger(self) -> None:
        from dev_gateway import Plan

        row, key = self.create(scopes=["model"], daily_requests=2)
        plan = Plan("{}", "https://example.invalid/v1", 1000, Decimal(1), Decimal(1))

        async def frames(plan: Plan) -> AsyncIterator[dict[str, Any]]:
            yield {
                "choices": [
                    {"index": 0, "delta": {"content": "56"}, "finish_reason": None}
                ]
            }
            yield {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield {
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                "cost_micro_eur": 12,
            }

        headers = {"Authorization": "Bearer " + key}
        body = {
            "model": "atlas-code",
            "messages": [{"role": "user", "content": "Calculate 7 times 8"}],
        }
        with (
            TestClient(app) as client,
            patch(
                "services.orchestrator.dev_inference.gateway.prepare", return_value=plan
            ),
            patch(
                "services.orchestrator.dev_inference.gateway.events", side_effect=frames
            ),
        ):
            models = client.get("/api/atlas/v1/models", headers=headers).json()
            self.assertEqual([m["id"] for m in models["data"]], ["atlas-code"])
            rejected = client.post(
                "/api/atlas/v1/chat/completions",
                headers=headers,
                json=body | {"model": "atlas"},
            )
            self.assertEqual(rejected.status_code, 403)
            self.assertEqual(rejected.json()["error"]["missing_scope"], "assistant")
            reply = client.post(
                "/api/atlas/v1/chat/completions", headers=headers, json=body
            )
            self.assertEqual(reply.status_code, 200, reply.text)
            self.assertEqual(reply.json()["choices"][0]["message"]["content"], "56")
            reply = client.post(
                "/api/atlas/v1/chat/completions",
                headers=headers,
                json=body | {"stream": True},
            )
            self.assertEqual(reply.status_code, 200)
            self.assertIn("56", reply.text)
            reply = client.post(
                "/api/atlas/v1/chat/completions", headers=headers, json=body
            )
            self.assertEqual(reply.status_code, 429)
            self.assertEqual(reply.json()["error"]["daily_requests"], 2)
        self.assertEqual(self.store.usage(row["id"])["charged_micro_eur"], 24)
        self.assertEqual(DailyLedger().summary()[0]["measured_eur"], 0.000024)

    def test_existing_user_search_limit_applies_to_key_requests(self) -> None:
        _, key = self.create()

        async def respond(payload: ChatRequest, item: Interaction) -> None:
            payload.__dict__["daily_ledger"].reserve_search(
                payload.__dict__["daily_user"], "en", "tavily"
            )
            self.fail("A second search must be refused before a provider call")

        with (
            patch.dict(os.environ, {"ATLAS_USER_DAILY_SEARCHES": "1"}),
            TestClient(app) as client,
            patch("services.orchestrator.chat_api.process", side_effect=respond),
        ):
            DailyLedger().reserve_search("webui-test-id", "en", "tavily")
            reply = client.post(
                "/api/atlas/v1/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json={
                    "model": "atlas",
                    "messages": [{"role": "user", "content": "Find current news"}],
                },
            )
            self.assertEqual(reply.status_code, 429, reply.text)
            self.assertIn("2 searches", reply.json()["error"]["message"])
            self.assertIn("1 searches", reply.json()["error"]["message"])

    def test_http_expired_revoked_invalid_and_quota_before_provider(self) -> None:
        row, key = self.create(daily_micro_eur=1)
        headers = {"Authorization": "Bearer " + key}
        with (
            TestClient(app) as client,
            patch("services.orchestrator.chat_api.process") as process,
        ):
            reply = client.post(
                "/api/atlas/v1/chat/completions",
                headers=headers,
                json={
                    "model": "atlas",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )
            self.assertEqual(reply.status_code, 429)
            self.assertEqual(reply.json()["error"]["daily_micro_eur"], 1)
            process.assert_not_called()
            for auth in (
                {},
                {"Authorization": "Bearer invalid"},
                {"Authorization": "Basic invalid"},
            ):
                self.assertEqual(
                    client.get("/api/atlas/v1/models", headers=auth).status_code, 401
                )
            self.now[0] = float(row["expires_at"])
            self.assertEqual(
                client.get("/api/atlas/v1/models", headers=headers).status_code, 401
            )
            self.now[0] -= 1
            self.store.revoke(row["id"])
            self.assertEqual(
                client.get("/api/atlas/v1/models", headers=headers).status_code, 401
            )
