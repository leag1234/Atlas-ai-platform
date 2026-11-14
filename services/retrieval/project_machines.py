"""Personal machines: a member declares, verifies and removes his own SSH targets.

Owner decision of 2026-11-11 (docs/18-remote-runners.md, section 13). No
administrator and no project grant is involved. A machine is a row of
`runner_targets` carrying `owner_member_id`; only that member reads it, here
and in row-level security. The private key stays in the runner broker.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from services.retrieval import project_runners
from services.retrieval.project_files import timestamp
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_store import Scope
from services.runner.machines import (
    FINGERPRINT,
    Announcement,
    Connection,
    Enrolment,
    Machine,
    MachinePatch,
    MachineVerify,
    install_command,
    valid_public_key,
    valid_work_root,
)
from services.runner.registry import Registry

Json = dict[str, Any]
# Each machine holds one key pair in the broker; a bound keeps that finite.
LIMIT = 20
PUBLIC = (
    "id",
    "org_id",
    "owner_member_id",
    "name",
    "os",
    "arch",
    "host",
    "port",
    "user",
    "work_root",
    "permission_mode",
    "status",
    "verified_at",
    "host_key_fingerprints",
    "credential_fingerprint",
    "public_key",
    "revision",
    "created_at",
    "updated_at",
)
PROBE = ("status", "error", "timestamp", "os", "arch", "free_bytes", "git")


# A one-time connection token is valid this long (seconds).
ENROL_S = 900


def view(target: Json) -> Json:
    """What the owner sees: never the credential reference, never a private key."""
    value = {key: target.get(key) for key in PUBLIC}
    probe = target.get("last_probe")
    value["last_probe"] = (
        {key: probe[key] for key in PROBE if key in probe} if probe else None
    )
    if target.get("transport") == "relay":
        value.update(
            transport="relay",
            connected=bool(target.get("relay_user")),
            enrolled=bool(target.get("relay_host_key")),
            connected_at=target.get("connected_at"),
        )
        return value
    value["install_command"] = install_command(
        target["user"], target["host"], target["port"], target["public_key"]
    )
    return value


class Machines:
    def __init__(self, scope: Scope) -> None:
        self.scope, self.registry = scope, Registry(scope)
        self.org, self.member = scope.identity.org, scope.identity.member

    def owned(self) -> list[Json]:
        rows = self.scope.db.execute(
            "SELECT data FROM atlas_projects.runner_targets WHERE org_id=%s AND owner_member_id=%s ORDER BY data->>'created_at', id",
            (self.org, self.member),
        ).fetchall()
        return [dict(row["data"]) for row in rows]

    def get(self, identifier: str) -> Json:
        row = self.scope.db.execute(
            "SELECT data FROM atlas_projects.runner_targets WHERE org_id=%s AND id=%s AND owner_member_id=%s FOR UPDATE",
            (self.org, identifier, self.member),
        ).fetchone()
        if row is None:
            # Absent and not-yours are the same answer.
            raise ProjectError(404, "not_found")
        return dict(row["data"])

    def permit(self, target: Json | None = None) -> Json:
        auth: Json = dict(org_id=self.org, personal=True, member_id=self.member)
        if target is not None:
            auth["target"] = target
        return auth

    def broker(self, operation: str, auth: Json, **data: Any) -> Json:
        try:
            return project_runners.call(operation, auth, **data)
        except ProjectError as exc:
            raise ProjectError(
                503,
                "dependency_unavailable",
                "Remote runner service: " + str(exc.body["error"]),
            ) from None

    def unique(self, name: str, except_id: str | None = None) -> None:
        if any(m["name"] == name and m["id"] != except_id for m in self.owned()):
            raise ProjectError(409, "runner_name_conflict")

    def declare(self, data: Json) -> Json:
        fields = Machine.model_validate(data).model_dump()
        if len(self.owned()) >= LIMIT:
            raise ProjectError(
                409,
                "runner_machine_limit",
                measurements=[dict(measured=LIMIT, limit=LIMIT, unit="entries")],
            )
        self.unique(fields["name"])
        generated = self.broker("generate", self.permit())
        try:
            reference = str(UUID(generated["credential_ref"]))
            public = valid_public_key(generated["public_key"])
            fingerprint = str(generated["fingerprint"])
        except (KeyError, ValueError):
            raise ProjectError(503, "dependency_unavailable") from None
        value = dict(
            self.registry.new(),
            **fields,
            arch="unknown",
            shell="posix",
            credential_ref=reference,
            credential_fingerprint=fingerprint,
            public_key=public,
            host_key_fingerprints=[],
            jump_target_id=None,
            capabilities=[],
            allow_rules=[],
            deny_rules=[],
            sensitive_rules=[],
            # A personal machine serves one user across several conversations at once.
            max_parallel_sessions=4,
            disk_quota_bytes=10 * 1024**3,
            monthly_minutes=3000,
            same_locality=False,
            owner_member_id=self.member,
            status="unverified",
            verified_at=None,
            last_probe=None,
        )
        if not FINGERPRINT.fullmatch(value["credential_fingerprint"]):
            raise ProjectError(503, "dependency_unavailable")
        self.registry.save("targets", value, create=True)
        self.scope.event("runner_machine_declared")
        return value

    def connect(self, data: Json, existing: Json | None = None) -> tuple[Json, str]:
        """A computer the member connects through the relay, and its one-time nonce.

        The nonce goes once into the token the member pastes on that computer;
        only its digest is kept. Connecting an enrolled computer again (another
        folder, a new host key) keeps its Atlas key and replaces its relay entry.
        """
        import hashlib
        import secrets
        from datetime import datetime, timedelta, timezone

        fields = Connection.model_validate(data).model_dump()
        nonce = secrets.token_urlsafe(32)
        expires = (
            (datetime.now(timezone.utc) + timedelta(seconds=ENROL_S))
            .isoformat()
            .replace("+00:00", "Z")
        )
        enrol = dict(
            digest=hashlib.sha256(nonce.encode()).hexdigest(), expires_at=expires
        )
        if existing is not None:
            if existing.get("transport") != "relay":
                raise ProjectError(409, "runner_name_conflict")
            existing["enrol"] = enrol
            self.registry.save("targets", existing)
            self.scope.event("runner_machine_connect_requested")
            return existing, nonce
        if len(self.owned()) >= LIMIT:
            raise ProjectError(
                409,
                "runner_machine_limit",
                measurements=[dict(measured=LIMIT, limit=LIMIT, unit="entries")],
            )
        self.unique(fields["name"])
        generated = self.broker("generate", self.permit())
        try:
            reference = str(UUID(generated["credential_ref"]))
            public = valid_public_key(generated["public_key"])
            fingerprint = str(generated["fingerprint"])
        except (KeyError, ValueError):
            raise ProjectError(503, "dependency_unavailable") from None
        if not FINGERPRINT.fullmatch(fingerprint):
            raise ProjectError(503, "dependency_unavailable")
        value = dict(
            self.registry.new(),
            name=fields["name"],
            transport="relay",
            host="relay",
            port=0,
            user=None,
            os="linux",
            arch="unknown",
            shell="posix",
            work_root=None,
            permission_mode=fields["permission_mode"],
            credential_ref=reference,
            credential_fingerprint=fingerprint,
            public_key=public,
            host_key_fingerprints=[],
            jump_target_id=None,
            capabilities=[],
            allow_rules=[],
            deny_rules=[],
            sensitive_rules=[],
            max_parallel_sessions=4,
            disk_quota_bytes=10 * 1024**3,
            monthly_minutes=3000,
            same_locality=False,
            owner_member_id=self.member,
            status="unverified",
            verified_at=None,
            last_probe=None,
            enrol=enrol,
            relay_host_key=None,
            relay_user=None,
            relay_announced_at=0,
            connected_at=None,
        )
        self.registry.save("targets", value, create=True)
        self.scope.event("runner_machine_connect_requested")
        return value, nonce

    def enrol(self, target: Json, data: Json) -> Json:
        """The computer presents the one-time nonce and its host key, once."""
        import hashlib
        import hmac
        from datetime import datetime, timezone

        fields = Enrolment.model_validate(data).model_dump()
        enrol = target.get("enrol") or {}
        if (
            target.get("transport") != "relay"
            or not enrol.get("digest")
            or datetime.fromisoformat(enrol["expires_at"]) <= datetime.now(timezone.utc)
            or not hmac.compare_digest(
                enrol["digest"], hashlib.sha256(fields["nonce"].encode()).hexdigest()
            )
        ):
            raise ProjectError(410, "connect_token_expired_or_used")
        target["enrol"] = None
        admitted = self.broker(
            "relay-admit", self.permit(target), host_key=fields["host_key"]
        )
        target.update(
            relay_host_key=" ".join(fields["host_key"].split()[:2]),
            user=fields["user"],
            os=fields["os"],
            arch=fields["arch"],
            work_root=fields["work_root"],
            relay_user=None,
            status="unverified" if not target.get("verified_at") else "unavailable",
        )
        self.registry.save("targets", target)
        self.scope.event("runner_machine_enrolled")
        return dict(
            guest_key=target["public_key"],
            relay_host_key=str(admitted.get("relay_host_key", "")),
        )

    def announce(self, target: Json, data: Json) -> Json:
        """The computer's signed word that its session started or ended."""
        fields = Announcement.model_validate(data).model_dump()
        if target.get("transport") != "relay" or not target.get("relay_host_key"):
            raise ProjectError(404, "not_found")
        found = self.broker("relay-verify", self.permit(target), **fields)
        if int(found["at"]) <= int(target.get("relay_announced_at") or 0):
            raise ProjectError(409, "announcement_replayed")
        target.update(relay_user=found["user"], relay_announced_at=int(found["at"]))
        if found["user"] is None:
            target["status"] = (
                "unavailable" if target.get("verified_at") else "unverified"
            )
            self.registry.save("targets", target)
            self.scope.event("runner_machine_disconnected")
            return dict(connected=False)
        target["connected_at"] = timestamp()
        self.registry.save("targets", target)
        self.scope.event("runner_machine_connected")
        checked = self.verify(target, {})
        return dict(connected=True, status=checked["status"])

    def verify(self, target: Json, data: Json) -> Json:
        reset = MachineVerify.model_validate(data).reset_host_key
        if target.get("transport") == "relay":
            return self.verify_connected(target)
        try:
            if reset or not target["host_key_fingerprints"]:
                # Trust on first use, by the owner's explicit request only. A
                # later change of host key fails closed until he resets it.
                seen = project_runners.call("scan", self.permit(target))["fingerprints"]
                if (
                    not isinstance(seen, list)
                    or not 1 <= len(seen) <= 8
                    or any(
                        not isinstance(item, str) or not FINGERPRINT.fullmatch(item)
                        for item in seen
                    )
                ):
                    raise ProjectError(409, "runner_host_key_unavailable")
                target["host_key_fingerprints"] = seen
                target["host_key_pinned_at"] = timestamp()
                if reset:
                    self.scope.event("runner_machine_host_key_reset")
            probe = project_runners.call("probe", self.permit(target))
            if probe["status"] == "available" and not target["work_root"]:
                target["work_root"] = valid_work_root(str(probe.get("work_root", "")))
        except ProjectError as exc:
            probe = dict(status="unavailable", error=str(exc.body["error"]))
        except ValueError:
            probe = dict(status="unavailable", error="invalid_work_root")
        if probe["status"] == "available":
            target.update(
                status="available",
                arch=str(probe.get("arch", target["arch"]))[:100],
                verified_at=target["verified_at"] or timestamp(),
            )
        else:
            target["status"] = "unavailable" if target["verified_at"] else "unverified"
        target["last_probe"] = dict(
            probe, timestamp=probe.get("timestamp", timestamp())
        )
        self.registry.save("targets", target)
        self.scope.event("runner_machine_verified")
        return target

    def verify_connected(self, target: Json) -> Json:
        """A connected computer is checked through the relay; nothing to pin."""
        if not target.get("relay_user"):
            probe: Json = dict(status="unavailable", error="machine_not_connected")
        else:
            try:
                probe = project_runners.call("probe", self.permit(target))
            except ProjectError as exc:
                probe = dict(status="unavailable", error=str(exc.body["error"]))
        if probe["status"] == "available":
            target.update(
                status="available",
                arch=str(probe.get("arch", target["arch"]))[:100],
                verified_at=target["verified_at"] or timestamp(),
            )
        else:
            target["status"] = "unavailable" if target["verified_at"] else "unverified"
        target["last_probe"] = dict(
            probe, timestamp=probe.get("timestamp", timestamp())
        )
        self.registry.save("targets", target)
        self.scope.event("runner_machine_verified")
        return target

    def change(self, target: Json, data: Json, expected: str | None) -> Json:
        fields = MachinePatch.model_validate(data).model_dump(exclude_none=True)
        self.scope.match(target, expected)
        if "name" in fields:
            self.unique(fields["name"], target["id"])
        target.update(fields)
        # The new revision invalidates approvals issued under the old mode.
        self.registry.save("targets", target)
        self.scope.event("runner_machine_changed")
        return target

    def delete(self, target: Json, expected: str | None) -> Json:
        self.scope.match(target, expected)
        usage = self.scope.db.execute(
            "SELECT * FROM atlas_projects.runner_usage(%s)", (target["id"],)
        ).fetchone()
        if usage is None or usage["target_commands"]:
            raise ProjectError(
                409,
                "runner_commands_active",
                "Cancel the commands running on this machine first",
            )
        forgotten = self.broker("forget", self.permit(target))
        closed = Jsonb(dict(state="closed", closed_at=timestamp()))
        self.scope.db.execute(
            "UPDATE atlas_projects.runner_sessions SET data=data||%s WHERE org_id=%s AND data->>'target_id'=%s AND data->>'state'<>'closed'",
            (closed, self.org, target["id"]),
        )
        self.scope.db.execute(
            "DELETE FROM atlas_projects.runner_grants WHERE org_id=%s AND id=%s",
            (self.org, target["id"]),
        )
        self.scope.db.execute(
            "DELETE FROM atlas_projects.runner_targets WHERE org_id=%s AND id=%s AND owner_member_id=%s",
            (self.org, target["id"], self.member),
        )
        self.scope.event("runner_machine_deleted")
        return dict(
            id=target["id"],
            deleted=True,
            remote_cleanup=bool(forgotten.get("remote_cleanup")),
        )


def api(
    scope: Scope, method: str, tail: list[str], data: Json, expected: str | None
) -> tuple[int, Json]:
    if scope.project is not None or not tail or tail[0] != "machines":
        raise ProjectError(404, "not_found")
    machines = Machines(scope)
    if len(tail) == 1:
        if method == "GET":
            return 200, dict(
                items=[view(item) for item in machines.owned()], next_cursor=None
            )
        if method == "POST":
            if data.get("transport") == "relay":
                same = [m for m in machines.owned() if m["name"] == data.get("name")]
                connected, nonce = machines.connect(data, same[0] if same else None)
                return 201, dict(view(connected), enrol_nonce=nonce)
            return 201, view(machines.declare(data))
        raise ProjectError(404, "not_found")
    try:
        identifier = str(UUID(tail[1]))
    except ValueError:
        raise ProjectError(404, "not_found") from None
    target = machines.get(identifier)
    if len(tail) == 2:
        if method == "GET":
            return 200, view(target)
        if method == "PATCH":
            return 200, view(machines.change(target, data, expected))
        if method == "DELETE":
            if data:
                raise ProjectError(422, "invalid_request")
            return 200, machines.delete(target, expected)
    elif tail[2:] == ["verify"] and method == "POST":
        return 200, view(machines.verify(target, data))
    elif tail[2:] == ["enrol"] and method == "POST":
        return 200, machines.enrol(target, data)
    elif tail[2:] == ["announce"] and method == "POST":
        return 200, machines.announce(target, data)
    raise ProjectError(404, "not_found")
