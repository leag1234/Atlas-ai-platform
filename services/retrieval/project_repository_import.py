"""REQ-PROJ-05: public import approvals, bound to immutable remote bytes."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb

from services.retrieval.project_files import action_digest, timestamp
from services.retrieval.project_forge import RepositoryActions, configured_adapter
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_work_contract import validate
from services.orchestrator.project_forge import canonical, snapshot_files

Json = dict[str, Any]


class RepositoryImports(RepositoryActions):
    def destination(self, data: Json) -> tuple[Json, Json]:
        self.scope.authorize("POST", ["repositories"])
        folder = self.files.upload_folder(data["folder_id"])
        self.work.folder(folder["id"])
        if data["connector_id"] not in self.scope.policy["connector_allowlist"]:
            raise ProjectError(403, "policy_denied")
        import os

        try:
            config = json.loads(os.environ.get("ATLAS_FORGE_CONNECTORS", "{}"))[
                data["connector_id"]
            ]
            repo = dict(data, forge=config.get("forge", "github"))
            repo.pop("approval_id", None)
        except (KeyError, ValueError, TypeError):
            raise ProjectError(403, "policy_denied") from None
        # Imports only populate an empty folder, never overwrite project content.
        if any(
            f["folder_id"] == folder["id"] for f in self.files.listing("file")
        ) or any(
            r["folder_id"] == folder["id"] for r in self.files.listing("repository")
        ):
            raise ProjectError(409, "revision_conflict")
        return folder, repo

    def issue(self, data: Json) -> Json:
        validate("RepositoryImportRequest", data)
        folder, repo = self.destination(data)
        adapter = configured_adapter(repo)
        base, payload = adapter.import_snapshot(
            self.scope.policy["quotas"]["upload_bytes"]
        )
        digest = hashlib.sha256(payload).hexdigest()
        entries = snapshot_files(payload, digest)
        binding = dict(
            connector_digest=adapter.connector_digest(),
            request=data,
            repository=repo,
            base_commit=base,
            sha256=digest,
            folder_revision=folder["revision"],
            grant=self.work.grant_stamp(self.scope.identity.member),
        )
        value = dict(
            self.files.new(),
            session_id=None,
            requested_by_member_id=self.scope.identity.member,
            approver_member_id=self.scope.identity.member,
            actor=dict(
                kind="member", member_id=self.scope.identity.member, service=None
            ),
            action_kind="repository_import",
            action_digest=action_digest(self.scope, "repository_import", binding),
            paths=[f["path"] for f in entries],
            version_refs=[],
            destination=dict(kind="connector", ref=data["connector_id"]),
            maximum_bytes=sum(len(base64.b64decode(f["content"])) for f in entries),
            expires_at=(datetime.now(timezone.utc) + timedelta(seconds=120))
            .isoformat()
            .replace("+00:00", "Z"),
            decision="pending",
            consumed_at=None,
            policy_revision=self.scope.org["revision"],
        )
        self.scope.db.execute(
            "INSERT INTO atlas_projects.file_approvals(org_id,project_id,id,data,execution) VALUES(%s,%s,%s,%s,%s)",
            (
                self.files.org,
                self.files.project,
                value["id"],
                Jsonb(value),
                Jsonb(dict(repository_import=binding)),
            ),
        )
        self.work.audit(value, value["id"], "request", "pending")
        return value

    def current_import(self, row: Json) -> Json:
        approval, execution = row["data"], row["execution"]
        if not execution or "repository_import" not in execution:
            raise ProjectError(403, "approval_required")
        if approval["requested_by_member_id"] != self.scope.identity.member:
            raise ProjectError(404, "not_found")
        if row["consumed_at"] or datetime.fromisoformat(
            approval["expires_at"]
        ) <= datetime.now(timezone.utc):
            raise ProjectError(410, "approval_expired_or_replayed")
        binding = execution["repository_import"]
        folder, repo = self.destination(binding["request"])
        if (
            approval["policy_revision"] != self.scope.org["revision"]
            or binding["grant"] != self.work.grant_stamp(self.scope.identity.member)
            or folder["revision"] != binding["folder_revision"]
            or repo != binding["repository"]
        ):
            raise ProjectError(409, "revision_conflict")
        if configured_adapter(repo).connector_digest() != binding["connector_digest"]:
            raise ProjectError(409, "revision_conflict")
        return dict(binding)

    def decide(self, identifier: str, data: Json) -> tuple[int, Any]:
        validate("ApprovalDecision", data)
        row = self.work.approval(identifier)
        self.current_import(row)
        approval = row["data"]
        if approval["decision"] != "pending":
            raise ProjectError(410, "approval_expired_or_replayed")
        if data["action_digest"] != approval["action_digest"]:
            raise ProjectError(403, "approval_required")
        approval["decision"] = "allowed" if data["allow"] else "denied"
        self.work.update_approval(approval)
        self.work.audit(approval, identifier, "decision", approval["decision"])
        return 200, dict(accepted=True, consumed_at=None)

    def create(self, data: Json) -> Json:
        validate("RepositoryCreate", data)
        row = self.work.approval(data["approval_id"])
        binding = self.current_import(row)
        approval = row["data"]
        if (
            approval["decision"] != "allowed"
            or {k: v for k, v in data.items() if k != "approval_id"}
            != binding["request"]
        ):
            raise ProjectError(403, "approval_required")
        _, payload = configured_adapter(binding["repository"]).import_snapshot(
            approval["maximum_bytes"], binding["base_commit"]
        )
        entries = snapshot_files(payload, binding["sha256"])
        # A transaction owns the approval lock, folder revision, all imported
        # versions and the repository registration. Failure rolls it all back.
        approval["consumed_at"] = timestamp()
        self.work.update_approval(approval)
        self.scope.db.execute(
            "UPDATE atlas_projects.file_approvals SET consumed_at=now() WHERE org_id=%s AND project_id=%s AND id=%s",
            (self.files.org, self.files.project, approval["id"]),
        )
        self.work.audit(approval, approval["id"], "execution", "allowed")
        for entry in entries:
            folder = self.files.get("folder", data["folder_id"])
            self.files.create_file(
                folder["id"],
                entry["path"],
                base64.b64decode(entry["content"]),
                folder["revision"],
                origin="import",
            )
        value = dict(
            self.files.new(),
            **binding["repository"],
            base_commit=binding["base_commit"],
        )
        self.files.put(
            "repository",
            value,
            data["folder_id"],
            private=dict(
                snapshot_sha256=hashlib.sha256(
                    canonical(dict(files=entries))
                ).hexdigest()
            ),
        )
        self.work.audit(approval, approval["id"], "outcome", "succeeded")
        return value
