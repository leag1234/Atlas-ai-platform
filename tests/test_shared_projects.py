"""REQ-SHARE-02/03/04/05/06: real PostgreSQL sharing and worker fences."""

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from services.retrieval.project_identity import ProjectError
from services.retrieval.project_install import upgrade_work
from services.retrieval.project_scoped_api import store

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_project_conversations as conversations
import test_project_work as work_tests
import test_project_auth as auth_tests


class SharedProjectsTests(conversations.ProjectConversationTests):
    def test_private_ledger_keeps_global_chain_and_scopes_outbox(self) -> None:
        from services.retrieval.project_ledger import claim_outbox

        self.assertEqual(self.begin().status_code, 201)
        self.assertEqual(self.broker("finish", self.finish_body()).status_code, 200)
        # Another member's read appends after private events without seeing them.
        response = self.request("GET", self.project_path() + "/instructions", "owner")
        self.assertEqual(response.status_code, 200, response.text)
        with psycopg.connect(self.admin_dsn) as db:
            events = [
                row[0]
                for row in db.execute(
                    "SELECT data FROM atlas_projects.ledger_events WHERE project_id=%s ORDER BY seq",
                    (self.project,),
                )
            ]
        self.assertTrue(any(e["conversation_id"] == self.conversation for e in events))
        for previous, current in zip(events, events[1:]):
            self.assertEqual(current["previous_digest"], previous["digest"])
        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            claimed = claim_outbox(scope)
        self.assertTrue(claimed)
        self.assertFalse(
            any(e["conversation_id"] == self.conversation for e in claimed)
        )

    def share(self, role: str = "viewer") -> Any:
        return self.request(
            "POST",
            self.project_path() + f"/conversations/{self.conversation}/share",
            role,
            {"confirm": True},
            match=0,
        )

    def test_role_matrix(self) -> None:
        # Actual authorization used by all public/broker operations, before parsing.
        actions = [
            ("GET", ["files"], {"viewer", "editor", "owner"}),
            ("GET", ["instructions"], {"viewer", "editor", "owner"}),
            ("GET", ["memory"], {"viewer", "editor", "owner"}),
            ("GET", ["conversations"], {"viewer", "editor", "owner"}),
            ("POST", ["conversations"], {"viewer", "editor", "owner"}),
            ("POST", ["work-sessions"], {"editor", "owner"}),
            ("POST", ["approvals", str(uuid4()), "decision"], {"editor", "owner"}),
            ("POST", ["files"], {"editor", "owner"}),
            ("PUT", ["instructions"], {"editor", "owner"}),
            ("POST", ["memory"], {"editor", "owner"}),
            ("PUT", ["grants", str(uuid4())], {"owner"}),
            ("DELETE", ["grants", str(uuid4())], {"owner"}),
            ("PUT", ["connectors"], {"owner"}),
        ]
        for role in ("viewer", "editor", "owner", "admin"):
            for method, tail, permitted in actions:
                with self.subTest(role=role, method=method, tail=tail):

                    def check() -> None:
                        with store(self.app_dsn).scope(
                            self.identity(role), self.org, self.project
                        ) as scope:
                            scope.authorize(method, tail)

                    if role in permitted:
                        check()
                    else:
                        with self.assertRaises(ProjectError) as caught:
                            check()
                        self.assertEqual(
                            caught.exception.status, 404 if role == "admin" else 403
                        )
        # Admin with a viewer grant has precisely viewer content/Work rights.
        self.assertEqual(
            self.request(
                "PUT",
                self.project_path() + "/grants/" + self.admin,
                "owner",
                {"role": "viewer"},
                match=0,
            ).status_code,
            200,
        )
        for method, tail, permitted in actions:
            with self.subTest(admin_grant="viewer", tail=tail):
                with store(self.app_dsn).scope(
                    self.identity("admin"), self.org, self.project
                ) as scope:
                    if "viewer" in permitted:
                        scope.authorize(method, tail)
                    else:
                        with self.assertRaises(ProjectError):
                            scope.authorize(method, tail)

    def test_private_share_author_and_audit(self) -> None:
        self.assertEqual(self.begin().status_code, 201)
        self.assertEqual(self.broker("finish", self.finish_body()).status_code, 200)
        turns = self.project_path() + f"/conversations/{self.conversation}/turns"
        for role in ("editor", "owner", "admin"):
            self.error(self.request("GET", turns, role), 404, "not_found")
            listed = self.request("GET", self.project_path() + "/conversations", role)
            if role != "admin":
                self.assertEqual(listed.json()["items"], [])
                ledger = self.request("GET", self.project_path() + "/ledger", role)
                self.assertNotIn(self.turn_id, ledger.text)
                events = self.request("GET", self.project_path() + "/events", role)
                self.assertNotIn(self.turn_id, events.text)
        denied = self.request(
            "POST",
            self.project_path() + f"/conversations/{self.conversation}/share",
            "viewer",
            {"confirm": False},
            match=0,
        )
        self.error(denied, 422, "invalid_request")
        # A creator alone can explicitly share, including a viewer's own Chat.
        self.error(self.share("owner"), 404, "not_found")
        # The completed turn updated the conversation revision.
        revision = self.request(
            "GET", self.project_path() + "/conversations", "viewer"
        ).json()["items"][0]["revision"]
        shared = self.request(
            "POST",
            self.project_path() + f"/conversations/{self.conversation}/share",
            "viewer",
            {"confirm": True},
            match=revision,
        )
        self.assertEqual(shared.status_code, 200, shared.text)
        self.assertEqual(shared.json()["visibility"], "shared")
        for role in ("viewer", "editor", "owner"):
            response = self.request("GET", turns, role)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json()["items"][0]["member_id"], self.members["viewer"]
            )
            self.assertIn("Revenue rose", response.text)
        self.error(self.request("GET", turns, "admin"), 404, "not_found")
        with psycopg.connect(self.admin_dsn) as db:
            kinds = [
                r[0]
                for r in db.execute(
                    "SELECT kind FROM atlas_projects.audit WHERE org_id=%s", (self.org,)
                )
            ]
        self.assertIn("conversation_shared", kinds)

    def test_shared_turn_busy_and_committed_context(self) -> None:
        self.assertEqual(self.share().status_code, 200)

        def start(role: str) -> Any:
            return self.broker(
                "begin", dict(request_id=str(uuid4()), request=self.payload()), role
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(start, ["viewer", "editor"]))
        self.assertEqual(sorted(r.status_code for r in results), [201, 409])
        busy = next(r for r in results if r.status_code == 409)
        self.assertEqual(busy.json()["error"], "session_busy")
        self.assertIn("retry", busy.text)
        winner = next(r.json()["turn"] for r in results if r.status_code == 201)
        self.turn_id = winner["id"]
        role = "viewer" if winner["member_id"] == self.members["viewer"] else "editor"
        self.assertEqual(
            self.broker("finish", self.finish_body(), role).status_code, 200
        )
        following = start("owner")
        self.assertEqual(following.status_code, 201, following.text)
        self.assertIn("Revenue rose", str(following.json()["context"]["history"]))
        self.assertEqual(following.json()["turn"]["member_id"], self.members["owner"])

    def test_admin_management_never_grants_implicit_content(self) -> None:
        prefix = f"/api/atlas/orgs/{self.org}"
        revoke = prefix + f"/project-grants/{self.project}/{self.members['editor']}"
        for role in ("viewer", "editor", "owner"):
            self.error(
                self.request("DELETE", revoke, role, match=1), 403, "permission_denied"
            )
        self.assertEqual(
            self.request("DELETE", revoke, "admin", match=1).status_code, 204
        )
        transfer = prefix + f"/project-ownership/{self.project}"
        self.assertEqual(
            self.request(
                "POST",
                transfer,
                "admin",
                {"member_id": self.members["viewer"]},
                match=0,
            ).status_code,
            204,
        )
        self.error(self.request("GET", self.project_path(), "admin"), 404, "not_found")
        result = self.request("GET", self.project_path(), "viewer")
        self.assertEqual(result.json()["owner_member_id"], self.members["viewer"])
        self.error(
            self.request(
                "PUT",
                self.project_path() + "/grants/" + self.members["editor"],
                "owner",
                {"role": "editor"},
                match=0,
            ),
            403,
            "permission_denied",
        )
        with psycopg.connect(self.admin_dsn) as db:
            kinds = [
                r[0]
                for r in db.execute(
                    "SELECT kind FROM atlas_projects.audit WHERE org_id=%s", (self.org,)
                )
            ]
        self.assertIn("admin_grant_revoked", kinds)
        self.assertIn("ownership_transferred", kinds)

    def test_member_lookup_and_connector_management(self) -> None:
        with psycopg.connect(self.admin_dsn) as db:
            for role, name in (
                ("owner", "Aster Vale"),
                ("editor", "Rowan Field"),
                ("viewer", "Mira Cloud"),
            ):
                db.execute(
                    "UPDATE atlas_projects.members SET data=data || %s WHERE org_id=%s AND id=%s",
                    (Jsonb({"display_name": name}), self.org, self.members[role]),
                )
        path = self.project_path() + "/members"
        for role in ("viewer", "editor"):
            self.error(self.request("GET", path, role), 403, "permission_denied")
        self.error(self.request("GET", path, "admin"), 404, "not_found")
        result = self.request("GET", path, "owner")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn(
            {"id": self.members["editor"], "display_name": "Rowan Field"},
            result.json()["items"],
        )
        self.assertTrue(
            all(set(row) == {"id", "display_name"} for row in result.json()["items"])
        )
        path = self.project_path() + "/connectors"
        self.assertEqual(
            self.request("GET", path, "viewer").json(), {"connector_ids": []}
        )
        for role in ("viewer", "editor"):
            self.error(
                self.request("PUT", path, role, {"connector_ids": []}, match=0),
                403,
                "permission_denied",
            )
        self.error(
            self.request(
                "PUT", path, "owner", {"connector_ids": [str(uuid4())]}, match=0
            ),
            403,
            "policy_denied",
        )
        self.assertEqual(
            self.request(
                "PUT", path, "owner", {"connector_ids": []}, match=0
            ).status_code,
            200,
        )

    def test_revoked_private_conversation_stays_private_and_chat_boundary_fails(
        self,
    ) -> None:
        self.assertEqual(self.begin().status_code, 201)
        self.assertEqual(
            self.request(
                "DELETE",
                self.project_path() + "/grants/" + self.members["viewer"],
                "owner",
                match=1,
            ).status_code,
            204,
        )
        self.error(
            self.broker(
                "check",
                {"request_id": self.turn_id, "conversation_id": self.conversation},
            ),
            404,
            "not_found",
        )
        for role in ("owner", "editor"):
            self.error(
                self.request(
                    "GET",
                    self.project_path() + f"/conversations/{self.conversation}/turns",
                    role,
                ),
                404,
                "not_found",
            )
        with psycopg.connect(self.admin_dsn) as db:
            row = db.execute(
                "SELECT data FROM atlas_projects.resources WHERE id=%s", (self.turn_id,)
            ).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row[0]["state"], "cancelled")

    def test_repeatable_upgrade_preserves_private_content(self) -> None:
        upgrade_work(self.admin_dsn)
        upgrade_work(self.admin_dsn)
        result = self.request("GET", self.project_path() + "/conversations", "viewer")
        self.assertEqual(result.json()["items"][0]["visibility"], "private")
        self.assertEqual(
            self.request("GET", self.project_path() + "/conversations", "owner").json()[
                "items"
            ],
            [],
        )

    def test_deactivation_cancels_private_chat_without_changing_visibility(
        self,
    ) -> None:
        self.assertEqual(self.begin().status_code, 201)
        response = self.request(
            "PATCH",
            f"/api/atlas/orgs/{self.org}/members/{self.members['viewer']}",
            "admin",
            {"status": "revoked"},
            match=0,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.error(self.begin(), 404, "not_found")
        with psycopg.connect(self.admin_dsn) as db:
            turn = db.execute(
                "SELECT data FROM atlas_projects.resources WHERE id=%s",
                (self.turn_id,),
            ).fetchone()
            conversation = db.execute(
                "SELECT data FROM atlas_projects.resources WHERE id=%s",
                (self.conversation,),
            ).fetchone()
        assert turn is not None and conversation is not None
        self.assertEqual(turn[0]["state"], "cancelled")
        self.assertEqual(conversation[0]["visibility"], "private")

    def test_independent_connections_serialize_shared_turns(self) -> None:
        from services.retrieval.project_store import ProjectPool, ScopedProjects
        from services.retrieval.project_transcript import Transcript

        self.assertEqual(self.share().status_code, 200)

        def begin(role: str) -> int:
            pool = ProjectPool(self.app_dsn)
            try:
                with ScopedProjects(pool).scope(
                    self.identity(role), self.org, self.project
                ) as scope:
                    return Transcript(scope).begin(
                        dict(request_id=str(uuid4()), request=self.payload()),
                        str(uuid4()),
                    )[0]
            except ProjectError as exc:
                self.assertEqual(exc.body["error"], "session_busy")
                return exc.status
            finally:
                pool.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(begin, ["editor", "viewer"])), [201, 409])

    def test_chat_tool_boundary_checks_before_effect(self) -> None:
        from services.orchestrator.loop import Call
        from services.orchestrator.project_transcript import ContextTools

        self.assertEqual(self.begin().status_code, 201)
        effects: list[str] = []

        class Memory:
            async def execute(self, call: Call) -> dict[str, Any]:
                effects.append(call.name)
                return {}

        async def check() -> None:
            result = self.broker(
                "check",
                dict(request_id=self.turn_id, conversation_id=self.conversation),
            )
            if result.status_code != 200:
                raise ProjectError(result.status_code, result.json()["error"])

        self.assertEqual(
            self.request(
                "DELETE",
                self.project_path() + "/grants/" + self.members["viewer"],
                "owner",
                match=1,
            ).status_code,
            204,
        )
        with self.assertRaises(ProjectError):
            asyncio.run(
                ContextTools({}, Memory(), check).execute(
                    Call("1", "project_memory", "{}"), 1
                )
            )
        self.assertEqual(effects, [])


class SharedWorkTests(work_tests.ProjectWorkTests):
    def test_public_private_cancel_preserves_accepted_state(self) -> None:
        import os
        from unittest.mock import patch

        from fastapi.testclient import TestClient
        from services.orchestrator.chat_api import app as front_app
        from services.retrieval.api import app as retrieval_app

        serve = getattr(auth_tests.ProjectAuthTests.serve, "__func__")
        retrieval_url = serve(type(self), retrieval_app)
        secret = os.urandom(32).hex()
        conversation = self.request(
            "POST",
            self.project_path() + "/conversations",
            "editor",
            {"title": "Rowan's private analysis"},
        ).json()
        session = self.request(
            "POST",
            self.project_path() + "/work-sessions",
            "editor",
            dict(conversation_id=conversation["id"], folder_ids=[], base_versions=[]),
        )
        self.assertEqual(session.status_code, 201, session.text)
        self.session = session.json()
        self.assertEqual(self.acquire().status_code, 200)
        with (
            patch.dict(
                os.environ,
                {
                    "ATLAS_PROJECT_MODE": "1",
                    "ATLAS_RETRIEVAL_URL": retrieval_url,
                    "ATLAS_PROJECT_IDENTITY_SECRET": os.urandom(32).hex(),
                    "ATLAS_PROJECT_ORIGIN": "http://testserver",
                    "ATLAS_PROXY_SECRET": secret,
                    "ATLAS_TRUSTED_EMAIL_HEADER": "X-Atlas-User",
                },
            ),
            TestClient(front_app) as client,
        ):
            response = client.post(
                self.project_path() + f"/work-sessions/{self.session['id']}/cancel",
                json=dict(request_id=self.turn_id),
                headers={
                    "X-Atlas-User": "owner",
                    "X-Atlas-Proxy-Secret": secret,
                    "Origin": "http://testserver",
                    "Idempotency-Key": str(uuid4()),
                },
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json(), {"state": "cancelling"})
        self.assertTrue(self.turn("check").json()["cleanup_required"])
        released = self.turn(
            "release", terminal_state="cancelled", cleanup_confirmed=True
        )
        self.assertEqual(released.status_code, 200, released.text)
        self.assertEqual(released.json()["session"]["state"], "cancelled")
        self.error(
            self.request(
                "GET",
                self.project_path() + f"/work-sessions/{self.session['id']}",
                "owner",
            ),
            404,
            "not_found",
        )

    def test_private_work_cancel_without_disclosing_content(self) -> None:
        created = self.request(
            "POST",
            self.project_path() + "/conversations",
            "editor",
            {"title": "Rowan's private analysis"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        session = self.request(
            "POST",
            self.project_path() + "/work-sessions",
            "editor",
            dict(conversation_id=created.json()["id"], folder_ids=[], base_versions=[]),
        )
        self.assertEqual(session.status_code, 201, session.text)
        self.session = session.json()
        self.assertEqual(self.acquire().status_code, 200)
        approval, _ = self.issue()
        path = self.project_path() + "/work-sessions/" + self.session["id"]
        for role in ("viewer", "owner", "admin"):
            self.error(self.request("GET", path, role), 404, "not_found")
        cancelled = self.request(
            "POST",
            path + "/cancel",
            "owner",
            dict(request_id=self.turn_id),
        )
        self.assertEqual(cancelled.status_code, 202, cancelled.text)
        self.assertEqual(cancelled.json(), {"state": "cancelling"})
        self.error(self.request("GET", path, "owner"), 404, "not_found")
        self.error(self.decision(approval), 410, "approval_expired_or_replayed")
        self.assertTrue(self.turn("check").json()["cleanup_required"])

    def test_fake_worker_revocation_stops_before_second_tool_and_voids_approvals(
        self,
    ) -> None:
        self.assertEqual(self.acquire().status_code, 200)
        from services.retrieval.project_files import ProjectFiles

        approval, file = self.issue()
        self.assertEqual(self.decision(approval).status_code, 200)
        response = self.broker(
            "approvals",
            dict(
                self.fence(),
                approver_member_id=self.members["editor"],
                action_kind="overwrite",
                effects=ProjectFiles.overwrite_effects(file, b"later"),
                destination=dict(kind="server", ref=self.project),
                maximum_bytes=5,
            ),
        )
        self.assertEqual(response.status_code, 200, response.text)
        pending = response.json()
        effects: list[str] = []

        tester = self

        class Broker:
            async def call(
                self, operation: str, data: dict[str, Any]
            ) -> dict[str, Any]:
                response = tester.broker(operation, data)
                if response.status_code != 200:
                    raise ProjectError(response.status_code, response.json()["error"])
                return dict(response.json())

        async def worker() -> None:
            from services.orchestrator.code_mode import WorkExecution

            session = self.request(
                "GET",
                self.project_path() + "/work-sessions/" + self.session["id"],
                "editor",
            ).json()
            execution = WorkExecution(Broker(), session, self.turn_id)
            await execution.check()
            effects.append("first tool")
            revoked = self.request(
                "DELETE",
                self.project_path() + "/grants/" + self.members["editor"],
                "owner",
                match=1,
            )
            self.assertEqual(revoked.status_code, 204, revoked.text)
            await execution.check()
            effects.append("second tool")

        with self.assertRaises(ProjectError):
            asyncio.run(worker())
        self.assertEqual(effects, ["first tool"])
        with psycopg.connect(self.admin_dsn) as db:
            session = db.execute(
                "SELECT data FROM atlas_projects.resources WHERE id=%s",
                (self.session["id"],),
            ).fetchone()
            decisions = [
                r[0]
                for r in db.execute(
                    "SELECT data->>'decision' FROM atlas_projects.file_approvals WHERE id=ANY(%s::uuid[])",
                    ([approval["id"], pending["id"]],),
                )
            ]
        assert session is not None
        self.assertEqual(session[0]["state"], "cancelling")
        self.assertEqual(decisions, ["invalidated", "invalidated"])

    def test_cancel_another_members_work_requires_owner_or_granted_admin(self) -> None:
        self.assertEqual(self.acquire().status_code, 200)
        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            scope.grant_write(self.members["ungranted"], "editor", '"0"')
        cancel = self.project_path() + f"/work-sessions/{self.session['id']}/cancel"
        self.error(
            self.request("POST", cancel, "ungranted", {"request_id": self.turn_id}),
            403,
            "permission_denied",
        )
        self.assertEqual(
            self.request(
                "POST", cancel, "owner", {"request_id": self.turn_id}
            ).status_code,
            202,
        )
