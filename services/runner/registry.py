"""Retrieval-owned target metadata and grants; contains no SSH credential bytes.

Imported by retrieval only. The runner process uses signed public projections,
never a connection to the project database.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from services.retrieval.project_files import timestamp
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_store import Scope, entity
from services.runner.contract import Target

Json = dict[str, Any]


class Registry:
    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self.org = scope.identity.org

    def get(self, kind: str, identifier: str) -> Json:
        if kind not in {"targets", "grants", "sessions", "commands"}:
            raise ValueError("invalid_runner_entity")
        args = [self.org, identifier]
        clause = ""
        if kind != "targets":
            args.append(self.scope.project_id)
            clause = " AND project_id=%s"
        row = self.scope.db.execute(
            "SELECT data FROM atlas_projects.runner_"
            + kind
            + " WHERE org_id=%s AND id=%s"
            + clause
            + ("" if kind == "targets" else " FOR UPDATE"),
            args,
        ).fetchone()
        if row is None:
            raise ProjectError(404, "not_found")
        return dict(row["data"])

    def save(self, kind: str, value: Json, create: bool = False) -> None:
        if kind not in {"targets", "grants", "sessions", "commands"}:
            raise ValueError("invalid_runner_entity")
        table = "atlas_projects.runner_" + kind
        if create:
            if kind == "targets":
                self.scope.db.execute(
                    "INSERT INTO " + table + " VALUES(%s,%s,%s)",
                    (self.org, value["id"], Jsonb(value)),
                )
            else:
                self.scope.db.execute(
                    "INSERT INTO " + table + " VALUES(%s,%s,%s,%s)",
                    (self.org, self.scope.project_id, value["id"], Jsonb(value)),
                )
        else:
            value["revision"] += 1
            value["updated_at"] = timestamp()
            args: list[Any] = [Jsonb(value), self.org, value["id"]]
            clause = ""
            if kind != "targets":
                args.append(self.scope.project_id)
                clause = " AND project_id=%s"
            self.scope.db.execute(
                "UPDATE " + table + " SET data=%s WHERE org_id=%s AND id=%s" + clause,
                args,
            )

    def new(self) -> Json:
        return dict(entity(self.scope.identity.member, self.org), id=str(uuid4()))

    def target(
        self, data: Json, identifier: str | None = None, expected: str | None = None
    ) -> Json:
        self.scope.admin()
        fields = Target.model_validate(data).model_dump()
        if fields["jump_target_id"]:
            jump = self.get("targets", fields["jump_target_id"])
            if jump.get("jump_target_id") or jump["id"] == identifier:
                raise ProjectError(
                    422, "invalid_request", "Only one registered jump is supported"
                )
        reused = self.scope.db.execute(
            "SELECT id FROM atlas_projects.runner_targets WHERE org_id=%s AND data->>'credential_ref'=%s AND id<>%s",
            (
                self.org,
                fields["credential_ref"],
                identifier or "00000000-0000-0000-0000-000000000000",
            ),
        ).fetchone()
        if reused:
            raise ProjectError(409, "credential_already_bound")
        value = self.get("targets", identifier) if identifier else self.new()
        if identifier:
            self.scope.match(value, expected)
            transport_fields = ("host", "port", "user", "work_root", "jump_target_id")
            if any(value[key] != fields[key] for key in transport_fields):
                usage = self.scope.db.execute(
                    "SELECT * FROM atlas_projects.runner_usage(%s)", (identifier,)
                ).fetchone()
                if usage and usage["target_sessions"]:
                    raise ProjectError(
                        409, "runner_sessions_must_close_before_moving_target"
                    )
        value.update(fields, status="unavailable", last_probe=None)
        self.save("targets", value, create=identifier is None)
        self.scope.event(
            "runner_target_registered"
            if identifier is None
            else "runner_target_changed"
        )
        return value

    def grant(self, target: str, enabled: bool) -> Json:
        self.scope.admin()
        self.get("targets", target)
        try:
            value = self.get("grants", target)
        except ProjectError as exc:
            if exc.status != 404:
                raise
            value = dict(
                self.new(),
                id=target,
                project_id=self.scope.project_id,
                target_id=target,
                granted_by=self.scope.identity.member,
                enabled=enabled,
                code_egress_approved_at=None,
                allow_rules=[],
            )
            self.save("grants", value, create=True)
        else:
            value.update(enabled=enabled, code_egress_approved_at=None, allow_rules=[])
            self.save("grants", value)
        self.scope.event("runner_grant_changed", self.scope.project_id)
        return value

    def granted(self, target: str) -> tuple[Json, Json]:
        grant = self.get("grants", target)
        if not grant["enabled"]:
            raise ProjectError(404, "not_found")
        return self.get("targets", target), grant

    def targets(self, model: bool = False, capability: str | None = None) -> list[Json]:
        if not model:
            self.scope.admin()
        rows = self.scope.db.execute(
            "SELECT data FROM atlas_projects.runner_targets WHERE org_id=%s ORDER BY id",
            (self.org,),
        ).fetchall()
        values = []
        for row in rows:
            value = dict(row["data"])
            if capability and capability not in value["capabilities"]:
                continue
            if model:
                try:
                    self.granted(value["id"])
                except ProjectError as exc:
                    if exc.status == 404:
                        continue
                    raise
                value = {
                    k: value[k]
                    for k in (
                        "id",
                        "name",
                        "os",
                        "arch",
                        "shell",
                        "capabilities",
                        "permission_mode",
                        "status",
                    )
                }
                value.update(profiles=[], devices=[])
            values.append(value)
        return values

    def resolve(self, name: str) -> str:
        values = [t for t in self.targets(model=True) if name in {t["id"], t["name"]}]
        if len(values) != 1:
            raise ProjectError(404, "not_found", "No unique granted target matches")
        return str(values[0]["id"])
