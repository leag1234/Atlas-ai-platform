"""Login resolution uses the host API and never creates a missing user."""

import os
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer
from fastapi.testclient import TestClient

from services.orchestrator.key_subject import resolve_login, SubjectError
from services.orchestrator.api_key_admin import app


class SubjectTests(unittest.IsolatedAsyncioTestCase):
    async def test_email_local_part_missing_ambiguous_and_inactive(self) -> None:
        users = [
            {"email": "tester@example.invalid", "id": "budget-identity", "role": "user"}
        ]
        calls = []

        async def directory(request: web.Request) -> web.Response:
            calls.append((request.method, request.path))
            self.assertEqual(
                request.headers["Authorization"], "Bearer directory-test-token"
            )
            return web.json_response({"users": users, "total": len(users)})

        server_app = web.Application()
        server_app.router.add_get("/api/v1/users/", directory)
        async with TestServer(server_app) as server:
            with patch.dict(
                os.environ,
                {
                    "ATLAS_WEBUI_URL": str(server.make_url("/")),
                    "ATLAS_WEBUI_ADMIN_TOKEN": "directory-test-token",
                },
            ):
                for login in ("tester", "tester@example.invalid", "TESTER"):
                    self.assertEqual(
                        await resolve_login(login),
                        ("tester@example.invalid", "budget-identity"),
                    )
                with self.assertRaisesRegex(SubjectError, "user_never_signed_in"):
                    await resolve_login("unknown")
                users.append(
                    {"email": "tester@another.invalid", "id": "another", "role": "user"}
                )
                with self.assertRaisesRegex(SubjectError, "ambiguous_login_use_email"):
                    await resolve_login("tester")
                users[0]["role"] = "pending"
                with self.assertRaisesRegex(SubjectError, "user_inactive"):
                    await resolve_login("tester@example.invalid")
        self.assertTrue(all(method == "GET" for method, _ in calls))

    def test_stable_admin_resolves_login_and_ignores_budget_override(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "ATLAS_STABLE": "1",
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_API_KEY_ADMIN_TOKEN": "admin-test-" * 4,
                },
            ),
            patch(
                "services.orchestrator.api_key_admin.resolve_login",
                return_value=("tester@example.invalid", "real-budget-id"),
            ),
            patch("services.orchestrator.api_key_admin.configured_store") as store,
            TestClient(app, client=("127.0.0.1", 1111)) as client,
        ):
            store.return_value.create.return_value = ({"id": "one"}, "test-delivery")
            response = client.post(
                "/keys",
                headers={"Authorization": "Bearer " + "admin-test-" * 4},
                json={
                    "login": "tester",
                    "name": "test",
                    "scopes": ["assistant"],
                    "budget_user": "forged",
                },
            )
            self.assertEqual(response.status_code, 201)
            values = store.return_value.create.call_args.kwargs
            self.assertEqual(values["budget_user"], "real-budget-id")
            self.assertEqual(values["member_id"], "real-budget-id")
            self.assertEqual(values["subject"], "tester@example.invalid")
            response = client.post(
                "/keys",
                headers={"Authorization": "Bearer " + "admin-test-" * 4},
                json={
                    "name": "test",
                    "scopes": ["assistant"],
                    "subject": "internal-id",
                    "member_id": "internal-id",
                    "budget_user": "internal-id",
                },
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], "login_required")

    def test_legacy_service_import_does_not_require_interactive_login(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"ATLAS_STABLE": "1", "ATLAS_API_KEY_ADMIN_TOKEN": "admin-test-" * 4},
            ),
            patch("services.orchestrator.api_key_admin.DeveloperStore"),
            patch("services.orchestrator.api_key_admin.configured_store") as store,
            patch("services.orchestrator.api_key_admin.resolve_login") as resolve,
            TestClient(app, client=("127.0.0.1", 1111)) as client,
        ):
            store.return_value.import_developer.return_value = {
                "id": "imported",
                "scopes": ["model"],
            }
            reply = client.post(
                "/imports/service-test",
                headers={"Authorization": "Bearer " + "admin-test-" * 4},
                json={
                    "org_id": "org",
                    "member_id": "service",
                    "subject": "service-test",
                    "budget_user": "service-ledger",
                    "name": "migration",
                    "scopes": ["model"],
                },
            )
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.json()["scopes"], ["model"])
            resolve.assert_not_called()
            self.assertEqual(
                store.return_value.import_developer.call_args.kwargs["budget_user"],
                "service-ledger",
            )
