"""REQ-SHARE-02/03: narrow sharing endpoints, without administrative content access."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import Field

from services.retrieval.project_files import ProjectFiles
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_policy import Closed
from services.retrieval.project_store import Scope


class Share(Closed):
    confirm: bool


class Connectors(Closed):
    connector_ids: list[UUID] = Field(max_length=100)


class Ownership(Closed):
    member_id: UUID


def execute(
    scope: Scope, method: str, tail: list[str], body: bytes, expected: str | None
) -> tuple[int, Any] | None:
    if scope.project is None:
        if not tail or tail[0] not in {"project-grants", "project-ownership"}:
            return None
        scope.admin()
        transfer = tail[0] == "project-ownership"
        if (transfer and (method != "POST" or len(tail) != 2)) or (
            not transfer and (method != "DELETE" or len(tail) != 3)
        ):
            raise ProjectError(404, "not_found")
        if expected is None:
            raise ProjectError(428, "precondition_required")
        if not expected.startswith('"') or not expected.endswith('"'):
            raise ProjectError(422, "invalid_request")
        revision = int(expected[1:-1])
        member = (
            Ownership.model_validate_json(body).member_id if transfer else UUID(tail[2])
        )
        row = scope.db.execute(
            "SELECT atlas_projects.manage_project_grant(%s,%s,%s,%s) AS result",
            (UUID(tail[1]), member, transfer, revision),
        ).fetchone()
        if row is None:
            raise ProjectError(503, "dependency_unavailable")
        code = row["result"]
        if code != "ok":
            if code == "last_owner":
                raise ProjectError(403, "policy_denied", "Transfer ownership first")
            raise ProjectError(
                {"permission_denied": 403, "not_found": 404, "revision_conflict": 409}[
                    code
                ],
                code,
            )
        return 204, None
    if tail == ["members"] and method == "GET":
        if scope.role != "owner":
            raise ProjectError(403, "permission_denied")
        rows = scope.db.execute(
            "SELECT id,data->>'display_name' AS display_name FROM atlas_projects.members WHERE org_id=%s AND status='active' ORDER BY id",
            (scope.identity.org,),
        ).fetchall()
        return 200, dict(
            items=[
                dict(id=str(r["id"]), display_name=r["display_name"] or "Member")
                for r in rows
            ],
            next_cursor=None,
        )
    if tail == ["connectors"]:
        if method == "GET":
            return 200, dict(connector_ids=scope.project_data.get("connector_ids", []))
        if method == "PUT":
            scope.match(scope.project, expected)
            ids = [str(v) for v in Connectors.model_validate_json(body).connector_ids]
            if len(set(ids)) != len(ids):
                raise ProjectError(422, "invalid_request")
            if not set(ids) <= set(scope.policy["connector_allowlist"]):
                raise ProjectError(403, "policy_denied")
            value = dict(scope.project_data, connector_ids=ids)
            scope.db.execute(
                "UPDATE atlas_projects.projects SET data=%s,revision=revision+1 WHERE org_id=%s AND id=%s",
                (Jsonb(value), scope.identity.org, scope.project_id),
            )
            scope.event("project_connectors_changed", scope.project_id)
            return 200, dict(connector_ids=ids)
    if len(tail) == 3 and tail[0] == "conversations" and tail[2] == "share":
        if method != "POST":
            raise ProjectError(404, "not_found")
        if not Share.model_validate_json(body).confirm:
            raise ProjectError(
                422,
                "invalid_request",
                "Confirm that all project members may read the entire conversation",
            )
        files = ProjectFiles(scope)
        conversation = files.get("conversation", tail[1])
        if conversation["owner_member_id"] != scope.identity.member:
            raise ProjectError(403, "permission_denied")
        scope.match(conversation, expected)
        conversation["visibility"] = "shared"
        files.save(conversation)
        scope.db.execute(
            "INSERT INTO atlas_projects.audit(org_id,id,member_id,project_id,kind,details) VALUES(%s,gen_random_uuid(),%s,%s,'conversation_shared',%s)",
            (
                scope.identity.org,
                scope.identity.member,
                scope.project_id,
                Jsonb(dict(conversation_id=conversation["id"], visibility="shared")),
            ),
        )
        return 200, conversation
    return None
