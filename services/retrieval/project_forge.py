"""Retrieval-owned repository approvals, immutable snapshot reads and durable sends.

The orchestrator adapter owns no database. This broker runs it on the retrieval
side, outside the worker, with current RLS scope and operator-pinned connectors.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from services.orchestrator.project_forge import (
    Connector,
    ForgeAdapter,
    ForgeError,
    ForgeUnknown,
    GitHubForge,
    snapshot_files,
)
from services.retrieval.project_files import ProjectFiles, action_digest, timestamp
from services.retrieval.project_identity import Identity, ProjectError
from services.retrieval.project_ledger import BYTE_KEYS, append
from services.retrieval.project_store import Scope, ScopedProjects
from services.retrieval.project_work import Work
from services.retrieval.project_work_contract import validate

Json = dict[str, Any]


def configured_adapter(repository: Json) -> ForgeAdapter:
    """Only operator configuration resolves tokens; never accept a caller URL."""
    try:
        configured = json.loads(os.environ.get("ATLAS_FORGE_CONNECTORS", "{}"))[
            repository["connector_id"]
        ]
        if (
            repository["forge"] != "github"
            or configured["remote_ref"] != repository["remote_ref"]
        ):
            raise ValueError
        for key in ("remote_ref", "api_url"):
            parsed = urlsplit(configured[key])
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError
            addresses = socket.getaddrinfo(
                parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
            )
            if not addresses or any(
                not ipaddress.ip_address(item[4][0]).is_global for item in addresses
            ):
                raise ValueError
        token = os.environ[configured["token_env"]]
        if not token:
            raise ValueError
        return GitHubForge(
            Connector(
                remote_ref=configured["remote_ref"],
                api_url=configured["api_url"],
                repository=configured["repository"],
                author_name=configured["author_name"],
                author_email=configured["author_email"],
                token=token,
            )
        )
    except (KeyError, TypeError, ValueError, OSError, ForgeError):
        raise ProjectError(
            403, "policy_denied", "Repository connector is unavailable or disallowed"
        ) from None


class RepositoryActions:
    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self.files = ProjectFiles(scope)
        self.work = Work(scope)

    def repository(self, identifier: str) -> Json:
        value = self.files.get("repository", identifier)
        if value["connector_id"] not in self.scope.policy["connector_allowlist"]:
            raise ProjectError(403, "policy_denied")
        self.work.folder(value["folder_id"])
        return value

    def snapshot(self, action: Json) -> tuple[bytes | None, list[Json], list[str]]:
        if action["kind"] != "commit":
            return None, [], []
        ref = action["snapshot"]
        version = self.files.get("version", ref["id"])
        file, version = self.files.version(version["file_id"], version["id"])
        self.work.folder(file["folder_id"])
        _, content, _ = self.files.content(file["id"], version["id"])
        if (
            len(content) > self.scope.policy["quotas"]["upload_bytes"]
            or version["sha256"] != ref["sha256"]
        ):
            raise ProjectError(409, "revision_conflict")
        try:
            entries = snapshot_files(content, ref["sha256"])
        except ForgeError:
            raise ProjectError(
                422, "invalid_request", "Invalid repository snapshot"
            ) from None
        return (
            content,
            [dict(file_id=file["id"], version_id=version["id"])],
            [item["path"] for item in entries],
        )

    def operation(self, identifier: str) -> Json:
        row = self.scope.db.execute(
            "SELECT data FROM atlas_projects.forge_operations WHERE org_id=%s AND project_id=%s AND id=%s FOR UPDATE",
            (self.files.org, self.files.project, identifier),
        ).fetchone()
        if row is None:
            raise ProjectError(404, "not_found")
        return dict(row["data"])

    def save(self, operation: Json) -> None:
        self.scope.db.execute(
            "UPDATE atlas_projects.forge_operations SET data=%s WHERE org_id=%s AND project_id=%s AND id=%s",
            (Jsonb(operation), self.files.org, self.files.project, operation["id"]),
        )

    def audit(self, operation: Json, approval: Json, phase: str, outcome: str) -> None:
        event_id = str(uuid4())
        actor = dict(kind="member", member_id=self.scope.identity.member, service=None)
        record = dict(
            self.files.new(),
            approval_id=approval["id"],
            request_id=operation["id"],
            actor=actor,
            phase=phase,
            action_digest=approval["action_digest"],
            timestamp=timestamp(),
            outcome=outcome,
            operation_id=operation["id"],
            ledger_event_id=event_id,
        )
        append(
            self.scope,
            kind="repository_action",
            request_id=operation["id"],
            actor=actor,
            resource_refs=[],
            source=dict(kind="server", ref=self.files.project),
            destination=approval["destination"],
            byte_counts={k: 0 for k in BYTE_KEYS},
            outcome="intent"
            if outcome in {"pending", "allowed", "running"}
            else "denied"
            if outcome in {"denied", "expired"}
            else outcome,
            approval_id=approval["id"],
            audit_record=record,
            event_id=event_id,
        )

    def current(self, operation: Json) -> tuple[Json, bytes | None]:
        self.scope.authorize("POST", ["repositories"])
        approval = self.work.approval(operation["approval_id"])["data"]
        if (
            self.scope.identity.member != approval["requested_by_member_id"]
            or self.scope.org["revision"] != approval["policy_revision"]
            or self.work.grant_stamp(approval["requested_by_member_id"])
            != operation["grant"]
            or self.repository(operation["repository"]["id"]) != operation["repository"]
        ):
            raise ProjectError(403, "approval_required")
        content, _, _ = self.snapshot(operation["action"])
        return approval, content

    def submit(self, identifier: str, data: Json) -> tuple[int, Json]:
        validate("RepositoryAction", data)
        action = {k: v for k, v in data.items() if k != "approval_id"}
        if "approval_id" in data:
            row = self.work.approval(data["approval_id"])
            if not row["execution"] or "forge_operation" not in row["execution"]:
                raise ProjectError(403, "approval_required")
            operation = self.operation(row["execution"]["forge_operation"])
            approval = row["data"]
            if row["consumed_at"] or operation["state"] != "pending":
                raise ProjectError(410, "approval_expired_or_replayed")
            if datetime.fromisoformat(approval["expires_at"]) <= datetime.now(
                timezone.utc
            ):
                return self.expire(operation, approval)
            if (
                action != operation["action"]
                or identifier != operation["repository"]["id"]
                or approval["decision"] != "allowed"
            ):
                raise ProjectError(403, "approval_required")
            self.current(operation)
            if (
                configured_adapter(operation["repository"]).bind(action)
                != operation["binding"]
            ):
                raise ProjectError(409, "revision_conflict")
            approval["consumed_at"] = timestamp()
            self.work.update_approval(approval)
            self.scope.db.execute(
                "UPDATE atlas_projects.file_approvals SET consumed_at=now() WHERE org_id=%s AND project_id=%s AND id=%s",
                (self.files.org, self.files.project, approval["id"]),
            )
            operation["state"] = "running"
            self.save(operation)
            self.audit(operation, approval, "execution", "allowed")
        else:
            repository = self.repository(identifier)
            content, versions, paths = self.snapshot(action)
            binding = configured_adapter(repository).bind(action)
            operation = dict(
                id=str(uuid4()),
                created_at=timestamp(),
                action=action,
                repository=repository,
                binding=binding,
                grant=self.work.grant_stamp(self.scope.identity.member),
                state="pending",
                stage="queued",
                commit=None,
            )
            digest = action_digest(
                self.scope,
                action["kind"],
                dict(
                    action=action,
                    repository=repository,
                    binding=binding,
                    operation_id=operation["id"],
                ),
            )
            approval = dict(
                self.files.new(),
                session_id=None,
                requested_by_member_id=self.scope.identity.member,
                approver_member_id=self.scope.identity.member,
                actor=dict(
                    kind="member", member_id=self.scope.identity.member, service=None
                ),
                action_kind=action["kind"],
                action_digest=digest,
                paths=paths,
                version_refs=versions,
                destination=dict(kind="connector", ref=repository["connector_id"]),
                maximum_bytes=len(content or b""),
                expires_at=(datetime.now(timezone.utc) + timedelta(seconds=120))
                .isoformat()
                .replace("+00:00", "Z"),
                decision="pending",
                consumed_at=None,
                policy_revision=self.scope.org["revision"],
            )
            operation["approval_id"] = approval["id"]
            self.scope.db.execute(
                "INSERT INTO atlas_projects.file_approvals(org_id,project_id,id,data,execution) VALUES(%s,%s,%s,%s,%s)",
                (
                    self.files.org,
                    self.files.project,
                    approval["id"],
                    Jsonb(approval),
                    Jsonb(dict(forge_operation=operation["id"])),
                ),
            )
            self.scope.db.execute(
                "INSERT INTO atlas_projects.forge_operations VALUES(%s,%s,%s,%s,%s)",
                (
                    self.files.org,
                    self.files.project,
                    operation["id"],
                    approval["id"],
                    Jsonb(operation),
                ),
            )
            self.audit(operation, approval, "request", "pending")
        return 202, dict(
            operation_id=operation["id"],
            state=operation["state"],
            approval_id=approval["id"],
            approval=approval,
        )

    def expire(self, operation: Json, approval: Json) -> tuple[int, Json]:
        approval["decision"] = "expired"
        operation["state"] = "failed"
        self.work.update_approval(approval)
        self.save(operation)
        self.audit(operation, approval, "outcome", "expired")
        # Returning the error commits the audit; raising would roll it back.
        return 410, ProjectError(410, "approval_expired_or_replayed").body

    def decide(self, identifier: str, data: Json) -> tuple[int, Any]:
        validate("ApprovalDecision", data)
        row = self.work.approval(identifier)
        approval = row["data"]
        operation = self.operation(row["execution"]["forge_operation"])
        if approval["approver_member_id"] != self.scope.identity.member:
            raise ProjectError(404, "not_found")
        if row["consumed_at"] or approval["decision"] != "pending":
            raise ProjectError(410, "approval_expired_or_replayed")
        if datetime.fromisoformat(approval["expires_at"]) <= datetime.now(timezone.utc):
            return self.expire(operation, approval)
        self.current(operation)
        if data["action_digest"] != approval["action_digest"]:
            self.audit(operation, approval, "decision", "denied")
            return 403, ProjectError(403, "approval_required").body
        approval["decision"] = "allowed" if data["allow"] else "denied"
        self.work.update_approval(approval)
        self.audit(operation, approval, "decision", approval["decision"])
        return 200, dict(accepted=True, consumed_at=None)


def run_operation(store: ScopedProjects, identity: Identity, identifier: str) -> None:
    """Also the private recovery entry point: same durable ID, no new approval.

    A session lock serializes recovery across processes, while separate RLS
    transactions commit each checkpoint before an effect can be sent.
    """
    with psycopg.connect(store.pool.dsn, autocommit=True) as lock:
        acquired = lock.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,1))", (identifier,)
        ).fetchone()
        if not acquired or not acquired[0]:
            return
        try:
            _run_operation(store, identity, identifier)
        except (ProjectError, psycopg.Error, OSError, ForgeError):
            # Stored intent survives infrastructure/authority failure. Never
            # print exception contents or mistake lack of evidence for success.
            logging.getLogger(__name__).info(
                "repository_operation interrupted operation_id=%s", identifier
            )


def _run_operation(store: ScopedProjects, identity: Identity, identifier: str) -> None:
    with store.scope(identity, identity.org, identity.project) as scope:
        broker = RepositoryActions(scope)
        operation = broker.operation(identifier)
        if operation["state"] not in {"running", "unknown"}:
            return
        approval, content = broker.current(operation)
        adapter = configured_adapter(operation["repository"])
        if operation["stage"] == "queued":
            operation["commit"] = adapter.prepare(operation, content)
            operation["stage"] = "prepared"
            broker.save(operation)
    with store.scope(identity, identity.org, identity.project) as scope:
        broker = RepositoryActions(scope)
        operation = broker.operation(identifier)
        approval, content = broker.current(operation)
        outcome = adapter.reconcile(operation)
        if outcome != "absent":
            operation["state"] = "failed" if outcome == "conflict" else outcome
            broker.save(operation)
            broker.audit(operation, approval, "outcome", operation["state"])
            return
        operation["stage"] = "sending"
        broker.save(operation)
    # A crash from here onwards requires remote read reconciliation above.
    with store.scope(identity, identity.org, identity.project) as scope:
        broker = RepositoryActions(scope)
        operation = broker.operation(identifier)
        approval, content = broker.current(operation)
        if datetime.fromisoformat(approval["expires_at"]) <= datetime.now(timezone.utc):
            operation["state"] = "failed"
            broker.save(operation)
            broker.audit(operation, approval, "outcome", "expired")
            return
        try:
            adapter.send(operation, content)
            outcome = adapter.reconcile(operation)
            operation["state"] = "succeeded" if outcome == "succeeded" else "unknown"
        except ForgeUnknown:
            operation["state"] = "unknown"
        except ForgeError:
            operation["state"] = "failed"
        broker.save(operation)
        broker.audit(operation, approval, "outcome", operation["state"])
