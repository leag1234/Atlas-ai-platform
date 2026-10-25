"""Public repository registration, immutable approval, tenant isolation and replay."""

import json
import os
import unittest
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import test_project_forge as forge_tests
import test_project_isolation as isolation
from services.retrieval.project_files import ProjectFiles
from services.retrieval.project_scoped_api import store


class RepositoryImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        isolation.ProjectIsolationTests.setUpClass()
        cls.addClassCleanup(isolation.ProjectIsolationTests.doClassCleanups)

    def setUp(self) -> None:
        fixture = forge_tests.ProjectForgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.case = fixture.case
        with store(self.case.app_dsn).scope(
            self.case.identity("editor"), self.case.org, self.case.project
        ) as scope:
            self.folder = ProjectFiles(scope).folder(
                dict(name="Import", locality="server", model_use="allow")
            )
        config = patch.dict(
            os.environ,
            {
                "ATLAS_FORGE_CONNECTORS": json.dumps(
                    {fixture.connector: {"forge": "github"}}
                )
            },
        )
        config.start()
        self.addCleanup(config.stop)
        factory = patch(
            "services.retrieval.project_repository_import.configured_adapter",
            return_value=fixture.adapter,
        )
        factory.start()
        self.addCleanup(factory.stop)
        metadata = patch.object(
            fixture.adapter, "request", return_value={"default_branch": "main"}
        )
        metadata.start()
        self.addCleanup(metadata.stop)
        self.data = dict(
            folder_id=self.folder["id"],
            connector_id=fixture.connector,
            remote_ref=fixture.repository["remote_ref"],
        )
        self.path = self.case.project_path() + "/repositories"

    def pending(self) -> dict[str, Any]:
        result = self.case.request(
            "POST", self.path + "/import-approvals", "editor", self.data
        )
        self.assertEqual(result.status_code, 201, result.text)
        return dict(result.json())

    def allow(self, approval: dict[str, Any]) -> Any:
        return self.case.request(
            "POST",
            self.case.project_path() + "/approvals/" + approval["id"] + "/decision",
            "editor",
            dict(allow=True, action_digest=approval["action_digest"]),
        )

    def test_public_import_requires_exact_decision_and_consumes_once(self) -> None:
        approval = self.pending()
        request = dict(self.data, approval_id=approval["id"])
        self.case.error(
            self.case.request("POST", self.path, "editor", request),
            403,
            "approval_required",
        )
        self.assertEqual(self.allow(approval).status_code, 200)
        self.case.error(
            self.case.request(
                "POST",
                self.path,
                "editor",
                dict(request, remote_ref="https://other.example/a.git"),
            ),
            403,
            "approval_required",
        )
        result = self.case.request("POST", self.path, "editor", request)
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["base_commit"], self.fixture.remote.base)
        self.case.error(
            self.case.request("POST", self.path, "editor", request),
            410,
            "approval_expired_or_replayed",
        )

    def test_denial_expiry_member_and_project_binding(self) -> None:
        approval = self.pending()
        decision = (
            self.case.project_path() + "/approvals/" + approval["id"] + "/decision"
        )
        self.case.error(
            self.case.request(
                "POST",
                decision,
                "owner",
                dict(allow=True, action_digest=approval["action_digest"]),
            ),
            404,
            "not_found",
        )
        self.case.error(
            self.case.request(
                "POST", decision, "editor", dict(allow=True, action_digest="0" * 64)
            ),
            403,
            "approval_required",
        )
        with store(self.case.app_dsn).scope(
            self.case.identity("editor"), self.case.org, self.case.project
        ) as scope:
            from services.retrieval.project_work import Work

            approval["expires_at"] = "2020-01-01T00:00:00Z"
            Work(scope).update_approval(approval)
        self.case.error(self.allow(approval), 410, "approval_expired_or_replayed")
        self.case.error(
            self.case.request(
                "POST", self.path, "editor", dict(self.data, approval_id=str(uuid4()))
            ),
            404,
            "not_found",
        )

    def test_work_forge_snapshot_is_immutable_and_fenced(self) -> None:
        import base64
        import hashlib
        from services.retrieval.project_scoped_api import execution_dispatch
        from services.retrieval.project_forge import run_operation

        conversation = self.case.request(
            "POST",
            self.case.project_path() + "/conversations",
            "editor",
            dict(title="Independent forge fixture"),
        ).json()
        session_response = self.case.request(
            "POST",
            self.case.project_path() + "/work-sessions",
            "editor",
            dict(
                conversation_id=conversation["id"],
                folder_ids=[self.fixture.repository["folder_id"]],
                base_versions=[
                    dict(
                        file_id=self.fixture.file["id"],
                        version_id=self.fixture.file["version_id"],
                    )
                ],
            ),
        )
        self.assertEqual(session_response.status_code, 201, session_response.text)
        session = session_response.json()
        identity = self.case.identity("editor", self.case.project)
        request = str(uuid4())

        def call(operation: str, data: dict[str, Any]) -> Any:
            return execution_dispatch(
                identity, operation, json.dumps(data).encode(), str(uuid4())
            )[1]

        acquired = call(
            "acquire",
            dict(
                session_id=session["id"],
                request_id=request,
                expected_revision=session["revision"],
                storage_bytes=0,
            ),
        )
        fence = dict(
            session_id=session["id"],
            request_id=request,
            generation=acquired["session"]["lease"]["generation"],
        )
        content = self.fixture.content
        digest = hashlib.sha256(content).hexdigest()
        action = dict(
            kind="commit",
            base_commit=self.fixture.remote.base,
            target_branch="main",
            change_digest=digest,
            snapshot=dict(id=str(uuid4()), sha256=digest),
            parameters=dict(message="Approved snapshot"),
        )
        pending = call(
            "forge",
            dict(
                fence,
                operation="issue",
                action=action,
                snapshot_content=base64.b64encode(content).decode(),
            ),
        )
        self.assertEqual(pending["approval"]["session_id"], session["id"])
        self.assertEqual(self.allow(pending["approval"]).status_code, 200)
        with self.assertRaises(Exception) as denied:
            call(
                "forge",
                dict(
                    fence,
                    generation=fence["generation"] + 1,
                    operation="execute",
                    operation_id=pending["operation_id"],
                ),
            )
        self.assertEqual(getattr(denied.exception, "status", None), 409)
        sent = call(
            "forge",
            dict(fence, operation="execute", operation_id=pending["operation_id"]),
        )
        self.assertEqual(sent["state"], "running")
        run_operation(store(self.case.app_dsn), identity, pending["operation_id"])
        result = call(
            "forge",
            dict(fence, operation="status", operation_id=pending["operation_id"]),
        )
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(
            self.fixture.remote.git("show", "main:hello.txt"), "Approved bytes"
        )
        with self.assertRaises(Exception) as replay:
            call(
                "forge",
                dict(fence, operation="execute", operation_id=pending["operation_id"]),
            )
        self.assertEqual(getattr(replay.exception, "status", None), 410)

    def test_connector_enrollment_requires_operator_and_does_not_grant_org_use(
        self,
    ) -> None:
        from services.retrieval.project_install import allow_forge_connector
        import psycopg

        connector = str(uuid4())
        with self.assertRaises(ValueError):
            allow_forge_connector(self.case.app_dsn, connector)
        allow_forge_connector(self.case.admin_dsn, connector)
        allow_forge_connector(self.case.admin_dsn, connector)
        with psycopg.connect(self.case.admin_dsn) as db:
            row = db.execute(
                "SELECT ceilings FROM atlas_projects.installation"
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row[0]["connector_allowlist"].count(connector), 1)
            row = db.execute(
                "SELECT policy FROM atlas_projects.organizations WHERE org_id=%s",
                (self.case.org,),
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            self.assertNotIn(connector, row[0]["connector_allowlist"])
