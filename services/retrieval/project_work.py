"""REQ-PROJ-05 authoritative Work transactions inside retrieval's scoped store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from services.retrieval.project_files import ProjectFiles, action_digest, timestamp
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_ledger import BYTE_KEYS, append
from services.retrieval.project_locality import reject_local_only_egress
from services.retrieval.project_store import Scope
from services.retrieval.project_work_contract import validate


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def empty_usage() -> dict[str, Any]:
    return dict(
        tool_calls=0,
        model_calls=0,
        elapsed_seconds=0,
        cost_eur=0,
        reserved_eur=0,
        unknown_eur=0,
        stored_bytes=0,
        input_tokens=0,
        output_tokens=0,
        tokens_estimated=False,
    )


class Work:
    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self.files = ProjectFiles(scope)
        self.db = scope.db
        self.org, self.project = scope.identity.org, scope.project_id

    def lease(self, identifier: str) -> dict[str, Any] | None:
        return self.db.execute(
            "SELECT * FROM atlas_projects.work_leases WHERE org_id=%s AND project_id=%s AND session_id=%s FOR UPDATE",
            (self.org, self.project, identifier),
        ).fetchone()

    def get(self, identifier: str) -> dict[str, Any]:
        try:
            value = self.files.get("work_session", identifier)
        except ProjectError as exc:
            if exc.status != 404:
                raise
            aliases = self.db.execute(
                "SELECT id FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND kind='work_session' AND data->>'legacy_session_id'=%s",
                (self.org, self.project, identifier),
            ).fetchall()
            if len(aliases) != 1:
                raise ProjectError(404, "not_found") from None
            value = self.files.get("work_session", str(aliases[0]["id"]))
        identifier = value["id"]
        row = self.lease(identifier)
        value["lease"] = (
            None
            if row is None or row["data"].get("released")
            else dict(
                holder_member_id=str(row["holder_member_id"]),
                request_id=str(row["request_id"]),
                expires_at=iso(row["expires_at"]),
                generation=row["generation"],
            )
        )
        approvals = self.db.execute(
            "SELECT data,consumed_at FROM atlas_projects.file_approvals WHERE org_id=%s AND project_id=%s AND data->>'session_id'=%s AND data->>'decision' IN ('pending','allowed') ORDER BY data->>'created_at',id",
            (self.org, self.project, identifier),
        ).fetchall()
        value["pending_approvals"] = [
            r["data"] for r in approvals if r["consumed_at"] is None
        ]
        return value

    def bases(self, value: dict[str, Any]) -> None:
        row = self.files.row("work_session", value["id"])
        for identifier in row["private"]["folder_ids"]:
            self.folder(identifier)
        for base in value["base_file_versions"]:
            file, version = self.files.version(base["file_id"], base["version_id"])
            self.folder(file["folder_id"])
            if file["version_id"] != version["id"]:
                from services.retrieval.project_schedules import Schedules

                scheduled = Schedules(self.scope).binding(value["id"])
                if (
                    scheduled is not None
                    and scheduled["run_data"].get("published") == file["version_id"]
                ):
                    current = self.files.get("version", file["version_id"])
                    if current["parent_version_id"] == version["id"]:
                        continue
                raise ProjectError(409, "revision_conflict", revision=file["revision"])

    def folder(self, identifier: str) -> None:
        folder = self.files.get("folder", identifier)
        reject_local_only_egress(folder["locality"])
        if folder["model_use"] != "allow":
            raise ProjectError(403, "policy_denied")
        if folder["parent_id"]:
            self.folder(folder["parent_id"])

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        validate("WorkSessionCreate", data)
        conversation = self.db.execute(
            "SELECT id FROM atlas_projects.legacy_rows WHERE org_id=%s AND project_id=%s AND kind='conversations' AND id=%s",
            (self.org, self.project, data["conversation_id"]),
        ).fetchone()
        # New conversation storage is owned by the conversation adapter; never
        # infer ownership merely from the caller's UUID.
        if conversation is None:
            conversation = self.db.execute(
                "SELECT id FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND kind='conversation' AND id=%s AND data->>'deleted_at' IS NULL",
                (self.org, self.project, data["conversation_id"]),
            ).fetchone()
        if conversation is None:
            raise ProjectError(404, "not_found")
        for folder in data["folder_ids"]:
            self.folder(folder)
        for base in data["base_versions"]:
            file, version = self.files.version(base["file_id"], base["version_id"])
            if (
                file["folder_id"] not in data["folder_ids"]
                or file["version_id"] != version["id"]
            ):
                raise ProjectError(409, "revision_conflict", revision=file["revision"])
        repositories = [
            r
            for r in self.files.listing("repository")
            if r["folder_id"] in data["folder_ids"]
        ]
        if len(repositories) > 1:
            raise ProjectError(
                422, "invalid_request", "Select one repository per Work session"
            )
        workspace = str(uuid4())
        value = dict(
            self.files.new(),
            conversation_id=data["conversation_id"],
            initiator_member_id=self.scope.identity.member,
            workspace_id=workspace,
            base_file_versions=data["base_versions"],
            repository_id=repositories[0]["id"] if repositories else None,
            state="ready",
            plan=[],
            effective_limits=self.scope.policy["quotas"]["per_turn"],
            usage=empty_usage(),
            lease=None,
            last_event_seq=0,
            legacy_session_id=None,
            pending_approvals=[],
            environment=dict(
                location="server",
                workspace_id=workspace,
                network=False,
                device_available=False,
            ),
        )
        self.files.put(
            "work_session", value, private=dict(folder_ids=data["folder_ids"])
        )
        return value

    def grant_stamp(self, member: str) -> str:
        row = self.db.execute(
            "SELECT g.revision,g.data->>'id' AS grant_id,m.revision AS membership_revision FROM atlas_projects.grants g JOIN atlas_projects.members m ON m.org_id=g.org_id AND m.id=g.member_id WHERE g.org_id=%s AND g.project_id=%s AND g.member_id=%s AND g.role IN ('owner','editor') AND m.status='active'",
            (self.org, self.project, member),
        ).fetchone()
        if row is None:
            raise ProjectError(404, "not_found")
        return f"{row['grant_id']}:{row['revision']}:{row['membership_revision']}"

    def authority(self, lease: dict[str, Any]) -> bool:
        data = lease["data"]
        return (
            str(lease["holder_member_id"]) == self.scope.identity.member
            and data["membership_revision"] == self.scope.member["revision"]
            and data["policy_revision"] == self.scope.org["revision"]
            and data["grant_stamp"] == self.grant_stamp(self.scope.identity.member)
        )

    def save_lease(self, identifier: str, row: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO atlas_projects.work_leases(org_id,project_id,session_id,generation,request_id,holder_member_id,expires_at,data) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(org_id,project_id,session_id) DO UPDATE SET generation=excluded.generation,request_id=excluded.request_id,holder_member_id=excluded.holder_member_id,expires_at=excluded.expires_at,data=excluded.data",
            (
                self.org,
                self.project,
                identifier,
                row["generation"],
                row["request_id"],
                row["holder_member_id"],
                row["expires_at"],
                Jsonb(row["data"]),
            ),
        )

    def acquire(self, data: dict[str, Any]) -> dict[str, Any]:
        value = self.get(data["session_id"])
        old = self.lease(value["id"])
        if old and not old["data"].get("released"):
            raise ProjectError(409, "session_busy")
        self.scope.match(value, f'"{data["expected_revision"]}"')
        self.bases(value)
        limits = dict(self.scope.policy["quotas"]["per_turn"])
        from services.retrieval.project_schedules import Schedules

        scheduled = Schedules(self.scope).binding(value["id"])
        if scheduled is not None:
            limits["cost_eur"] = min(
                limits["cost_eur"], scheduled["run_data"]["reserved_eur"]
            )
            limits["per_call_eur"] = min(limits["per_call_eur"], limits["cost_eur"])
        snapshot_bytes = sum(
            self.files.get("version", base["version_id"])["size_bytes"]
            for base in value["base_file_versions"]
        )
        storage_bytes = max(data["storage_bytes"], snapshot_bytes)
        if storage_bytes > limits["expanded_bytes"]:
            raise ProjectError(
                413,
                "size_limit",
                f"workspace: measured {storage_bytes} bytes; limit {limits['expanded_bytes']} bytes",
                measurements=[
                    dict(
                        measured=storage_bytes,
                        limit=limits["expanded_bytes"],
                        unit="bytes",
                    )
                ],
            )
        self.scope.reserve(
            data["request_id"],
            dict(
                concurrent_jobs=1,
                stored_bytes=storage_bytes,
                temporary_bytes=storage_bytes,
                daily_model_eur=limits["cost_eur"],
                monthly_model_eur=limits["cost_eur"],
            ),
        )
        started = now()
        deadline = started + timedelta(seconds=limits["wall_seconds"])
        row = dict(
            generation=old["generation"] + 1 if old else 1,
            request_id=data["request_id"],
            holder_member_id=self.scope.identity.member,
            expires_at=min(deadline, started + timedelta(seconds=30)),
            data=dict(
                reservation_id=data["request_id"],
                reserved_eur=limits["cost_eur"],
                storage_bytes=storage_bytes,
                membership_revision=self.scope.member["revision"],
                policy_revision=self.scope.org["revision"],
                grant_stamp=self.grant_stamp(self.scope.identity.member),
                deadline=iso(deadline),
                cleanup=False,
                released=False,
            ),
        )
        value.update(
            state="running",
            effective_limits=limits,
            usage=dict(empty_usage(), reserved_eur=limits["cost_eur"]),
        )
        self.save_lease(value["id"], row)
        self.files.save(value)
        return dict(session=self.get(value["id"]), cleanup_required=False)

    def fence(
        self, data: dict[str, Any], *, cleanup: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        value = self.get(data["session_id"])
        row = self.lease(value["id"])
        if (
            row is None
            or row["generation"] != data["generation"]
            or str(row["request_id"]) != data["request_id"]
            or str(row["holder_member_id"]) != self.scope.identity.member
            or row["data"].get("released")
        ):
            raise ProjectError(409, "session_busy")
        if not cleanup:
            from services.retrieval.project_schedules import Schedules

            Schedules(self.scope).binding(value["id"])
            self.scope.authorize("POST", ["work-sessions"])
            if (
                row["expires_at"] <= now()
                or row["data"]["cleanup"]
                or value["state"] == "cancelling"
            ):
                raise ProjectError(409, "session_busy")
            if not self.authority(row):
                raise ProjectError(403, "policy_denied")
            self.bases(value)
        return value, row

    def turn(self, data: dict[str, Any]) -> dict[str, Any]:
        operation = data["operation"]
        if operation != "release" and (
            data["usage"] is not None
            or data["terminal_state"] is not None
            or data["cleanup_confirmed"]
        ):
            raise ProjectError(422, "invalid_request")
        if operation == "recover":
            value = self.get(data["session_id"])
            row = self.lease(value["id"])
            if (
                row is None
                or row["generation"] != data["generation"]
                or row["data"].get("released")
            ):
                raise ProjectError(409, "session_busy")
            # Recovery cannot steal a live cleanup owner's fence.
            if row["expires_at"] > now() and (
                row["data"]["cleanup"]
                or (value["state"] != "cancelling" and self.holder_current(row))
            ):
                raise ProjectError(409, "session_busy")
            row.update(
                generation=row["generation"] + 1,
                request_id=data["request_id"],
                holder_member_id=self.scope.identity.member,
                expires_at=now() + timedelta(seconds=30),
            )
            row["data"]["cleanup"] = True
            value["state"] = "cancelling"
        else:
            value, row = self.fence(data, cleanup=operation in {"release", "check"})
            if operation in {"check", "renew"}:
                if not row["data"]["cleanup"] and not self.authority(row):
                    raise ProjectError(403, "policy_denied")
                if operation == "renew" or value["state"] != "cancelling":
                    self.fence(data)
                if operation == "renew":
                    row["expires_at"] = min(
                        datetime.fromisoformat(row["data"]["deadline"]),
                        now() + timedelta(seconds=30),
                    )
            elif operation == "release":
                if not data["cleanup_confirmed"] or data["terminal_state"] is None:
                    raise ProjectError(422, "invalid_request")
                from services.retrieval.project_runners import close as close_runners

                if data["terminal_state"] in {"cancelled", "failed"}:
                    close_runners(self.scope, value["id"])
                reservation = row["data"]["reservation_id"]
                usage = data["usage"]
                if row["data"]["cleanup"] and usage is not None:
                    raise ProjectError(
                        422,
                        "invalid_request",
                        "Recovery cannot release unknown model spend",
                    )
                unknown = usage is None or usage["unknown_eur"] > 0
                amount = (
                    None if usage is None else usage["cost_eur"] + usage["unknown_eur"]
                )
                for resource in ("daily_model_eur", "monthly_model_eur"):
                    self.scope.reconcile(reservation, resource, amount)
                    if unknown:
                        self.scope.reconcile(reservation, resource, None)
                self.db.execute(
                    "SELECT atlas_projects.release_work_reservations(%s,%s,%s)",
                    (self.org, self.project, reservation),
                )
                value["usage"] = usage or dict(
                    value["usage"],
                    unknown_eur=row["data"]["reserved_eur"],
                    reserved_eur=0,
                )
                value["state"] = (
                    "cancelled"
                    if value["state"] == "cancelling"
                    else data["terminal_state"]
                )
                row["data"]["released"] = True
                self.invalidate(value["id"], "invalidated", data["request_id"])
        self.save_lease(value["id"], row)
        self.files.save(value)
        return dict(
            session=self.get(value["id"]),
            cleanup_required=value["state"] == "cancelling",
        )

    def holder_current(self, row: dict[str, Any]) -> bool:
        member = self.db.execute(
            "SELECT revision,status FROM atlas_projects.members WHERE org_id=%s AND id=%s",
            (self.org, row["holder_member_id"]),
        ).fetchone()
        try:
            grant = self.grant_stamp(str(row["holder_member_id"]))
        except ProjectError:
            return False
        return bool(
            self.scope.project is not None
            and self.scope.project["state"] == "active"
            and member
            and member["status"] == "active"
            and member["revision"] == row["data"]["membership_revision"]
            and grant == row["data"]["grant_stamp"]
            and self.scope.org["revision"] == row["data"]["policy_revision"]
        )

    def cancel(self, identifier: str, data: dict[str, Any]) -> dict[str, Any]:
        validate("CancelRequest", data)
        try:
            value = self.get(identifier)
        except ProjectError as exc:
            if exc.status != 404:
                raise
            row = self.db.execute(
                "SELECT atlas_projects.cancel_private_work(%s,%s,%s) AS result",
                (self.project, identifier, data["request_id"]),
            ).fetchone()
            if row is None or row["result"] == "not_found":
                raise exc
            if row["result"] != "ok":
                raise ProjectError(409, "session_busy")
            return dict(state="cancelling")
        if (
            value["initiator_member_id"] != self.scope.identity.member
            and self.scope.role != "owner"
            and self.scope.member["role"] != "admin"
        ):
            raise ProjectError(403, "permission_denied")
        row = self.lease(identifier)
        if (
            row
            and not row["data"].get("released")
            and str(row["request_id"]) != data["request_id"]
        ):
            raise ProjectError(409, "session_busy")
        value["state"] = (
            "cancelling" if row and not row["data"].get("released") else "cancelled"
        )
        self.files.save(value)
        self.invalidate(identifier, "invalidated", data["request_id"])
        return dict(state="cancelling")

    def effects(
        self,
        kind: str,
        effects: dict[str, Any],
        destination: dict[str, Any],
        maximum: int,
    ) -> tuple[list[str], list[dict[str, str]]]:
        if destination not in (
            {"kind": "server", "ref": self.project},
            {"kind": "server", "ref": None},
        ):
            raise ProjectError(403, "policy_denied")
        if kind not in {"overwrite", "delete"}:
            raise ProjectError(409, "feature_not_available")
        if "file_id" in effects:
            file = self.files.get("file", effects["file_id"])
            self.folder(file["folder_id"])
            if (
                effects["base_version"] != file["version_id"]
                or effects["revision"] != file["revision"]
            ):
                raise ProjectError(409, "revision_conflict", revision=file["revision"])
            if kind == "overwrite":
                if (
                    set(effects)
                    != {
                        "file_id",
                        "folder_id",
                        "path",
                        "base_version",
                        "revision",
                        "sha256",
                        "size_bytes",
                    }
                    or effects["path"] != file["relative_path"]
                    or effects["folder_id"] != file["folder_id"]
                    or effects["size_bytes"] > maximum
                ):
                    raise ProjectError(422, "invalid_request")
                self.scope.check_quota("stored_bytes", effects["size_bytes"])
            elif set(effects) != {"file_id", "base_version", "revision"}:
                raise ProjectError(422, "invalid_request")
            return [file["relative_path"]], [
                dict(file_id=file["id"], version_id=file["version_id"])
            ]
        if kind != "delete" or set(effects) != {"memory_ids", "memory_revision"}:
            raise ProjectError(422, "invalid_request")
        if effects["memory_revision"] != self.scope.project_data["memory_revision"]:
            raise ProjectError(409, "revision_conflict")
        for identifier in effects["memory_ids"]:
            self.files.get("memory", identifier)
        return [], []

    def approval(self, identifier: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM atlas_projects.file_approvals WHERE org_id=%s AND project_id=%s AND id=%s FOR UPDATE",
            (self.org, self.project, str(UUID(identifier))),
        ).fetchone()
        if row is None:
            raise ProjectError(404, "not_found")
        return row

    def audit(
        self, approval: dict[str, Any], request: str, phase: str, outcome: str
    ) -> dict[str, Any]:
        event_id = str(uuid4())
        actor = dict(kind="member", member_id=self.scope.identity.member, service=None)
        record = dict(
            self.files.new(),
            approval_id=approval["id"],
            request_id=request,
            actor=actor,
            phase=phase,
            action_digest=approval["action_digest"],
            timestamp=timestamp(),
            outcome=outcome,
            operation_id=None,
            ledger_event_id=event_id,
        )
        return append(
            self.scope,
            kind="approval_requested" if phase == "request" else "approval_decided",
            request_id=request,
            actor=actor,
            resource_refs=[],
            source=dict(kind="none", ref=None),
            destination=dict(kind="server", ref=self.project),
            byte_counts={key: 0 for key in BYTE_KEYS},
            outcome="intent"
            if outcome == "pending"
            else "denied"
            if outcome in {"denied", "expired"}
            else "succeeded",
            session_id=approval["session_id"],
            approval_id=approval["id"],
            audit_record=record,
            event_id=event_id,
            cleanup=phase == "outcome",
        )

    def issue(self, data: dict[str, Any]) -> dict[str, Any]:
        self.fence(data)
        self.grant_stamp(data["approver_member_id"])
        paths, versions = self.effects(
            data["action_kind"],
            data["effects"],
            data["destination"],
            data["maximum_bytes"],
        )
        value = dict(
            self.files.new(),
            session_id=data["session_id"],
            requested_by_member_id=self.scope.identity.member,
            approver_member_id=data["approver_member_id"],
            actor=dict(
                kind="member", member_id=self.scope.identity.member, service=None
            ),
            action_kind=data["action_kind"],
            action_digest=action_digest(
                self.scope, data["action_kind"], data["effects"]
            ),
            paths=paths,
            version_refs=versions,
            destination=data["destination"],
            maximum_bytes=data["maximum_bytes"],
            expires_at=iso(now() + timedelta(seconds=120)),
            decision="pending",
            consumed_at=None,
            policy_revision=self.scope.org["revision"],
        )
        execution = dict(
            request_id=data["request_id"],
            generation=data["generation"],
            effects=data["effects"],
            approver_grant=self.grant_stamp(data["approver_member_id"]),
        )
        self.db.execute(
            "INSERT INTO atlas_projects.file_approvals(org_id,project_id,id,data,execution) VALUES(%s,%s,%s,%s,%s)",
            (self.org, self.project, value["id"], Jsonb(value), Jsonb(execution)),
        )
        self.audit(value, data["request_id"], "request", "pending")
        session = self.get(data["session_id"])
        session["state"] = "awaiting_approval"
        self.files.save(session)
        return value

    def update_approval(self, value: dict[str, Any]) -> None:
        value["revision"] += 1
        value["updated_at"] = timestamp()
        self.db.execute(
            "UPDATE atlas_projects.file_approvals SET data=%s WHERE org_id=%s AND project_id=%s AND id=%s",
            (Jsonb(value), self.org, self.project, value["id"]),
        )

    def approval_current(self, row: dict[str, Any]) -> bool:
        value, execution = row["data"], row["execution"]
        if (
            not execution
            or "runner_command" in execution
            or "forge_operation" in execution
            or "repository_import" in execution
        ):
            return False
        lease = self.lease(value["session_id"])
        session = self.get(value["session_id"])
        if (
            lease is None
            or lease["data"].get("released")
            or lease["data"]["cleanup"]
            or lease["generation"] != execution["generation"]
            or str(lease["request_id"]) != execution["request_id"]
            or lease["expires_at"] <= now()
            or session["state"] == "cancelling"
            or not self.holder_current(lease)
        ):
            return False
        try:
            self.bases(session)
            self.effects(
                value["action_kind"],
                execution["effects"],
                value["destination"],
                value["maximum_bytes"],
            )
            return bool(
                self.grant_stamp(value["approver_member_id"])
                == execution["approver_grant"]
            )
        except ProjectError:
            return False

    def decide(self, identifier: str, data: dict[str, Any]) -> tuple[int, Any]:
        validate("ApprovalDecision", data)
        row = self.approval(identifier)
        if row["execution"] and "runner_command" in row["execution"]:
            from services.retrieval.project_runners import RemoteWork

            return RemoteWork(self.scope).decide(identifier, data)
        if row["execution"] and "repository_import" in row["execution"]:
            from services.retrieval.project_repository_import import RepositoryImports

            return RepositoryImports(self.scope).decide(identifier, data)
        if row["execution"] and "forge_operation" in row["execution"]:
            from services.retrieval.project_forge import RepositoryActions

            return RepositoryActions(self.scope).decide(identifier, data)
        value = row["data"]
        if value["approver_member_id"] != self.scope.identity.member:
            raise ProjectError(404, "not_found")
        request = row["execution"]["request_id"] if row["execution"] else str(uuid4())
        if (
            row["consumed_at"]
            or value["decision"] != "pending"
            or datetime.fromisoformat(value["expires_at"]) <= now()
            or not self.approval_current(row)
        ):
            if value["decision"] == "pending":
                value["decision"] = "expired"
                self.update_approval(value)
                self.audit(value, request, "outcome", "expired")
            error = ProjectError(410, "approval_expired_or_replayed")
            return error.status, error.body
        if data["action_digest"] != value["action_digest"]:
            self.audit(value, request, "decision", "denied")
            error = ProjectError(403, "approval_required")
            return error.status, error.body
        value["decision"] = "allowed" if data["allow"] else "denied"
        self.update_approval(value)
        self.audit(value, request, "decision", value["decision"])
        session = self.get(value["session_id"])
        session["state"] = "running"
        self.files.save(session)
        return 200, dict(accepted=True, consumed_at=None)

    def invalidate(self, session: str, decision: str, request: str) -> None:
        rows = self.db.execute(
            "SELECT data FROM atlas_projects.file_approvals WHERE org_id=%s AND project_id=%s AND data->>'session_id'=%s AND data->>'decision' IN ('pending','allowed') AND consumed_at IS NULL",
            (self.org, self.project, session),
        ).fetchall()
        for row in rows:
            value = row["data"]
            value["decision"] = decision
            self.update_approval(value)
            self.audit(value, request, "outcome", "denied")

    def events(self, data: dict[str, Any]) -> dict[str, Any]:
        value, _ = self.fence(data)
        events: list[dict[str, Any]] = []
        for item in data["progress"]:
            if (
                item["org_id"] != self.org
                or item["project_id"] != self.project
                or item["session_id"] != value["id"]
                or item["request_id"] != data["request_id"]
            ):
                raise ProjectError(404, "not_found")
            if item["approval_id"]:
                approval = self.approval(item["approval_id"])["data"]
                if approval["session_id"] != value["id"]:
                    raise ProjectError(404, "not_found")
            row = self.db.execute(
                "SELECT nextval(pg_get_serial_sequence('atlas_projects.ledger_events','seq')) AS seq"
            ).fetchone()
            assert row is not None
            event = dict(item, seq=row["seq"])
            self.db.execute(
                "INSERT INTO atlas_projects.work_progress VALUES(%s,%s,%s,%s,%s)",
                (self.org, self.project, value["id"], row["seq"], Jsonb(event)),
            )
            events.append(event)
            if event["type"] == "plan":
                value["plan"] = event["output"].get("items") or event["plan"]
            if event["type"] == "tool":
                append(
                    self.scope,
                    kind="tool_started"
                    if event["status"] == "running"
                    else "tool_failed"
                    if event["status"] == "failed"
                    else "tool_completed",
                    request_id=data["request_id"],
                    actor=dict(
                        kind="member",
                        member_id=self.scope.identity.member,
                        service=None,
                    ),
                    resource_refs=[],
                    source=dict(kind="server", ref=None),
                    destination=dict(kind="server", ref=None),
                    byte_counts={k: 0 for k in BYTE_KEYS},
                    outcome="intent"
                    if event["status"] == "running"
                    else "failed"
                    if event["status"] == "failed"
                    else "succeeded",
                    session_id=value["id"],
                )
        for record in data["audit"]:
            if (
                record["org_id"] != self.org
                or record["project_id"] != self.project
                or record["request_id"] != data["request_id"]
                or record["actor"]
                != dict(
                    kind="member", member_id=self.scope.identity.member, service=None
                )
            ):
                raise ProjectError(404, "not_found")
            approval = (
                self.approval(record["approval_id"])["data"]
                if record["approval_id"]
                else None
            )
            if (
                approval is None
                or approval["session_id"] != value["id"]
                or approval["action_digest"] != record["action_digest"]
            ):
                raise ProjectError(404, "not_found")
            events.append(
                self.audit(
                    approval, data["request_id"], record["phase"], record["outcome"]
                )
            )
        if events:
            latest = self.db.execute(
                "SELECT coalesce(max(seq),0) AS seq FROM atlas_projects.ledger_events WHERE org_id=%s AND project_id=%s AND data->>'session_id'=%s",
                (self.org, self.project, value["id"]),
            ).fetchone()
            value["last_event_seq"] = max(
                max(item["seq"] for item in events), latest["seq"] if latest else 0
            )
            self.files.save(value)
        return dict(events=events, last_event_seq=value["last_event_seq"])

    def organize(self, data: dict[str, Any]) -> dict[str, Any]:
        """Validate the entire batch before moves, inside the caller's transaction."""
        from services.retrieval.project_file_inputs import relative_path

        session, _ = self.fence(data)
        selected = self.files.row("work_session", session["id"])["private"][
            "folder_ids"
        ]
        bases = {
            item["file_id"]: item["version_id"]
            for item in session["base_file_versions"]
        }
        operations = []
        selectors: set[str] = set()
        destinations: set[tuple[str, str]] = set()
        for item in data["operations"]:
            item = dict(item)
            for field in ("file_id", "folder_id", "base_version"):
                item[field] = str(UUID(item[field]))
            item["path"] = relative_path(item["path"])
            destination = (item["folder_id"], item["path"])
            if item["file_id"] in selectors or destination in destinations:
                raise ProjectError(
                    422, "invalid_request", "Duplicate selector or destination"
                )
            selectors.add(item["file_id"])
            destinations.add(destination)
            operations.append(item)
        for item in operations:
            file = self.files.get("file", item["file_id"])
            if (
                bases.get(file["id"]) != item["base_version"]
                or file["folder_id"] not in selected
                or item["folder_id"] not in selected
            ):
                raise ProjectError(404, "not_found")
            self.folder(file["folder_id"])
            self.folder(item["folder_id"])
            self.files.move_preconditions(
                item["file_id"], item, f'"{item["expected_revision"]}"'
            )
        result = []
        if data["mode"] == "apply":
            for item in operations:
                result.append(
                    self.files.move(
                        item["file_id"], item, f'"{item["expected_revision"]}"'
                    )
                )
            # Paths participate in contexts even when model-use policy is unchanged.
            # Invalidate once for the batch, atomically with moves and their outbox.
            self.files.memory_revision()
        return dict(mode=data["mode"], operations=operations, files=result)
