"""One approval binds the entire publication; recovery never repeats known effects."""

import copy
from typing import Any
from unittest.mock import patch

import test_project_forge as fixture
from services.orchestrator.project_forge import ForgeUnknown
from services.retrieval.project_forge import run_operation
from services.retrieval.project_scoped_api import store


class PublishChangeTests(fixture.ProjectForgeTests):
    def publication(self) -> dict[str, Any]:
        action = self.action("commit")
        action.pop("target_branch")
        action.update(
            kind="publish_change",
            parameters=dict(
                branch="review/repair",
                target="main",
                message="Repair total",
                title="Fix discount",
                body="Verified with unit tests",
            ),
        )
        return action

    def send_approved(self, action: dict[str, Any], pending: dict[str, Any]) -> None:
        self.assertEqual(self.allow(pending).status_code, 200)
        result = self.case.request(
            "POST",
            self.path,
            "editor",
            dict(action, approval_id=pending["approval_id"]),
        )
        self.assertEqual(result.status_code, 202, result.text)

    def test_one_approval_all_steps_and_exact_fields(self) -> None:
        action = self.publication()
        pending = self.pending(action)
        summary = pending["approval"]["publication"]
        self.assertEqual(summary["branch"], "review/repair")
        self.assertEqual(summary["message"], "Repair total")
        self.assertEqual(summary["title"], "Fix discount")
        self.assertEqual(summary["diff"], [{"path": "hello.txt", "status": "added"}])
        self.assertEqual(self.remote.writes, 0)
        self.assertEqual(self.allow(pending).status_code, 200)
        for field in ("branch", "target", "message", "title", "body"):
            changed = copy.deepcopy(action)
            changed["parameters"][field] += "-changed"
            self.case.error(
                self.case.request(
                    "POST",
                    self.path,
                    "editor",
                    dict(changed, approval_id=pending["approval_id"]),
                ),
                403,
                "approval_required",
            )
        sent = self.case.request(
            "POST",
            self.path,
            "editor",
            dict(action, approval_id=pending["approval_id"]),
        )
        self.assertEqual(sent.status_code, 202, sent.text)
        operation = self.stored(pending)
        self.assertEqual(operation["state"], "succeeded")
        self.assertEqual(
            operation["completed_steps"], ["branch", "commit", "pull_request"]
        )
        self.assertEqual(self.remote.writes, 3)
        self.assertEqual(
            self.remote.git("rev-parse", "refs/heads/main"), self.remote.base
        )
        self.assertEqual(
            self.remote.git("show", "review/repair:hello.txt"), "Approved bytes"
        )
        self.assertEqual(len(self.remote.pull_requests), 1)
        self.case.error(
            self.case.request(
                "POST",
                self.path,
                "editor",
                dict(action, approval_id=pending["approval_id"]),
            ),
            410,
            "approval_expired_or_replayed",
        )

    def test_target_base_mismatch_writes_nothing(self) -> None:
        action = self.publication()
        pending = self.pending(action)
        self.assertEqual(self.allow(pending).status_code, 200)
        tree = self.remote.git("mktree", content=b"")
        moved = self.remote.git(
            "commit-tree", tree, "-p", self.remote.base, content=b"Moved\n"
        )
        self.remote.git("update-ref", "refs/heads/main", moved)
        self.case.error(
            self.case.request(
                "POST",
                self.path,
                "editor",
                dict(action, approval_id=pending["approval_id"]),
            ),
            409,
            "revision_conflict",
        )
        self.assertEqual(self.remote.writes, 0)

    def test_pr_failure_retains_branch_and_commit_and_reconciles(self) -> None:
        action = self.publication()
        pending = self.pending(action)
        original = self.adapter.send

        def send(operation: dict[str, Any], content: bytes | None) -> None:
            if operation["action"]["kind"] == "pull_request":
                raise ForgeUnknown("forge_unavailable")
            original(operation, content)

        with patch.object(self.adapter, "send", side_effect=send):
            self.send_approved(action, pending)
        failed = self.stored(pending)
        self.assertEqual(failed["state"], "unknown")
        self.assertEqual(failed["failed_step"], "pull_request")
        self.assertEqual(failed["completed_steps"], ["branch", "commit"])
        self.assertEqual(self.remote.writes, 2)
        run_operation(
            store(self.case.app_dsn),
            self.case.identity("editor", self.case.project),
            pending["operation_id"],
        )
        self.assertEqual(self.stored(pending)["state"], "succeeded")
        self.assertEqual(self.remote.writes, 3)
        run_operation(
            store(self.case.app_dsn),
            self.case.identity("editor", self.case.project),
            pending["operation_id"],
        )
        self.assertEqual(self.remote.writes, 3)

    def test_lost_pr_response_reconciles_without_duplicate(self) -> None:
        action = self.publication()
        pending = self.pending(action)
        original = self.adapter.send

        def send(operation: dict[str, Any], content: bytes | None) -> None:
            original(operation, content)
            if operation["action"]["kind"] == "pull_request":
                raise ForgeUnknown("lost_response")

        with patch.object(self.adapter, "send", side_effect=send):
            self.send_approved(action, pending)
        self.assertEqual(self.stored(pending)["state"], "unknown")
        run_operation(
            store(self.case.app_dsn),
            self.case.identity("editor", self.case.project),
            pending["operation_id"],
        )
        self.assertEqual(self.stored(pending)["state"], "succeeded")
        self.assertEqual(len(self.remote.pull_requests), 1)
        self.assertEqual(self.remote.writes, 3)

    def test_recovery_after_every_remote_effect_is_idempotent(self) -> None:
        for step in ("branch", "commit", "pull_request"):
            with self.subTest(step=step):
                action = self.publication()
                action["parameters"]["branch"] = "review/" + step
                pending = self.pending(action)
                original = self.adapter.send

                def send(operation: dict[str, Any], content: bytes | None) -> None:
                    original(operation, content)
                    if operation["action"]["kind"] == step:
                        raise ForgeUnknown("lost_response")

                before = self.remote.writes
                with patch.object(self.adapter, "send", side_effect=send):
                    self.send_approved(action, pending)
                self.assertEqual(self.stored(pending)["failed_step"], step)
                run_operation(
                    store(self.case.app_dsn),
                    self.case.identity("editor", self.case.project),
                    pending["operation_id"],
                )
                self.assertEqual(self.stored(pending)["state"], "succeeded")
                self.assertEqual(self.remote.writes - before, 3)
                run_operation(
                    store(self.case.app_dsn),
                    self.case.identity("editor", self.case.project),
                    pending["operation_id"],
                )
                self.assertEqual(self.remote.writes - before, 3)

    def test_target_moving_between_steps_stops_remaining_effects(self) -> None:
        action = self.publication()
        pending = self.pending(action)
        original = self.adapter.send

        def send(operation: dict[str, Any], content: bytes | None) -> None:
            original(operation, content)
            if operation["action"]["kind"] == "branch":
                tree = self.remote.git("mktree", content=b"")
                moved = self.remote.git(
                    "commit-tree", tree, "-p", self.remote.base, content=b"Moved\n"
                )
                self.remote.git("update-ref", "refs/heads/main", moved)

        with patch.object(self.adapter, "send", side_effect=send):
            self.send_approved(action, pending)
        operation = self.stored(pending)
        self.assertEqual(operation["state"], "failed")
        self.assertEqual(operation["failed_step"], "commit")
        self.assertEqual(operation["completed_steps"], ["branch"])
        self.assertEqual(self.remote.writes, 1)
        self.assertEqual(self.remote.pull_requests, [])

    def test_denied_publication_has_no_effect(self) -> None:
        action = self.publication()
        pending = self.pending(action)
        decision = self.case.request(
            "POST",
            self.case.project_path() + f"/approvals/{pending['approval_id']}/decision",
            "editor",
            dict(allow=False, action_digest=pending["approval"]["action_digest"]),
        )
        self.assertEqual(decision.status_code, 200, decision.text)
        result = self.case.request(
            "POST",
            self.path,
            "editor",
            dict(action, approval_id=pending["approval_id"]),
        )
        self.case.error(result, 403, "approval_required")
        self.assertEqual(self.remote.writes, 0)

    def test_result_retains_only_same_origin_pr_url(self) -> None:
        original = self.adapter.request
        for index, url in enumerate(
            ("https://forge.example/org/demo/pull/1", "https://foreign.example/pull/1")
        ):
            with self.subTest(url=url):
                action = self.publication()
                action["parameters"]["branch"] = "review/url-" + str(index)
                pending = self.pending(action)

                def request(
                    method: str, suffix: str, data: dict[str, Any] | None = None
                ) -> Any:
                    value = original(method, suffix, data)
                    if method == "GET" and suffix.startswith("/pulls?"):
                        for row in value:
                            row["html_url"] = url
                    return value

                with patch.object(self.adapter, "request", side_effect=request):
                    self.send_approved(action, pending)
                operation = self.stored(pending)
                self.assertEqual(operation["state"], "succeeded")
                self.assertEqual(operation["pr_url"], url if index == 0 else None)

    def test_work_session_publication_uses_one_approval_and_resumes(self) -> None:
        import base64
        import hashlib
        import json
        from uuid import uuid4
        from services.retrieval.project_scoped_api import execution_dispatch

        conversation = self.case.request(
            "POST",
            self.case.project_path() + "/conversations",
            "editor",
            dict(title="Publication session"),
        ).json()
        response = self.case.request(
            "POST",
            self.case.project_path() + "/work-sessions",
            "editor",
            dict(
                conversation_id=conversation["id"],
                folder_ids=[self.repository["folder_id"]],
                base_versions=[
                    dict(file_id=self.file["id"], version_id=self.file["version_id"])
                ],
            ),
        )
        self.assertEqual(response.status_code, 201, response.text)
        session = response.json()
        identity = self.case.identity("editor", self.case.project)
        request_id = str(uuid4())

        def call(operation: str, data: dict[str, Any]) -> Any:
            return execution_dispatch(
                identity, operation, json.dumps(data).encode(), str(uuid4())
            )[1]

        acquired = call(
            "acquire",
            dict(
                session_id=session["id"],
                request_id=request_id,
                expected_revision=session["revision"],
                storage_bytes=0,
            ),
        )
        fence = dict(
            session_id=session["id"],
            request_id=request_id,
            generation=acquired["session"]["lease"]["generation"],
        )
        action = self.publication()
        digest = hashlib.sha256(self.content).hexdigest()
        action.update(
            change_digest=digest, snapshot=dict(id=str(uuid4()), sha256=digest)
        )
        pending = call(
            "forge",
            dict(
                fence,
                operation="issue",
                action=action,
                snapshot_content=base64.b64encode(self.content).decode(),
            ),
        )
        path = self.case.project_path() + "/work-sessions/" + session["id"]
        waiting = self.case.request("GET", path, "editor").json()
        self.assertEqual(waiting["state"], "awaiting_approval")
        self.assertEqual(len(waiting["pending_approvals"]), 1)
        self.assertEqual(self.allow(pending).status_code, 200)
        self.assertEqual(
            self.case.request("GET", path, "editor").json()["state"], "running"
        )
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
            result["completed_steps"], ["branch", "commit", "pull_request"]
        )
        self.assertEqual(self.remote.writes, 3)
        self.assertEqual(
            self.case.request("GET", path, "editor").json()["pending_approvals"], []
        )
