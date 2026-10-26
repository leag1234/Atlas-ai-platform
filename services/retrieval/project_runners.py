"""Organization administration and broker-only remote execution authority."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
import time
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from services.orchestrator.project_forge import snapshot_files
from services.retrieval.project_files import ProjectFiles, action_digest, timestamp
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_ledger import append, BYTE_KEYS
from services.retrieval.project_store import Scope
from services.retrieval.project_work import Work
from services.runner.registry import Registry
from services.runner.channel import call
from services.runner.contract import RemoteRun, RemoteFetch, RemoteStatus, ListTargets
from services.runner.policy import decision, prefix_rule

Json = dict[str, Any]
ACTIVE = {"running", "unknown"}


def admin_api(
    scope: Scope, method: str, tail: list[str], data: Json, expected: str | None
) -> tuple[int, Json]:
    registry = Registry(scope)
    if scope.project is None:
        scope.admin()
        if tail == ["runner-targets", "credentials"] and method == "POST" and not data:
            return 200, call("generate", dict(org_id=registry.org, admin=True))
        if tail == ["runner-targets"]:
            if method == "GET":
                targets = registry.targets()
                for target in targets:
                    auth = dict(org_id=registry.org, target=target, admin=True)
                    if target.get("jump_target_id"):
                        auth["jump"] = registry.get("targets", target["jump_target_id"])
                    try:
                        probe = call("probe-status", auth)
                    except ProjectError:
                        probe = dict(
                            status="unavailable",
                            error="runner_unavailable",
                            timestamp=timestamp(),
                        )
                    target.update(status=probe["status"], last_probe=probe)
                return 200, dict(items=targets, next_cursor=None)
            if method == "POST":
                return 201, registry.target(data)
        if len(tail) >= 2:
            target = registry.get("targets", str(UUID(tail[1])))
            if len(tail) == 2 and method == "PUT":
                return 200, registry.target(data, target["id"], expected)
            if tail[2:] == ["probe"] and method == "POST" and not data:
                auth = dict(org_id=registry.org, target=target, admin=True)
                if target.get("jump_target_id"):
                    auth["jump"] = registry.get("targets", target["jump_target_id"])
                try:
                    probe = call("probe", auth)
                except ProjectError as exc:
                    probe = dict(status="unavailable", error=exc.body["error"])
                target.update(
                    status=probe["status"],
                    last_probe=dict(
                        probe, timestamp=probe.get("timestamp", timestamp())
                    ),
                )
                registry.save("targets", target)
                return 200, target
    elif tail == ["runner-targets"] and method == "GET":
        return 200, dict(items=registry.targets(model=True), next_cursor=None)
    elif (
        len(tail) == 3
        and tail[0] == "runner-targets"
        and tail[2] == "commands"
        and method == "GET"
    ):
        scope.admin()
        registry.get("targets", str(UUID(tail[1])))
        rows = scope.db.execute(
            "SELECT data FROM atlas_projects.runner_commands WHERE org_id=%s AND project_id=%s AND data->>'target_id'=%s ORDER BY data->>'created_at' DESC LIMIT 100",
            (registry.org, scope.project_id, tail[1]),
        ).fetchall()
        fields = (
            "id",
            "member_id",
            "work_session_id",
            "target_id",
            "command",
            "command_digest",
            "code_digest",
            "decision",
            "state",
            "exit_code",
            "duration_s",
            "bytes_sent",
            "bytes_received",
            "log_file_id",
            "artifact_file_ids",
            "created_at",
        )
        scope.event("runner_audit_viewed", scope.project_id)
        return 200, dict(
            items=[{key: row["data"].get(key) for key in fields} for row in rows],
            next_cursor=None,
        )
    elif (
        len(tail) == 3
        and tail[0] == "runner-targets"
        and tail[2] == "grant"
        and method == "PUT"
    ):
        if set(data) != {"enabled"} or type(data["enabled"]) is not bool:
            raise ProjectError(422, "invalid_request")
        scope.admin()
        if not data["enabled"]:
            sessions = scope.db.execute(
                "SELECT data FROM atlas_projects.runner_sessions WHERE org_id=%s AND project_id=%s AND data->>'target_id'=%s AND data->>'state'<>'closed'",
                (registry.org, scope.project_id, tail[1]),
            ).fetchall()
            for row in sessions:
                close(scope, row["data"]["work_session_id"], tail[1])
        grant = registry.grant(str(UUID(tail[1])), data["enabled"])
        if not data["enabled"]:
            RemoteWork(scope).audit(
                "runner_code_egress_revoked", dict(target_id=tail[1])
            )
        return 200, grant
    raise ProjectError(404, "not_found")


class RemoteWork:
    def __init__(self, scope: Scope) -> None:
        self.scope, self.registry = scope, Registry(scope)
        self.work, self.files = Work(scope), ProjectFiles(scope)

    def audit(self, kind: str, command: Json, outcome: str = "succeeded") -> None:
        append(
            self.scope,
            kind=kind,
            request_id=str(command.get("request_id", self.registry.new()["id"])),
            actor=dict(
                kind="member", member_id=self.scope.identity.member, service=None
            ),
            resource_refs=[],
            source=dict(kind="server", ref=self.scope.project_id),
            destination=dict(kind="connector", ref=command["target_id"]),
            byte_counts={
                k: int(command.get("bytes_sent", 0))
                if k == "serialized_payload_bytes"
                else int(command.get("bytes_received", 0))
                if k == "response_bytes"
                else 0
                for k in BYTE_KEYS
            },
            outcome=outcome,
            session_id=command.get("work_session_id"),
            runner=dict(
                target_id=command["target_id"],
                command_digest=command.get("command_digest"),
                code_digest=command.get("code_digest"),
                decision=command.get("decision"),
                exit_code=command.get("exit_code"),
                duration_s=command.get("duration_s"),
                bytes_sent=int(command.get("bytes_sent", 0)),
                bytes_received=int(command.get("bytes_received", 0)),
            ),
        )
        # Full command text and decision are tenant-scoped runner_commands, not
        # copies of log content in the general ledger.

    def binding(self, target: Json, grant: Json) -> Json:
        return dict(
            target_revision=target["revision"],
            grant_revision=grant["revision"],
            policy_revision=self.scope.org["revision"],
            member_stamp=self.work.grant_stamp(self.scope.identity.member),
        )

    def authority(self, command: Json, require_binding: bool = False) -> Json:
        self.scope.authorize("POST", ["work-sessions"])
        target, grant = self.registry.granted(command["target_id"])
        session = self.registry.get("sessions", command["runner_session_id"])
        if command["member_id"] != self.scope.identity.member:
            raise ProjectError(404, "not_found")
        if require_binding and command["binding"] != self.binding(target, grant):
            raise ProjectError(403, "approval_required")
        auth = dict(
            org_id=self.registry.org,
            project_id=self.scope.project_id,
            target=target,
            grant=grant,
            session=session,
            command=command,
        )
        if command.get("approval_id"):
            auth["approval"] = self.work.approval(command["approval_id"])["data"]
        if target.get("jump_target_id"):
            auth["jump"] = self.registry.get("targets", target["jump_target_id"])
        return auth

    def session(self, target: Json, work_session: Json) -> Json:
        stale = self.scope.db.execute(
            "SELECT data FROM atlas_projects.runner_sessions WHERE org_id=%s AND project_id=%s AND data->>'state'<>'closed' AND (data->>'last_activity')::timestamptz < now()-interval '24 hours'",
            (self.registry.org, self.scope.project_id),
        ).fetchall()
        for row in stale:
            close(self.scope, row["data"]["work_session_id"], row["data"]["target_id"])
        rows = self.scope.db.execute(
            "SELECT data FROM atlas_projects.runner_sessions WHERE org_id=%s AND project_id=%s AND data->>'work_session_id'=%s AND data->>'target_id'=%s AND data->>'state'<>'closed' FOR UPDATE",
            (
                self.registry.org,
                self.scope.project_id,
                work_session["id"],
                target["id"],
            ),
        ).fetchall()
        if rows:
            value = dict(rows[0]["data"])
            value["last_activity"] = timestamp()
            self.registry.save("sessions", value)
            return value
        value = dict(
            self.registry.new(),
            work_session_id=work_session["id"],
            target_id=target["id"],
            state="pending",
            synced_digest=None,
            opened_at=timestamp(),
            last_activity=timestamp(),
            closed_at=None,
        )
        self.registry.save("sessions", value, create=True)
        self.audit("runner_session_opened", value)
        return value

    def prepare(self, data: Json, work_session: Json) -> Json:
        args = RemoteRun.model_validate(data["arguments"]).model_dump()
        target_id = self.registry.resolve(args.pop("target"))
        target, grant = self.registry.granted(target_id)
        probe_time = (target.get("last_probe") or {}).get("timestamp")
        if (
            probe_time
            and (
                datetime.now(timezone.utc) - datetime.fromisoformat(probe_time)
            ).total_seconds()
            >= 900
        ):
            probe_auth = dict(org_id=self.registry.org, target=target, admin=True)
            if target.get("jump_target_id"):
                probe_auth["jump"] = self.registry.get(
                    "targets", target["jump_target_id"]
                )
            current_probe = call("probe", probe_auth)
            if current_probe["status"] != "available":
                raise ProjectError(
                    409,
                    "runner_unavailable",
                    str(current_probe.get("error", "Probe failed")),
                )
        if target["status"] != "available":
            raise ProjectError(409, "runner_unavailable", "Target probe is unavailable")
        # Current folder locality is checked at the storage boundary; snapshot
        # contents cannot label themselves as eligible for egress.
        for version in work_session["base_file_versions"]:
            file = self.files.get("file", version["file_id"])
            if file["locality"] == "local_only" and not target["same_locality"]:
                raise ProjectError(403, "runner_locality_denied")
        raw = base64.b64decode(data["snapshot_content"], validate=True)
        digest = hashlib.sha256(raw).hexdigest()
        snapshot_files(raw, digest)
        mode = decision(
            args["command"],
            target["permission_mode"],
            target["allow_rules"],
            target["deny_rules"],
            target["sensitive_rules"],
            grant["allow_rules"],
        )
        session = self.session(target, work_session)
        unresolved = self.scope.db.execute(
            "SELECT data FROM atlas_projects.runner_commands WHERE org_id=%s AND project_id=%s AND data->>'runner_session_id'=%s AND data->>'state'='unknown'",
            (self.registry.org, self.scope.project_id, session["id"]),
        ).fetchall()
        for row in unresolved:
            if self.refresh(dict(row["data"]))["state"] == "unknown":
                raise ProjectError(409, "runner_reconciliation_required")
        command = dict(
            self.registry.new(),
            **args,
            target_id=target_id,
            runner_session_id=session["id"],
            work_session_id=work_session["id"],
            request_id=data["request_id"],
            member_id=self.scope.identity.member,
            generation=data["generation"],
            code_digest=digest,
            decision=mode,
            state="denied" if mode == "denied" else "awaiting_approval",
            command_digest=hashlib.sha256(args["command"].encode()).hexdigest(),
            binding=self.binding(target, grant),
            exit_code=None,
            bytes_sent=0,
            bytes_received=0,
            log_file_id=None,
            artifact_file_ids=[],
            approval_id=None,
            snapshot=None,
        )
        if mode != "denied":
            folder = self.files.get(
                "folder", self.scope.project_data["deliverables_folder_id"]
            )
            snapshot = self.files.create_file(
                folder["id"],
                "runner-snapshot-" + command["id"] + ".json",
                raw,
                folder["revision"],
            )
            command["snapshot"] = dict(
                file_id=snapshot["id"], version_id=snapshot["version_id"]
            )
            if not grant["code_egress_approved_at"] or mode == "awaiting_approval":
                approval = dict(
                    self.files.new(),
                    session_id=work_session["id"],
                    requested_by_member_id=self.scope.identity.member,
                    approver_member_id=self.scope.identity.member,
                    actor=dict(
                        kind="member",
                        member_id=self.scope.identity.member,
                        service=None,
                    ),
                    action_kind="external_export",
                    action_digest=action_digest(self.scope, "remote_run", command),
                    paths=[],
                    version_refs=[command["snapshot"]],
                    destination=dict(kind="connector", ref=target_id),
                    maximum_bytes=len(raw),
                    expires_at=(datetime.now(timezone.utc) + timedelta(seconds=120))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    decision="pending",
                    consumed_at=None,
                    policy_revision=self.scope.org["revision"],
                    remote=dict(
                        target_id=target_id,
                        target_name=target["name"],
                        command=args["command"],
                        code_digest=digest,
                        code_egress=not bool(grant["code_egress_approved_at"]),
                        cwd=args["cwd"],
                        timeout_s=args["timeout_s"],
                    ),
                )
                self.scope.db.execute(
                    "INSERT INTO atlas_projects.file_approvals(org_id,project_id,id,data,execution) VALUES(%s,%s,%s,%s,%s)",
                    (
                        self.registry.org,
                        self.scope.project_id,
                        approval["id"],
                        Jsonb(approval),
                        Jsonb(dict(runner_command=command["id"])),
                    ),
                )
                command["approval_id"] = approval["id"]
                work_session["state"] = "awaiting_approval"
                self.files.save(work_session)
            else:
                command["state"] = "ready"
        self.registry.save("commands", command, create=True)
        self.audit(
            "runner_command_denied" if mode == "denied" else "runner_command_requested",
            command,
            "denied" if mode == "denied" else "intent",
        )
        return self.result(command)

    def result(self, command: Json) -> Json:
        value = {
            k: command.get(k)
            for k in (
                "state",
                "exit_code",
                "duration_s",
                "output",
                "bytes_sent",
                "bytes_received",
                "log_file_id",
                "artifact_file_ids",
                "code_digest",
                "reports",
                "error",
            )
        }
        value["command_id"] = command["id"]
        if command.get("approval_id"):
            value["approval"] = self.work.approval(command["approval_id"])["data"]
        return value

    def decide(self, identifier: str, data: Json) -> tuple[int, Json]:
        row = self.work.approval(identifier)
        approval = row["data"]
        command = self.registry.get("commands", row["execution"]["runner_command"])
        if approval["approver_member_id"] != self.scope.identity.member:
            raise ProjectError(404, "not_found")
        if (
            row["consumed_at"]
            or approval["decision"] != "pending"
            or datetime.fromisoformat(approval["expires_at"])
            <= datetime.now(timezone.utc)
        ):
            raise ProjectError(410, "approval_expired_or_replayed")
        self.work.fence(
            dict(
                session_id=command["work_session_id"],
                request_id=command["request_id"],
                generation=command["generation"],
            )
        )
        self.authority(command, require_binding=True)
        if data["action_digest"] != approval["action_digest"]:
            raise ProjectError(403, "approval_required")
        if data.get("always_allow") and not data["allow"]:
            raise ProjectError(422, "invalid_request")
        if data.get("always_allow"):
            # Confirmation is a browser-only decision; the model cannot supply it.
            approval["remote"]["project_rule"] = prefix_rule(command["command"])
        approval["decision"] = "allowed" if data["allow"] else "denied"
        self.work.update_approval(approval)
        self.audit(
            "runner_command_approved" if data["allow"] else "runner_command_denied",
            command,
            "succeeded" if data["allow"] else "denied",
        )
        return 200, dict(accepted=True, consumed_at=None)

    def consume(self, command: Json) -> Json:
        auth = self.authority(command, require_binding=True)
        if command["state"] not in {"ready", "awaiting_approval"}:
            raise ProjectError(410, "approval_expired_or_replayed")
        grant = auth["grant"]
        if command["approval_id"]:
            row = self.work.approval(command["approval_id"])
            approval = row["data"]
            if (
                row["consumed_at"]
                or approval["decision"] != "allowed"
                or datetime.fromisoformat(approval["expires_at"])
                <= datetime.now(timezone.utc)
            ):
                raise ProjectError(403, "approval_required")
            approval["consumed_at"] = timestamp()
            self.work.update_approval(approval)
            self.scope.db.execute(
                "UPDATE atlas_projects.file_approvals SET consumed_at=now() WHERE org_id=%s AND project_id=%s AND id=%s",
                (self.registry.org, self.scope.project_id, approval["id"]),
            )
            if not grant["code_egress_approved_at"]:
                grant["code_egress_approved_at"] = timestamp()
                self.audit("runner_code_egress_approved", command)
            rule = approval["remote"].get("project_rule")
            if rule and rule not in grant["allow_rules"]:
                grant["allow_rules"].append(rule)
            self.registry.save("grants", grant)
            command["decision"] = "approved"
        # Scope holds the organization advisory lock, so quota reservation is
        # atomic across projects and concurrent HTTP requests.
        rows = self.scope.db.execute(
            "SELECT data FROM atlas_projects.runner_commands WHERE org_id=%s AND data->>'state' IN ('running','unknown')",
            (self.registry.org,),
        ).fetchall()
        if (
            len(rows) >= 16
            or sum(r["data"]["target_id"] == command["target_id"] for r in rows)
            >= auth["target"]["max_parallel_sessions"]
        ):
            raise ProjectError(429, "runner_concurrency_limit")
        if any(
            r["data"]["runner_session_id"] == command["runner_session_id"] for r in rows
        ):
            raise ProjectError(409, "runner_reconciliation_required")
        usage = self.scope.db.execute(
            "SELECT * FROM atlas_projects.runner_usage(%s)", (command["target_id"],)
        ).fetchone()
        if usage is None:
            raise ProjectError(403, "permission_denied")
        session = auth["session"]
        opening = int(session["state"] != "open")
        if (
            usage["active_commands"] >= 16
            or usage["target_commands"] >= auth["target"]["max_parallel_sessions"]
            or usage["open_sessions"] + opening > 16
            or usage["target_sessions"] + opening
            > auth["target"]["max_parallel_sessions"]
        ):
            raise ProjectError(429, "runner_concurrency_limit")
        if (
            usage["reserved_seconds"] + command["timeout_s"]
            > auth["target"]["monthly_minutes"] * 60
            or usage["org_reserved_seconds"] + command["timeout_s"] > 3000 * 60
        ):
            raise ProjectError(429, "runner_minutes_limit")
        session["state"] = "open"
        self.registry.save("sessions", session)
        command.update(
            state="running", stage="queued", binding=self.binding(auth["target"], grant)
        )
        self.registry.save("commands", command)
        self.audit("runner_command_started", command, "intent")
        return self.result(command)

    def progress(self, command: Json) -> None:
        from services.retrieval.project_work_contract import validate
        import json

        digest = hashlib.sha256(
            str((command["state"], command.get("output", ""))).encode()
        ).hexdigest()
        if command.get("progress_digest") == digest:
            return
        event = dict(
            org_id=self.registry.org,
            project_id=self.scope.project_id,
            version=1,
            seq=1,
            session_id=command["work_session_id"],
            request_id=command["request_id"],
            turn_id=None,
            type="tool",
            status="running"
            if command["state"] in ACTIVE
            else "completed"
            if command["state"] == "succeeded"
            else "failed",
            label="Remote command " + command["id"][:8],
            tool="remote_status",
            plan=[],
            approval_id=command.get("approval_id"),
            job_id=None,
            text=None,
            error=None,
            output=dict(
                text=command.get("output", "").strip()
                or "Remote command " + command["state"],
                tool_result=json.dumps(self.result(command), ensure_ascii=False)[
                    :16000
                ],
            ),
        )
        validate("ProgressEvent", event)
        row = self.scope.db.execute(
            "SELECT nextval(pg_get_serial_sequence('atlas_projects.ledger_events','seq')) AS seq"
        ).fetchone()
        if row is None:
            raise ProjectError(503, "dependency_unavailable")
        event["seq"] = row["seq"]
        self.scope.db.execute(
            "INSERT INTO atlas_projects.work_progress VALUES(%s,%s,%s,%s,%s)",
            (
                self.registry.org,
                self.scope.project_id,
                command["work_session_id"],
                row["seq"],
                Jsonb(event),
            ),
        )
        command["progress_digest"] = digest
        self.registry.save("commands", command)

    def refresh(self, command: Json, cancel: bool = False) -> Json:
        auth = self.authority(command)
        if (
            command.get("stage") in {"queued", "sending"}
            and time.time() - command.get("launch_started_at", 0) < 60
            and not cancel
        ):
            return self.result(command)
        if command["state"] in ACTIVE:
            try:
                value = call("cancel" if cancel else "status", auth)
            except ProjectError:
                value = dict(state="unknown")
            command.update(value)
            self.progress(command)
            self.registry.save("commands", command)
            if command["state"] not in ACTIVE:
                self.audit(
                    "runner_command_cancelled"
                    if command["state"] == "cancelled"
                    else "runner_command_completed"
                    if command["state"] == "succeeded"
                    else "runner_command_failed",
                    command,
                    "succeeded" if command["state"] == "succeeded" else "failed",
                )
            elif command["state"] == "unknown":
                self.audit("runner_command_unknown", command, "unknown")
        if (
            command["state"] not in ACTIVE | {"awaiting_approval", "ready", "denied"}
            and not command["log_file_id"]
        ):
            if command.get("not_started"):
                log: Json = dict(reports=[])
                content = (
                    "Remote command not started: " + command["error"] + "\n"
                ).encode()
            else:
                log = call("log", self.authority(command))
                content = base64.b64decode(log["content"], validate=True)
            folder = self.files.get(
                "folder", self.scope.project_data["deliverables_folder_id"]
            )
            file = self.files.create_file(
                folder["id"],
                "runner-log-" + command["id"] + ".txt",
                content,
                folder["revision"],
            )
            command.update(
                log_file_id=file["id"],
                log_version_id=file["version_id"],
                reports=log.get("reports", []),
            )
            self.registry.save("commands", command)
        result = self.result(command)
        if command.get("log_file_id"):
            result["log_file"] = self.files.get("file", command["log_file_id"])
        return result

    def fetch(self, data: Json, work_session: Json) -> Json:
        args = RemoteFetch.model_validate(data["arguments"])
        target_id = self.registry.resolve(args.target)
        rows = self.scope.db.execute(
            "SELECT data FROM atlas_projects.runner_commands WHERE org_id=%s AND project_id=%s AND data->>'work_session_id'=%s AND data->>'target_id'=%s ORDER BY data->>'created_at' DESC LIMIT 1",
            (self.registry.org, self.scope.project_id, work_session["id"], target_id),
        ).fetchall()
        if not rows:
            raise ProjectError(404, "not_found")
        command = dict(rows[0]["data"])
        auth = self.authority(command)
        if not auth["grant"]["code_egress_approved_at"]:
            raise ProjectError(403, "approval_required")
        fetched = call("fetch", auth, path_or_glob=args.path_or_glob)["files"]
        if (
            len(fetched) > 20
            or sum(f["size_bytes"] for f in fetched) > 16 * 1024 * 1024
        ):
            raise ProjectError(413, "size_limit")
        files = []
        for item in fetched:
            content = base64.b64decode(item["content"], validate=True)
            if (
                len(content) != item["size_bytes"]
                or hashlib.sha256(content).hexdigest() != item["sha256"]
            ):
                raise ProjectError(409, "runner_artifact_integrity")
            folder = self.files.get(
                "folder", self.scope.project_data["deliverables_folder_id"]
            )
            from pathlib import PurePosixPath

            name = PurePosixPath(item["path"]).name
            file = self.files.create_file(
                folder["id"],
                "runner-" + self.registry.new()["id"] + "-" + name,
                content,
                folder["revision"],
            )
            command["artifact_file_ids"].append(file["id"])
            command["bytes_received"] += len(content)
            self.audit("runner_file_fetched", command)
            files.append(file)
        self.registry.save("commands", command)
        return dict(files=files, trust="untrusted")


def execute(scope: Scope, data: Json) -> Json:
    remote = RemoteWork(scope)
    operation = data["operation"]
    work_session, _ = remote.work.fence(data, cleanup=operation == "close")
    if operation == "list":
        listed = ListTargets.model_validate(data.get("arguments", {}))
        return dict(
            items=remote.registry.targets(model=True, capability=listed.capability)
        )
    if operation == "prepare":
        return remote.prepare(data, work_session)
    if operation == "fetch":
        return remote.fetch(data, work_session)
    if operation == "close":
        return close(scope, work_session["id"])
    args = RemoteStatus.model_validate(data["arguments"])
    command = remote.registry.get("commands", args.command_id)
    if command["work_session_id"] != work_session["id"]:
        raise ProjectError(404, "not_found")
    if operation == "execute":
        return remote.consume(command)
    if operation == "status":
        return remote.refresh(command, args.cancel)
    raise ProjectError(422, "invalid_request")


def close(scope: Scope, work_session: str, target_id: str | None = None) -> Json:
    remote = RemoteWork(scope)
    rows = scope.db.execute(
        "SELECT data FROM atlas_projects.runner_sessions WHERE org_id=%s AND project_id=%s AND data->>'work_session_id'=%s AND data->>'state'<>'closed' AND (%s::text IS NULL OR data->>'target_id'=%s)",
        (scope.identity.org, scope.project_id, work_session, target_id, target_id),
    ).fetchall()
    for row in rows:
        session = dict(row["data"])
        commands = scope.db.execute(
            "SELECT data FROM atlas_projects.runner_commands WHERE org_id=%s AND project_id=%s AND data->>'runner_session_id'=%s",
            (scope.identity.org, scope.project_id, session["id"]),
        ).fetchall()
        if commands:
            # Persist every full log before deleting the remote directory. If
            # contact is ambiguous, retain the directory for reconciliation.
            settled = []
            for entry in commands:
                current = dict(entry["data"])
                if current["state"] in ACTIVE:
                    remote.refresh(current, cancel=True)
                    for _ in range(20):
                        if current["state"] not in ACTIVE:
                            break
                        time.sleep(0.1)
                        remote.refresh(current)
                    else:
                        raise ProjectError(409, "runner_cleanup_unknown")
                elif current["state"] not in {"awaiting_approval", "ready", "denied"}:
                    remote.refresh(current)
                settled.append(current)
            command = settled[-1]
            auth: Json = dict(
                org_id=scope.identity.org,
                project_id=scope.project_id,
                target=remote.registry.get("targets", session["target_id"]),
                grant=remote.registry.get("grants", session["target_id"]),
                session=session,
                command=command,
            )
            if auth["target"].get("jump_target_id"):
                auth["jump"] = remote.registry.get(
                    "targets", auth["target"]["jump_target_id"]
                )
            auth["commands"] = settled
            call("close", auth)
        session.update(state="closed", closed_at=timestamp())
        remote.registry.save("sessions", session)
        remote.audit("runner_session_closed", session)
    return dict(state="closed")


def run_command(store: Any, identity: Any, identifier: str) -> None:
    """Durable intent before SSH; ambiguous transport is reconciled, never retried."""
    with store.scope(identity, identity.org, identity.project) as scope:
        remote = RemoteWork(scope)
        command = remote.registry.get("commands", identifier)
        if command["state"] != "running" or command.get("stage") != "queued":
            return
        remote.work.fence(
            dict(
                session_id=command["work_session_id"],
                request_id=command["request_id"],
                generation=command["generation"],
            )
        )
        auth = remote.authority(command, require_binding=True)
        ref = command["snapshot"]
        _, content, _ = remote.files.content(ref["file_id"], ref["version_id"])
        command["stage"] = "sending"
        remote.registry.save("commands", command)
    try:
        value = call("run", auth, content=base64.b64encode(content).decode())
    except ProjectError as exc:
        error = str(exc.body["error"])
        known = error in {
            "approval_required",
            "host_key_fingerprint_mismatch",
            "credential_unavailable",
            "credential_fingerprint_mismatch",
            "runner_policy_denied",
            "runner_disk_quota",
        }
        value = dict(
            state="failed" if known else "unknown", error=error, not_started=known
        )
    with store.scope(identity, identity.org, identity.project) as scope:
        remote = RemoteWork(scope)
        command = remote.registry.get("commands", identifier)
        command.update(value, stage="launched")
        remote.registry.save("commands", command)
        if value.get("synced_digest"):
            session = remote.registry.get("sessions", command["runner_session_id"])
            session["synced_digest"] = value["synced_digest"]
            remote.registry.save("sessions", session)
        if command["state"] == "unknown":
            remote.audit("runner_command_unknown", command, "unknown")
    # Complete logs do not depend on whether the model chooses to poll again.
    # Current membership/policy is checked on each transaction; no new command
    # is issued here. Lost authority leaves a durable unknown operation.
    import time
    import logging
    import psycopg

    deadline = time.monotonic() + command["timeout_s"] + 15
    unknown_count = 0
    while time.monotonic() < deadline:
        try:
            with store.scope(identity, identity.org, identity.project) as scope:
                remote = RemoteWork(scope)
                current = remote.registry.get("commands", identifier)
                result = remote.refresh(current)
                if result["state"] not in ACTIVE:
                    return
                unknown_count = unknown_count + 1 if result["state"] == "unknown" else 0
                if unknown_count >= 3:
                    return
        except (ProjectError, psycopg.Error, OSError):
            logging.getLogger(__name__).info(
                "runner_reconciliation_required command=%s", identifier
            )
            return
        time.sleep(1)


def start_command(store: Any, identity: Any, identifier: str) -> None:
    import threading
    import logging
    import psycopg

    def guarded() -> None:
        try:
            run_command(store, identity, identifier)
        except (ProjectError, psycopg.Error, OSError, ValueError):
            logging.getLogger(__name__).warning(
                "runner_reconciliation_required command=%s", identifier
            )

    worker = threading.Thread(target=guarded, daemon=True)
    worker.start()
