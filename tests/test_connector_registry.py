"""Real tenant database + local MCP/OAuth: policy, accounts and single-use approval."""

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb

from services.retrieval.connector_store import allowed, configured_store
from services.retrieval.project_identity import sign
from services.retrieval.project_install import upgrade_work
from services.retrieval.project_scoped_api import store
from services.retrieval.project_files import ProjectFiles
import test_project_isolation as isolation
from connector_fake import FakeMCP


class RegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        isolation.ProjectIsolationTests.setUpClass()
        cls.addClassCleanup(isolation.ProjectIsolationTests.doClassCleanups)

    def setUp(self) -> None:
        # Other suite modules enable INFO globally; OAuth state must not enter logs.
        logger = logging.getLogger("httpx")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.WARNING)
        self.fixture = isolation.ProjectIsolationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.server = FakeMCP()
        self.addCleanup(self.server.close)
        self.identifier = str(uuid4())
        self.personal = str(uuid4())
        self.path = Path(self.fixture.directory.name) / "broker.sqlite"
        environment = patch.dict(
            os.environ,
            dict(
                ATLAS_MCP_BROKER_DB=str(self.path),
                ATLAS_MCP_BROKER_KEY=base64.b64encode(os.urandom(32)).decode(),
                ATLAS_MCP_ALLOW_LOCAL="1",
            ),
        )
        environment.start()
        self.addCleanup(environment.stop)
        with psycopg.connect(self.fixture.admin_dsn) as db:
            policy = dict(
                self.fixture.policy,
                connector_allowlist=[self.identifier, self.personal],
            )
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=jsonb_set(ceilings, '{connector_allowlist}', %s)",
                (Jsonb([self.identifier, self.personal]),),
            )
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s WHERE org_id=%s",
                (Jsonb(policy), self.fixture.org),
            )
        upgrade_work(self.fixture.admin_dsn)
        with store(self.fixture.app_dsn).scope(
            self.fixture.identity("owner", self.fixture.project),
            self.fixture.org,
            self.fixture.project,
        ) as scope:
            files = ProjectFiles(scope)
            conversation = dict(
                files.new(),
                title="Connector test",
                owner_member_id=self.fixture.members["owner"],
            )
            files.put("conversation", conversation)
            self.conversation = conversation["id"]
        for identifier, kind, auth, path in [
            (self.identifier, "shared", "none", "/public"),
            (self.personal, "personal", "oauth", "/personal"),
        ]:
            result = self.call(
                "register",
                dict(
                    id=identifier,
                    name="Handbook" if kind == "shared" else "My account",
                    kind=kind,
                    auth=auth,
                    url=self.server.url + path,
                    client_id="atlas-test" if auth == "oauth" else "",
                    redirect_uri=self.server.url + "/callback"
                    if auth == "oauth"
                    else "",
                ),
                "admin",
            )
            self.assertEqual(result.status_code, 200, result.text)
            for role in ("owner", "editor"):
                self.assertEqual(
                    self.call(
                        "member-rule",
                        dict(
                            connector_id=identifier,
                            member_id=self.fixture.members[role],
                            rule="allow",
                        ),
                        "admin",
                    ).status_code,
                    200,
                )

    def call(
        self,
        operation: str,
        data: dict[str, Any],
        role: str = "editor",
        project: bool = False,
    ) -> Any:
        return self.fixture.client.post(
            "/internal/atlas/connectors/" + operation,
            json=data,
            headers={
                "X-Atlas-Project-Identity": sign(
                    self.fixture.identity(
                        role, self.fixture.project if project else None
                    ),
                    self.fixture.key.encode(),
                )
            },
        )

    def action(
        self, identifier: str, tool: str = "search", project: bool = False
    ) -> dict[str, Any]:
        return dict(
            connector_id=identifier,
            tool=tool,
            arguments=dict(query="release"),
            conversation_id=self.conversation if project else None,
        )

    def oauth(self, role: str = "editor") -> None:
        response = self.call("oauth-start", dict(connector_id=self.personal), role)
        self.assertEqual(response.status_code, 200, response.text)
        redirect = httpx.get(
            response.json()["authorization_url"], follow_redirects=False
        )
        query = {
            k: v[0]
            for k, v in parse_qs(urlsplit(redirect.headers["Location"]).query).items()
        }
        response = self.call(
            "oauth-finish", dict(connector_id=self.personal, **query), role
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.call(
                "oauth-finish", dict(connector_id=self.personal, **query), role
            ).status_code,
            400,
        )

    def test_discovery_reads_writes_and_member_revocation(self) -> None:
        discovery = self.call("discover", {}).json()
        self.assertEqual(discovery["connectors"][0]["tools"][0]["name"], "search")
        read = self.call("call", self.action(self.identifier))
        self.assertEqual(read.status_code, 200, read.text)
        self.assertIn("12 November", json.dumps(read.json()))
        write = self.call("call", self.action(self.identifier, "publish")).json()
        self.assertEqual(write["state"], "approval_required")
        self.assertEqual(self.server.writes, 0)
        self.assertEqual(
            self.call(
                "approve", dict(approval_id=write["approval_id"], allow=True), "owner"
            ).status_code,
            409,
        )
        approved = self.call(
            "approve", dict(approval_id=write["approval_id"], allow=True)
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(self.server.writes, 1)
        self.assertEqual(
            self.call(
                "approve", dict(approval_id=write["approval_id"], allow=True)
            ).status_code,
            409,
        )
        self.call(
            "member-rule",
            dict(
                connector_id=self.identifier,
                member_id=self.fixture.members["editor"],
                rule="deny",
            ),
            "admin",
        )
        self.assertEqual(
            self.call("call", self.action(self.identifier)).status_code, 403
        )
        self.assertEqual(
            self.call("call", self.action(self.identifier), "other").status_code, 404
        )

    def test_personal_oauth_isolation_project_default_and_disconnect(self) -> None:
        self.oauth()
        self.assertEqual(
            self.call("call", self.action(self.personal), "owner").status_code, 409
        )
        self.assertEqual(self.call("call", self.action(self.personal)).status_code, 200)
        self.assertEqual(
            self.call(
                "call", self.action(self.personal, project=True), project=True
            ).status_code,
            403,
        )
        self.assertEqual(
            self.call(
                "project-list", dict(connectors=[self.personal]), "editor", True
            ).status_code,
            403,
        )
        self.assertEqual(
            self.call(
                "project-list", dict(connectors=[self.personal]), "owner", True
            ).status_code,
            200,
        )
        before = len(self.server.calls)
        approval = self.call(
            "call", self.action(self.personal, project=True), project=True
        ).json()
        self.assertEqual(approval["state"], "approval_required")
        self.assertIn("every member", approval["visibility_warning"])
        self.assertEqual(len(self.server.calls), before)
        decision = self.call(
            "approve",
            dict(approval_id=approval["approval_id"], allow=False),
            project=True,
        )
        self.assertEqual(decision.json()["state"], "denied")
        self.assertEqual(len(self.server.calls), before)
        for token in self.server.tokens:
            self.assertNotIn(token.encode(), self.path.read_bytes())
        self.assertEqual(
            self.call("disconnect", dict(connector_id=self.personal)).status_code, 200
        )
        self.assertIsNone(
            configured_store().get(
                self.fixture.org,
                "credential",
                self.fixture.members["editor"],
                self.personal,
            )
        )
        self.assertEqual(self.call("call", self.action(self.personal)).status_code, 409)

    def test_allowance_deny_precedence(self) -> None:
        self.assertTrue(allowed([self.identifier], self.identifier, None, ("allow",)))
        self.assertFalse(
            allowed([self.identifier], self.identifier, "deny", ("allow",))
        )
        self.assertFalse(
            allowed([self.identifier], self.identifier, "allow", project=[])
        )
        self.assertEqual(
            self.call(
                "register",
                dict(
                    id=str(uuid4()), name="Unsafe", kind="shared", command_id="unlisted"
                ),
                "admin",
            ).status_code,
            400,
        )

    def test_invalid_arguments_never_reach_tools_or_create_approval(self) -> None:
        for arguments in ({}, {"query": 4}, {"query": "release", "confirmed": True}):
            result = self.call(
                "call", {**self.action(self.identifier), "arguments": arguments}
            )
            self.assertEqual(result.status_code, 400)
        oversized = self.call(
            "call",
            {**self.action(self.identifier), "arguments": {"query": "x" * 16384}},
        )
        self.assertEqual(oversized.status_code, 413)
        self.assertIn("limit 16384 bytes", oversized.json()["message"])
        self.assertEqual(self.server.calls, [])
        self.assertEqual(
            configured_store().list(
                self.fixture.org, "approval", self.fixture.members["editor"]
            ),
            [],
        )

    def test_deactivated_member_cannot_approve_pending_write(self) -> None:
        pending = self.call("call", self.action(self.identifier, "publish")).json()
        with psycopg.connect(self.fixture.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.members SET status='revoked' WHERE org_id=%s AND id=%s",
                (self.fixture.org, self.fixture.members["editor"]),
            )
        result = self.call(
            "approve", dict(approval_id=pending["approval_id"], allow=True)
        )
        self.assertEqual(result.status_code, 404)
        self.assertEqual(self.server.writes, 0)

    def test_personal_every_call_private_reads_and_ledger(self) -> None:
        self.oauth()
        self.call("project-list", dict(connectors=[self.personal]), "owner", True)
        for _ in range(2):
            before = len(self.server.calls)
            result = self.call(
                "call", self.action(self.personal, project=True), project=True
            ).json()
            self.assertEqual(result["state"], "approval_required")
            self.assertEqual(len(self.server.calls), before)
            approved = self.call(
                "approve",
                dict(approval_id=result["approval_id"], allow=True),
                project=True,
            )
            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertEqual(len(self.server.calls), before + 1)
        with store(self.fixture.app_dsn).scope(
            self.fixture.identity("owner", self.fixture.project),
            self.fixture.org,
            self.fixture.project,
        ) as scope:
            events = scope.db.execute(
                "SELECT data FROM atlas_projects.ledger_events WHERE data->>'kind'='approval_decided'"
            ).fetchall()
            self.assertEqual(len(events), 2)
            for row in events:
                self.assertEqual(
                    row["data"]["actor"]["member_id"], self.fixture.members["editor"]
                )
                self.assertEqual(row["data"]["conversation_id"], self.conversation)
                self.assertEqual(row["data"]["source"]["ref"], self.personal)
            files = ProjectFiles(scope)
            conversation = files.get("conversation", self.conversation)
            conversation.update(
                visibility="private", owner_member_id=self.fixture.members["editor"]
            )
            scope.db.execute(
                "UPDATE atlas_projects.resources SET data=%s WHERE org_id=%s AND project_id=%s AND id=%s",
                (
                    Jsonb(conversation),
                    self.fixture.org,
                    self.fixture.project,
                    self.conversation,
                ),
            )
        result = self.call(
            "call", self.action(self.personal, project=True), project=True
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("12 November", json.dumps(result.json()))
        self.assertEqual(
            self.call(
                "call", self.action(self.personal, project=True), "owner", True
            ).status_code,
            404,
        )
        write = self.call(
            "call", self.action(self.personal, "publish", True), project=True
        )
        self.assertEqual(write.json()["state"], "approval_required")

    def test_viewer_approval_and_policy_revocation_before_execute(self) -> None:
        self.call(
            "member-rule",
            dict(
                connector_id=self.personal,
                member_id=self.fixture.members["viewer"],
                rule="allow",
            ),
            "admin",
        )
        self.call("project-list", dict(connectors=[self.personal]), "owner", True)
        self.oauth("viewer")
        pending = self.call(
            "call", self.action(self.personal, project=True), "viewer", True
        )
        self.assertEqual(pending.status_code, 200, pending.text)
        approved = self.call(
            "approve",
            dict(approval_id=pending.json()["approval_id"], allow=True),
            "viewer",
            True,
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        pending = self.call(
            "call", self.action(self.personal, project=True), "viewer", True
        )
        self.call(
            "member-rule",
            dict(
                connector_id=self.personal,
                member_id=self.fixture.members["viewer"],
                rule="deny",
            ),
            "admin",
        )
        before = len(self.server.calls)
        self.assertEqual(
            self.call(
                "approve",
                dict(approval_id=pending.json()["approval_id"], allow=True),
                "viewer",
                True,
            ).status_code,
            403,
        )
        self.assertEqual(len(self.server.calls), before)

    def test_static_accounts_never_cross_members_or_registry_updates(self) -> None:
        import secrets

        identifier = str(uuid4())
        config = dict(
            id=identifier,
            name="Account",
            kind="personal",
            auth="token",
            url=self.server.url + "/personal",
        )
        with psycopg.connect(self.fixture.admin_dsn) as db:
            for table, field in [
                ("organizations", "policy"),
                ("installation", "ceilings"),
            ]:
                db.execute(
                    f"UPDATE atlas_projects.{table} SET {field}=jsonb_set({field}, '{{connector_allowlist}}', %s)",
                    (Jsonb([identifier]),),
                )
        self.assertEqual(self.call("register", config, "admin").status_code, 200)
        for role in ("editor", "owner"):
            self.call(
                "member-rule",
                dict(
                    connector_id=identifier,
                    member_id=self.fixture.members[role],
                    rule="allow",
                ),
                "admin",
            )
            token = secrets.token_urlsafe(32)
            self.server.tokens[token] = role
            self.assertEqual(
                self.call(
                    "connect-token", dict(connector_id=identifier, token=token), role
                ).status_code,
                200,
            )
            self.assertEqual(
                self.call("call", self.action(identifier), role).status_code, 200
            )
            self.assertEqual(self.server.calls[-1]["account"], role)
            self.assertNotIn(token.encode(), self.path.read_bytes())
        self.assertEqual(
            self.call(
                "register", {**config, "url": self.server.url + "/other"}, "admin"
            ).status_code,
            409,
        )
        self.call("disconnect", dict(connector_id=identifier))
        self.assertEqual(self.call("call", self.action(identifier)).status_code, 409)
        self.assertEqual(
            self.call("call", self.action(identifier), "owner").status_code, 200
        )

    def test_oauth_state_is_member_bound_and_expired_state_rejected(self) -> None:
        response = self.call("oauth-start", dict(connector_id=self.personal))
        redirect = httpx.get(
            response.json()["authorization_url"], follow_redirects=False
        )
        query = {
            k: v[0]
            for k, v in parse_qs(urlsplit(redirect.headers["Location"]).query).items()
        }
        self.assertEqual(
            self.call(
                "oauth-finish", dict(connector_id=self.personal, **query), "owner"
            ).status_code,
            400,
        )
        pending = configured_store().get(
            self.fixture.org, "oauth", self.fixture.members["editor"], query["state"]
        )
        self.assertIsNotNone(pending)
        assert pending is not None
        pending["expires"] = 0
        configured_store().put(
            self.fixture.org,
            "oauth",
            self.fixture.members["editor"],
            query["state"],
            pending,
        )
        self.assertEqual(
            self.call(
                "oauth-finish", dict(connector_id=self.personal, **query)
            ).status_code,
            400,
        )

    def test_reconnected_account_invalidates_pending_approval(self) -> None:
        self.oauth()
        pending = self.call("call", self.action(self.personal, "publish")).json()
        self.oauth()
        result = self.call(
            "approve", dict(approval_id=pending["approval_id"], allow=True)
        )
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["error"], "connector_configuration_changed")
        self.assertEqual(self.server.writes, 0)

    def test_confirmation_audit_survives_connector_failure(self) -> None:
        from services.retrieval.connector_service import Service

        self.oauth()
        self.call("project-list", dict(connectors=[self.personal]), "owner", True)
        pending = self.call(
            "call", self.action(self.personal, project=True), project=True
        ).json()
        with patch.object(Service, "exchange", side_effect=TimeoutError):
            result = self.call(
                "approve",
                dict(approval_id=pending["approval_id"], allow=True),
                project=True,
            )
        self.assertEqual(result.status_code, 400)
        with store(self.fixture.app_dsn).scope(
            self.fixture.identity("owner", self.fixture.project),
            self.fixture.org,
            self.fixture.project,
        ) as scope:
            row = scope.db.execute(
                "SELECT data FROM atlas_projects.ledger_events WHERE data->>'kind'='approval_decided'"
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["data"]["approval_id"], pending["approval_id"])
        self.assertEqual(
            self.call(
                "approve",
                dict(approval_id=pending["approval_id"], allow=True),
                project=True,
            ).status_code,
            409,
        )
        self.assertEqual(self.server.calls, [])
