"""PostgreSQL task-B store. All application work runs in an authorized transaction."""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from services.retrieval.project_identity import Identity, ProjectError
from services.retrieval.project_policy import intersect, quota

DB = psycopg.Connection[dict[str, Any]]


def entity(member: str, org: str, identifier: str | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return dict(
        id=identifier or str(uuid4()),
        org_id=org,
        created_by_member_id=member,
        created_at=now,
        updated_at=now,
        revision=0,
        deleted_at=None,
        purge_at=None,
    )


def public(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = dict(row["data"])
    for key in ("revision", "role", "status", "state", "owner_member_id"):
        if key in row:
            result[key] = str(row[key]) if key == "owner_member_id" else row[key]
    return result


class ProjectPool:
    """A bounded one-connection pool; scope exists only inside a transaction.

    Serialization avoids connection affinity assumptions and caps local memory.
    Separate service processes can use independent pools; quota locks are in SQL.
    """

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.lock = threading.RLock()
        self.db: DB | None = None

    @contextmanager
    def connection(self) -> Iterator[DB]:
        with self.lock:
            if self.db is None or self.db.closed:
                self.db = psycopg.connect(
                    self.dsn, row_factory=dict_row, autocommit=True
                )
                role = self.db.execute(
                    "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
                ).fetchone()
                owner = self.db.execute(
                    "SELECT pg_has_role(current_user,nspowner,'MEMBER') AS owns FROM pg_namespace WHERE nspname='atlas_projects'"
                ).fetchone()
                if (
                    role is None
                    or role["rolsuper"]
                    or role["rolbypassrls"]
                    or owner is None
                    or owner["owns"]
                ):
                    self.db.close()
                    raise ProjectError(
                        503,
                        "dependency_unavailable",
                        "A non-owner RLS application role is required",
                    )
            with self.db.transaction():
                yield self.db

    def close(self) -> None:
        with self.lock:
            if self.db is not None:
                self.db.close()


class ScopedProjects:
    def __init__(self, pool: ProjectPool) -> None:
        self.pool = pool

    @contextmanager
    def scope(
        self, identity: Identity, org: str, project: str | None = None
    ) -> Iterator[Scope]:
        if identity.org != org or (
            identity.project is not None and identity.project != project
        ):
            raise ProjectError(404, "not_found")
        with self.pool.connection() as db:
            for key, value in (
                ("org", org),
                ("member", identity.member),
                ("subject", identity.subject),
                ("project", project or ""),
            ):
                db.execute("SELECT set_config(%s,%s,true)", ("atlas." + key, value))
            # Serialize policy, membership, grants and reservations across processes.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (org,))
            member = db.execute(
                "SELECT * FROM atlas_projects.members WHERE org_id=%s AND id=%s AND subject=%s AND status='active'",
                (org, identity.member, identity.subject),
            ).fetchone()
            organization = db.execute(
                "SELECT * FROM atlas_projects.organizations WHERE org_id=%s", (org,)
            ).fetchone()
            if (
                member is None
                or organization is None
                or member["revision"] != identity.membership_revision
                or organization["revision"] != identity.policy_revision
            ):
                raise ProjectError(404, "not_found")
            installation = db.execute(
                "SELECT ceilings FROM atlas_projects.installation"
            ).fetchone()
            if installation is None:
                raise ProjectError(503, "dependency_unavailable")
            scope = Scope(
                db,
                identity,
                member,
                organization,
                intersect(installation["ceilings"], organization["policy"]),
            )
            if project is not None:
                scope.project = db.execute(
                    "SELECT * FROM atlas_projects.projects WHERE org_id=%s AND id=%s",
                    (org, project),
                ).fetchone()
                grant = db.execute(
                    "SELECT role FROM atlas_projects.grants WHERE org_id=%s AND project_id=%s AND member_id=%s",
                    (org, project, identity.member),
                ).fetchone()
                if scope.project is None or grant is None:
                    raise ProjectError(404, "not_found")
                scope.role = grant["role"]
            try:
                yield scope
            except BaseException:
                for path in scope.rollback_files:
                    path.unlink(missing_ok=True)
                raise


class Scope:
    def __init__(
        self,
        db: DB,
        identity: Identity,
        member: dict[str, Any],
        organization: dict[str, Any],
        policy: dict[str, Any],
    ) -> None:
        self.db, self.identity, self.member = db, identity, member
        self.org, self.policy = organization, policy
        self.project: dict[str, Any] | None = None
        self.role = ""
        self.rollback_files: list[Path] = []

    @property
    def project_id(self) -> str:
        if self.project is None:
            raise ProjectError(404, "not_found")
        return str(self.project["id"])

    @property
    def project_data(self) -> dict[str, Any]:
        if self.project is None:
            raise ProjectError(404, "not_found")
        data: dict[str, Any] = self.project["data"]
        return data

    def admin(self) -> None:
        if self.member["role"] != "admin":
            raise ProjectError(403, "permission_denied")

    def authorize(self, method: str, tail: list[str]) -> None:
        if self.project is None:
            return
        owner = not tail or tail[0] in {"grants", "connectors"}
        chat = tail in (["conversations"], ["context"]) or (
            len(tail) == 3 and tail[0] == "conversations" and tail[2] == "share"
        )
        if method == "GET":
            return
        if (
            self.role == "viewer"
            and len(tail) == 3
            and tail[0] == "conversations"
            and tail[2] == "share"
        ):
            # A viewer's exception to read-only permissions is limited to their
            # own conversation. Resolve that authority before parsing input.
            own = self.db.execute(
                "SELECT id FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND kind='conversation' AND id=%s AND data->>'owner_member_id'=%s",
                (self.identity.org, self.project_id, tail[1], self.identity.member),
            ).fetchone()
            if own is None:
                raise ProjectError(403, "permission_denied")
        if owner and self.role != "owner":
            raise ProjectError(403, "permission_denied")
        cancel = len(tail) == 3 and tail[0] == "work-sessions" and tail[2] == "cancel"
        if not owner and not chat and self.role not in ("owner", "editor"):
            if not (cancel and self.member["role"] == "admin"):
                raise ProjectError(
                    403,
                    "permission_denied",
                    "Viewer role cannot edit or start/approve Work",
                )
        if (
            self.project["state"] != "active"
            and not chat
            and not (not tail and method == "PATCH")
        ):
            raise ProjectError(
                403, "policy_denied", "Archived projects permit read and Chat only"
            )

    @staticmethod
    def match(row: dict[str, Any] | None, expected: str | None) -> None:
        if expected is None:
            raise ProjectError(428, "precondition_required")
        revision = 0 if row is None else int(row["revision"])
        if expected != f'"{revision}"':
            raise ProjectError(409, "revision_conflict", revision=revision)

    def event(self, kind: str, project: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO atlas_projects.audit(org_id,id,member_id,project_id,kind) VALUES(%s,%s,%s,%s,%s)",
            (self.identity.org, str(uuid4()), self.identity.member, project, kind),
        )

    def usage(self, resource: str) -> float:
        if resource == "active_members":
            row = self.db.execute(
                "SELECT count(*) AS value FROM atlas_projects.members WHERE org_id=%s AND status IN ('active','invited')",
                (self.identity.org,),
            ).fetchone()
        elif resource == "active_projects":
            row = self.db.execute(
                "SELECT atlas_projects.project_count() AS value"
            ).fetchone()
        else:
            period = "day" if resource == "daily_model_eur" else "month"
            row = self.db.execute(
                """SELECT coalesce(sum(amount),0) AS value FROM atlas_projects.reservations
                WHERE org_id=%s AND resource=%s AND state<>'released'
                AND (%s NOT IN ('daily_model_eur','monthly_model_eur') OR created_at >= date_trunc(%s,now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')""",
                (self.identity.org, resource, resource, period),
            ).fetchone()
        return float(row["value"]) if row else 0

    def check_quota(self, resource: str, increment: float) -> None:
        quota(
            resource, self.usage(resource) + increment, self.policy["quotas"][resource]
        )

    def reserve(self, identifier: str, amounts: dict[str, float]) -> None:
        """Atomically reserve all resources; caller holds an authorized transaction.

        Unknown/failed dispatch keeps its reservation. Only verified reconciliation
        may settle or release it. Reusing an ID cannot silently double charge.
        """
        if self.project is None:
            raise ProjectError(404, "not_found")
        if not amounts:
            raise ProjectError(422, "invalid_request")
        import math

        for resource, amount in amounts.items():
            if (
                resource not in self.policy["quotas"]
                or resource in ("per_turn", "active_members", "active_projects")
                or not math.isfinite(amount)
                or amount < 0
            ):
                raise ProjectError(422, "invalid_request")
            self.check_quota(resource, amount)
        for resource, amount in amounts.items():
            rid = str(uuid5(UUID(identifier), resource))
            self.db.execute(
                "INSERT INTO atlas_projects.reservations(org_id,project_id,id,resource,amount,state) VALUES(%s,%s,%s,%s,%s,'reserved')",
                (self.identity.org, self.project["id"], rid, resource, amount),
            )

    def reconcile(self, identifier: str, resource: str, amount: float | None) -> None:
        from uuid import UUID, uuid5

        if self.project is None:
            raise ProjectError(404, "not_found")
        rid = str(uuid5(UUID(identifier), resource))
        row = self.db.execute(
            "SELECT * FROM atlas_projects.reservations WHERE org_id=%s AND project_id=%s AND id=%s FOR UPDATE",
            (self.identity.org, self.project["id"], rid),
        ).fetchone()
        if row is None:
            raise ProjectError(404, "not_found")
        if amount is None:
            self.db.execute(
                "UPDATE atlas_projects.reservations SET state='unknown' WHERE org_id=%s AND id=%s",
                (self.identity.org, rid),
            )
            return
        import math

        if not math.isfinite(amount) or amount < 0:
            raise ProjectError(422, "invalid_request")
        self.check_quota(resource, amount - float(row["amount"]))
        self.db.execute(
            "UPDATE atlas_projects.reservations SET amount=%s,state=%s WHERE org_id=%s AND id=%s",
            (amount, "used" if amount else "released", self.identity.org, rid),
        )

    def replay(
        self, method: str, path: str, key: str, body: bytes
    ) -> tuple[int, Any] | None:
        row = self.db.execute(
            "SELECT * FROM atlas_projects.requests WHERE org_id=%s AND member_id=%s AND method=%s AND path=%s AND key=%s",
            (self.identity.org, self.identity.member, method, path, key),
        ).fetchone()
        if row is None:
            return None
        if row["digest"] != hashlib.sha256(body).hexdigest():
            raise ProjectError(409, "idempotency_conflict")
        return row["status"], row["result"]

    def remember(
        self, method: str, path: str, key: str, body: bytes, result: tuple[int, Any]
    ) -> None:
        self.db.execute(
            "INSERT INTO atlas_projects.requests(org_id,member_id,method,path,key,digest,status,result,project_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                self.identity.org,
                self.identity.member,
                method,
                path,
                key,
                hashlib.sha256(body).hexdigest(),
                result[0],
                Jsonb(result[1]),
                self.project["id"] if self.project else None,
            ),
        )

    def member_write(
        self, member_id: str | None, data: dict[str, Any], expected: str | None
    ) -> dict[str, Any]:
        self.admin()
        org, actor = self.identity.org, self.identity.member
        if member_id is None:
            self.check_quota("active_members", 1)
            value = entity(actor, org)
            value.update(
                identity_subject=data["identity_subject"],
                display_name=data.get("display_name", "Member"),
                role=data["role"],
                status="invited",
                joined_at=None,
            )
            self.db.execute(
                "INSERT INTO atlas_projects.members(org_id,id,subject,role,status,data) VALUES(%s,%s,%s,%s,'invited',%s)",
                (
                    org,
                    value["id"],
                    data["identity_subject"],
                    data["role"],
                    Jsonb(value),
                ),
            )
        else:
            row = self.db.execute(
                "SELECT * FROM atlas_projects.members WHERE org_id=%s AND id=%s",
                (org, member_id),
            ).fetchone()
            if row is None:
                raise ProjectError(404, "not_found")
            self.match(row, expected)
            if data.get("status") == "revoked":
                stranded = self.db.execute(
                    "SELECT atlas_projects.stranded(%s) AS value", (member_id,)
                ).fetchone()
                if stranded and stranded["value"]:
                    raise ProjectError(
                        403,
                        "policy_denied",
                        "Transfer ownership before revoking the last active owner",
                    )
            if row["role"] == "admin" and (
                data.get("role") == "member" or data.get("status") == "revoked"
            ):
                admins = self.db.execute(
                    "SELECT count(*) AS count FROM atlas_projects.members WHERE org_id=%s AND role='admin' AND status='active' AND id<>%s",
                    (org, member_id),
                ).fetchone()
                if admins is None or admins["count"] == 0:
                    raise ProjectError(
                        403,
                        "policy_denied",
                        "Retain an active organization administrator",
                    )
            if data.get("status") == "active" and row["status"] == "revoked":
                self.check_quota("active_members", 1)
            value = public(row)
            value.update(data)
            value["revision"] += 1
            value["updated_at"] = entity(actor, org)["updated_at"]
            if value["status"] == "active" and value["joined_at"] is None:
                value["joined_at"] = value["updated_at"]
            self.db.execute(
                "UPDATE atlas_projects.members SET role=%s,status=%s,revision=%s,data=%s WHERE org_id=%s AND id=%s",
                (
                    value["role"],
                    value["status"],
                    value["revision"],
                    Jsonb(value),
                    org,
                    member_id,
                ),
            )
        self.event("membership_changed")
        return value

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        self.check_quota("active_projects", 1)
        org, actor = self.identity.org, self.identity.member
        value = entity(actor, org)
        value.update(
            name=data["name"],
            owner_member_id=actor,
            state="active",
            default_action=data["default_action"],
            instructions_file_id=str(uuid4()),
            memory_revision=0,
            deliverables_folder_id=str(uuid4()),
            retention_override=None,
            policy_revision=self.org["revision"],
        )
        self.db.execute(
            "INSERT INTO atlas_projects.projects(org_id,id,owner_member_id,state,data,instructions) VALUES(%s,%s,%s,'active',%s,%s)",
            (org, value["id"], actor, Jsonb(value), data["instructions"]),
        )
        grant = entity(actor, org)
        grant.update(project_id=value["id"], member_id=actor, role="owner", revision=1)
        self.db.execute(
            "INSERT INTO atlas_projects.grants VALUES(%s,%s,%s,'owner',1,%s)",
            (org, value["id"], actor, Jsonb(grant)),
        )
        # Initial instructions are real retained bytes, charged in the same commit.
        self.project = {
            "id": value["id"],
            "data": value,
            "state": "active",
            "revision": 0,
        }
        self.role = "owner"
        self.reserve(value["id"], {"stored_bytes": len(data["instructions"].encode())})
        from services.retrieval.project_files import ProjectFiles

        ProjectFiles(self).initialize(data["instructions"])
        self.event("project_created", value["id"])
        return value

    def grant_write(
        self, member_id: str, role: str | None, expected: str | None
    ) -> dict[str, Any] | None:
        if self.project is None or self.role != "owner":
            raise ProjectError(403, "permission_denied")
        org, project = self.identity.org, self.project["id"]
        target = self.db.execute(
            "SELECT * FROM atlas_projects.members WHERE org_id=%s AND id=%s AND status='active'",
            (org, member_id),
        ).fetchone()
        if target is None:
            raise ProjectError(404, "not_found")
        row = self.db.execute(
            "SELECT * FROM atlas_projects.grants WHERE org_id=%s AND project_id=%s AND member_id=%s",
            (org, project, member_id),
        ).fetchone()
        self.match(row, expected)
        if row and row["role"] == "owner" and role != "owner":
            owners = self.db.execute(
                "SELECT count(*) AS count FROM atlas_projects.grants g JOIN atlas_projects.members m ON m.org_id=g.org_id AND m.id=g.member_id WHERE g.org_id=%s AND g.project_id=%s AND g.role='owner' AND m.status='active' AND g.member_id<>%s",
                (org, project, member_id),
            ).fetchone()
            if not owners or owners["count"] == 0:
                raise ProjectError(
                    403, "policy_denied", "Retain at least one active project owner"
                )
        if (
            row
            and row["role"] == "owner"
            and role != "owner"
            and str(self.project["owner_member_id"]) == member_id
        ):
            successor = self.db.execute(
                "SELECT g.member_id FROM atlas_projects.grants g JOIN atlas_projects.members m ON m.org_id=g.org_id AND m.id=g.member_id WHERE g.org_id=%s AND g.project_id=%s AND g.role='owner' AND m.status='active' AND g.member_id<>%s ORDER BY g.member_id LIMIT 1",
                (org, project, member_id),
            ).fetchone()
            if successor is None:
                raise ProjectError(403, "policy_denied")
            self.db.execute(
                "UPDATE atlas_projects.projects SET owner_member_id=%s,revision=revision+1 WHERE org_id=%s AND id=%s",
                (successor["member_id"], org, project),
            )
        if role is None:
            if row is None:
                raise ProjectError(404, "not_found")
            self.db.execute(
                "DELETE FROM atlas_projects.grants WHERE org_id=%s AND project_id=%s AND member_id=%s",
                (org, project, member_id),
            )
            value = None
        else:
            value = public(row) if row else entity(self.identity.member, org)
            value.update(
                project_id=str(project),
                member_id=member_id,
                role=role,
                revision=(row["revision"] + 1 if row else 1),
            )
            self.db.execute(
                "INSERT INTO atlas_projects.grants VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(org_id,project_id,member_id) DO UPDATE SET role=excluded.role,revision=excluded.revision,data=excluded.data",
                (org, project, member_id, role, value["revision"], Jsonb(value)),
            )
        # An explicit admin grant is the only way admin content becomes visible.
        self.event(
            "administrative_access" if target["role"] == "admin" else "grant_changed",
            str(project),
        )
        return value
