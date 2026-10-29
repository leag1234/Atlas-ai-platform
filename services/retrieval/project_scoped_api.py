"""Task-B contract routes and fail-closed authorization for later project operations."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import logging
import json
import os
import time
from functools import lru_cache
from typing import Any
from uuid import UUID
from urllib.parse import parse_qs, urlsplit, urlencode

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.background import BackgroundTask

from services.orchestrator.project_forge import ForgeError, ForgeUnknown
from services.retrieval.project_forge import RepositoryActions, run_operation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.retrieval.project_identity import (
    Identity,
    ProjectError,
    authenticate_broker,
    sign,
    verify,
)
from services.retrieval.project_policy import (
    GrantPut,
    MemberCreate,
    MemberPatch,
    PolicyPatch,
    ProjectCreate,
    ProjectPatch,
    intersect,
)
from services.retrieval.project_store import (
    ProjectPool,
    Scope,
    ScopedProjects,
    entity,
    public,
)
from services.retrieval.project_pages import page
from services.retrieval.project_file_api import (
    execute_files,
    preflight,
    authorize_replay,
    publication_parts,
)
from services.retrieval.project_files import ProjectFiles
from services.retrieval.project_transcript import Transcript, canonical
from services.retrieval.project_work import Work
from services.retrieval.project_work_contract import validate
from services.retrieval.project_ledger import summary
from services.retrieval.project_locality import (
    p0_capabilities,
    p0_skill_references,
    policy_only_tightens,
)

router = APIRouter()


@lru_cache(maxsize=4)
def store(dsn: str) -> ScopedProjects:
    return ScopedProjects(ProjectPool(dsn))


def parse_path(path: str) -> tuple[str, str | None, list[str]]:
    parts = path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "orgs":
        raise ProjectError(404, "not_found")
    try:
        org = str(UUID(parts[1]))
        project = (
            str(UUID(parts[3])) if len(parts) >= 4 and parts[2] == "projects" else None
        )
        for item in parts[3:] if project is None else parts[4:]:
            if len(item) == 36:
                UUID(item)
    except ValueError:
        raise ProjectError(404, "not_found") from None
    return org, project, parts[4:] if project is not None else parts[2:]


def guard(scope: Scope, method: str, tail: list[str]) -> None:
    if scope.project is not None:
        scope.authorize(method, tail)
        if scope.member["role"] == "admin":
            scope.event("administrative_access", str(scope.project["id"]))
    elif tail and tail[0] in (
        "members",
        "policy",
        "project-grants",
        "project-ownership",
    ):
        scope.admin()


def execute(
    scope: Scope, method: str, tail: list[str], body: bytes, expected: str | None
) -> tuple[int, Any]:
    from services.retrieval.project_sharing import execute as sharing

    if scope.project is not None and tail and tail[0] == "scheduled-tasks":
        from services.retrieval.project_schedules import Schedules

        return Schedules(scope).execute(method, tail[1:], body, expected)
    shared_result = sharing(scope, method, tail, body, expected)
    if shared_result is not None:
        return shared_result
    org, member = scope.identity.org, scope.identity.member
    if scope.project is None:
        if tail == ["members"] and method == "POST":
            return 201, scope.member_write(
                None, MemberCreate.model_validate_json(body).model_dump(), expected
            )
        if len(tail) == 2 and tail[0] == "members" and method == "PATCH":
            data = MemberPatch.model_validate_json(body).model_dump(exclude_unset=True)
            require_patch(data)
            return 200, scope.member_write(tail[1], data, expected)
        if tail == ["policy"]:
            if method == "PATCH":
                scope.match(scope.org, expected)
                data = PolicyPatch.model_validate_json(body).model_dump(
                    exclude_unset=True
                )
                require_patch(data)
                candidate = {**scope.org["policy"], **data}
                ceiling = scope.db.execute(
                    "SELECT ceilings FROM atlas_projects.installation"
                ).fetchone()
                if ceiling is None:
                    raise ProjectError(503, "dependency_unavailable")
                if not policy_only_tightens(candidate, ceiling["ceilings"], intersect):
                    raise ProjectError(
                        403,
                        "policy_denied",
                        "Organization policy cannot weaken installation ceilings",
                    )
                scope.db.execute(
                    "UPDATE atlas_projects.organizations SET policy=%s,revision=revision+1 WHERE org_id=%s",
                    (Jsonb(candidate), org),
                )
                scope.policy = candidate
                scope.org["revision"] += 1
                scope.event("policy_changed")
            elif method != "GET":
                raise ProjectError(404, "not_found")
            usage = dict(
                tool_calls=0,
                model_calls=0,
                elapsed_seconds=0,
                cost_eur=scope.usage("monthly_model_eur"),
                reserved_eur=0,
                unknown_eur=0,
                stored_bytes=int(scope.usage("stored_bytes")),
                input_tokens=0,
                output_tokens=0,
                tokens_estimated=False,
            )
            for state, target in (
                ("reserved", "reserved_eur"),
                ("unknown", "unknown_eur"),
            ):
                row = scope.db.execute(
                    "SELECT coalesce(sum(amount),0) AS value FROM atlas_projects.reservations WHERE org_id=%s AND resource='monthly_model_eur' AND state=%s",
                    (org, state),
                ).fetchone()
                usage[target] = float(row["value"]) if row else 0
            return 200, dict(
                org_id=org,
                revision=scope.org["revision"],
                effective_policy=scope.policy,
                usage=usage,
            )
        if tail == ["projects"]:
            if method == "POST":
                return 201, scope.create(
                    ProjectCreate.model_validate_json(body).model_dump()
                )
            if method == "GET":
                rows = scope.db.execute(
                    "SELECT * FROM atlas_projects.projects WHERE org_id=%s ORDER BY data->>'created_at',id",
                    (org,),
                ).fetchall()
                return 200, {
                    "items": [public(row) for row in rows],
                    "next_cursor": None,
                }
    else:
        project = str(scope.project["id"])
        if not tail:
            if method == "GET":
                return 200, public(scope.project)
            if method == "PATCH":
                scope.match(scope.project, expected)
                data = ProjectPatch.model_validate_json(body).model_dump(
                    exclude_unset=True
                )
                require_patch(data)
                if data.get("state") == "active" and scope.project["state"] != "active":
                    scope.check_quota("active_projects", 1)
                if "pinned_memory_tokens" in data:
                    from services.retrieval.project_memory import check_cap

                    check_cap(ProjectFiles(scope), limit=data["pinned_memory_tokens"])
                value = public(scope.project)
                value.update(data)
                value["revision"] += 1
                value["updated_at"] = entity(member, org)["updated_at"]
                scope.db.execute(
                    "UPDATE atlas_projects.projects SET data=%s,state=%s,revision=revision+1 WHERE org_id=%s AND id=%s",
                    (
                        Jsonb({**scope.project_data, **value}),
                        value["state"],
                        org,
                        project,
                    ),
                )
                scope.event("project_changed", project)
                return 200, value
            if method == "DELETE":
                return 202, scope.delete_project(expected)
        if tail == ["grants"] and method == "GET":
            rows = scope.db.execute(
                "SELECT * FROM atlas_projects.grants WHERE org_id=%s AND project_id=%s ORDER BY data->>'created_at',member_id",
                (org, project),
            ).fetchall()
            return 200, {"items": [public(row) for row in rows], "next_cursor": None}
        if tail == ["skill-references"] and method == "GET":
            return 200, p0_skill_references()
        if len(tail) == 2 and tail[0] == "grants":
            if method == "PUT":
                return 200, scope.grant_write(
                    tail[1], GrantPut.model_validate_json(body).role, expected
                )
            if method == "DELETE":
                scope.grant_write(tail[1], None, expected)
                return 204, None
    if scope.project is not None and tail:
        transcript = Transcript(scope)
        if tail == ["conversations"]:
            if method == "POST":
                return 201, transcript.create(json.loads(body))
            if method == "GET":
                return 200, dict(
                    items=transcript.files.listing("conversation"), next_cursor=None
                )
        if tail == ["conversations", "import"] and method == "POST":
            return 201, transcript.import_conversation(json.loads(body))
        if (
            len(tail) == 3
            and tail[0] == "conversations"
            and tail[2] == "turns"
            and method == "GET"
        ):
            return 200, dict(items=transcript.turns(tail[1]), next_cursor=None)
        if tail == ["context"] and method == "POST":
            data = validate("ContextRequest", json.loads(body))
            scope.authorize(
                "POST",
                ["conversations"] if data["action"] == "chat" else ["work-sessions"],
            )
            return 200, transcript.context(
                data["conversation_id"], data["query"], [], data.get("work_session_id")
            )
        work = Work(scope)
        if tail in (["repositories"], ["repositories", "import-approvals"]):
            from services.retrieval.project_repository_import import RepositoryImports

            imports = RepositoryImports(scope)
            if method == "GET" and tail == ["repositories"]:
                return 200, dict(
                    items=imports.files.listing("repository"), next_cursor=None
                )
            if method == "POST":
                try:
                    return 201, imports.issue(json.loads(body)) if len(
                        tail
                    ) == 2 else imports.create(json.loads(body))
                except ForgeUnknown:
                    raise ProjectError(503, "dependency_unavailable") from None
                except ForgeError:
                    raise ProjectError(409, "revision_conflict") from None
        if (
            len(tail) == 3
            and tail[0] == "repositories"
            and tail[2] == "actions"
            and method == "POST"
        ):
            try:
                return RepositoryActions(scope).submit(tail[1], json.loads(body))
            except ForgeUnknown:
                raise ProjectError(503, "dependency_unavailable") from None
            except ForgeError:
                raise ProjectError(409, "revision_conflict") from None
        if tail == ["work-sessions"] and method == "POST":
            return 201, work.create(json.loads(body))
        if tail[0] == "work-sessions" and len(tail) == 2 and method == "GET":
            return 200, work.get(tail[1])
        if (
            tail[0] == "work-sessions"
            and len(tail) == 3
            and tail[2] == "cancel"
            and method == "POST"
        ):
            return 202, work.cancel(tail[1], json.loads(body))
        if (
            tail[0] == "approvals"
            and len(tail) == 3
            and tail[2] == "decision"
            and method == "POST"
        ):
            return work.decide(tail[1], json.loads(body))
    raise ProjectError(409, "feature_not_available")


def ledger_events(
    scope: Scope, session_id: str | None, after: int | None
) -> list[dict[str, Any]]:
    clauses = ["org_id=%s", "project_id=%s"]
    values: list[Any] = [scope.identity.org, scope.project_id]
    if session_id is not None:
        clauses.append("data->>'session_id'=%s")
        values.append(session_id)
    if after is not None:
        clauses.append("seq>%s")
        values.append(after)
    rows = scope.db.execute(
        "SELECT data FROM atlas_projects.ledger_events WHERE "
        + " AND ".join(clauses)
        + " ORDER BY seq",
        values,
    ).fetchall()
    return [dict(row["data"]) for row in rows]


def require_patch(data: dict[str, Any]) -> None:
    if not data or any(value is None for value in data.values()):
        raise ProjectError(422, "invalid_request")


def dispatch(
    identity: Identity, method: str, path: str, body: bytes, headers: dict[str, str]
) -> tuple[int, Any]:
    dsn = os.environ.get("ATLAS_PROJECT_DSN", "")
    if not dsn:
        raise ProjectError(
            503, "dependency_unavailable", "Project installation is not configured"
        )
    if path == "me" and method == "GET":
        with store(dsn).scope(identity, identity.org, identity.project) as scope:
            return 200, {
                "memberships": [
                    {
                        "org_id": identity.org,
                        "role": scope.member["role"],
                        "name": scope.org["data"]["name"],
                    }
                ],
                "capabilities": p0_capabilities(work=True),
            }
    parsed = urlsplit(path)
    query = parse_qs(parsed.query, keep_blank_values=True)
    org, project, tail = parse_path(parsed.path)
    # Canonical selectors prevent alternate spelling from escaping idempotency.
    segments = parsed.path.strip("/").split("/")
    for index, segment in enumerate(segments):
        if len(segment) == 36:
            try:
                segments[index] = str(UUID(segment))
            except ValueError:
                raise ProjectError(404, "not_found") from None
    path = "/".join(segments)
    with store(dsn).scope(identity, org, project) as scope:
        guard(scope, method, tail)
        if tail and tail[0] == "skills":
            from services.retrieval.project_skills import api, authorize

            authorize(scope, method, tail)
            if query:
                raise ProjectError(422, "invalid_request")
            key = headers.get("idempotency-key", "")
            if method != "GET":
                if not key or len(key) > 200:
                    raise ProjectError(
                        422, "invalid_request", "Idempotency-Key is required"
                    )
                replay = scope.replay(method, path, key, body)
                if replay is not None:
                    return replay
            skill_result = api(scope, method, tail, body, headers.get("if-match"))
            if method != "GET":
                scope.remember(method, path, key, body, skill_result)
            return skill_result
        if tail and tail[0] == "runner-targets":
            from services.retrieval.project_runners import admin_api

            if query:
                raise ProjectError(422, "invalid_request")
            data = json.loads(body) if body else {}
            key = headers.get("idempotency-key", "")
            if method != "GET":
                if not key or len(key) > 200:
                    raise ProjectError(422, "invalid_request")
                replay = scope.replay(method, path, key, body)
                if replay is not None:
                    if project is None:
                        scope.admin()
                    if tail == ["runner-targets", "credentials"]:
                        raise ProjectError(410, "public_key_already_shown")
                    return replay
            runner_result = admin_api(
                scope, method, tail, data, headers.get("if-match")
            )
            if method != "GET":
                remembered = (
                    runner_result[0],
                    {k: v for k, v in runner_result[1].items() if k != "public_key"},
                )
                scope.remember(method, path, key, body, remembered)
            return runner_result
        checked = check_query(method, tail, query)
        if checked:
            path += "?" + urlencode(sorted(checked.items()))
        preflight(scope, method, tail, checked) if project else None
        key = headers.get("idempotency-key", "")
        if method != "GET":
            if not key or len(key) > 200:
                raise ProjectError(
                    422, "invalid_request", "Idempotency-Key is required"
                )
            if tail and tail[0] == "approvals":
                return execute(scope, method, tail, body, headers.get("if-match"))
            if tail == ["repositories"]:
                return execute(scope, method, tail, body, headers.get("if-match"))
            if (
                len(tail) == 3
                and tail[0] == "repositories"
                and tail[2] == "actions"
                and "approval_id" in json.loads(body)
            ):
                return execute(scope, method, tail, body, headers.get("if-match"))
            if tail == ["context"]:
                context_request = validate("ContextRequest", json.loads(body))
                scope.authorize(
                    "POST",
                    ["conversations"]
                    if context_request["action"] == "chat"
                    else ["work-sessions"],
                )
            replay = scope.replay(method, path, key, body)
            if replay is not None:
                authorize_replay(scope, method, tail, replay[1])
                if tail == ["conversations"]:
                    ProjectFiles(scope).get("conversation", replay[1]["id"])
                if tail == ["context"]:
                    transcript = Transcript(scope)
                    context = replay[1]
                    versions = [context["instructions_version"]] + [
                        p["version_id"] for p in context["passages"]
                    ]
                    for fact in context["facts"]:
                        versions.extend(fact["source_file_version_ids"])
                    transcript.check_sources(versions)
                    for turn in context["history"]:
                        current = transcript.files.get("turn", turn["id"])
                        transcript.sources(current)
                    if (
                        context["memory_revision"]
                        != scope.project_data["memory_revision"]
                        or context["context_manifest"]["policy_revision"]
                        != scope.org["revision"]
                        or context["instructions_version"]
                        != transcript.files.instructions()["version"]
                    ):
                        raise ProjectError(409, "revision_conflict")
                return replay
        result: tuple[int, Any]
        if project and tail == ["events"] and method == "GET":
            after = checked.get("after", headers.get("last-event-id", "0"))
            if not after.isdigit() or (
                "after" in checked
                and "last-event-id" in headers
                and checked["after"] != headers["last-event-id"]
            ):
                raise ProjectError(422, "invalid_request")
            session_id = checked.get("session_id")
            if session_id:
                Work(scope).get(session_id)
            rows = scope.db.execute(
                "SELECT data FROM (SELECT seq,data FROM atlas_projects.ledger_events WHERE org_id=%s AND project_id=%s UNION ALL SELECT seq,data FROM atlas_projects.work_progress WHERE org_id=%s AND project_id=%s UNION ALL SELECT seq,data FROM atlas_projects.transcript_progress WHERE org_id=%s AND project_id=%s) e WHERE seq>%s AND (%s::text IS NULL OR data->>'session_id'=%s) ORDER BY seq LIMIT 100",
                (
                    org,
                    project,
                    org,
                    project,
                    org,
                    project,
                    int(after),
                    session_id,
                    session_id,
                ),
            ).fetchall()
            visible = []
            for row in rows:
                event = row["data"]
                if event.get("version") == 1:
                    try:
                        row_turn = scope.db.execute(
                            "SELECT data FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND kind='turn' AND data->>'request_id'=%s",
                            (org, project, event["request_id"]),
                        ).fetchone()
                        if row_turn is not None:
                            turn = ProjectFiles(scope).get(
                                "turn", row_turn["data"]["id"]
                            )
                            Transcript(scope).sources(turn)
                    except ProjectError as exc:
                        if exc.status in {403, 404, 409}:
                            continue
                        raise
                visible.append(row)
            content = "".join(
                "event: atlas.project\nid: "
                + str(row["data"]["seq"])
                + "\ndata: "
                + json.dumps(row["data"], separators=(",", ":"))
                + "\n\n"
                for row in visible
            ).encode()
            result = (200, (200, content, {"Content-Type": "text/event-stream"}))
        elif project and tail == ["ledger"] and method == "GET":
            result = (
                200,
                {
                    "items": ledger_events(
                        scope,
                        checked.get("session_id"),
                        int(checked["after"]) if "after" in checked else None,
                    ),
                    "next_cursor": None,
                },
            )
        elif project and tail == ["ledger", "summary"] and method == "GET":
            events = ledger_events(scope, checked.get("session_id"), None)
            files = ProjectFiles(scope)
            versions = files.listing("version")
            server_versions = {item["id"]: item["size_bytes"] for item in versions}
            produced_versions = {
                item["file_version_id"]: server_versions[item["file_version_id"]]
                for item in files.listing("deliverable")
                if item["file_version_id"] in server_versions
            }
            totals = summary(events, server_versions, produced_versions)
            latest = scope.db.execute(
                "SELECT coalesce(max(seq),0) AS seq FROM atlas_projects.ledger_events WHERE org_id=%s AND project_id=%s",
                (scope.identity.org, scope.project_id),
            ).fetchone()
            assert latest is not None
            totals["as_of_seq"] = latest["seq"]
            result = (200, totals)
        else:
            result = (
                execute_files(scope, method, tail, body, headers, checked)
                if project
                and tail
                and tail[0]
                in {
                    "folders",
                    "files",
                    "instructions",
                    "memory",
                    "deliverables",
                    "jobs",
                    "sources",
                }
                else execute(scope, method, tail, body, headers.get("if-match"))
            )
        if (
            method == "GET"
            and tail != ["skill-references"]
            and isinstance(result[1], dict)
            and "items" in result[1]
        ):
            result = (
                result[0],
                page(
                    scope,
                    parsed.path
                    + (
                        "?"
                        + urlencode(
                            sorted(
                                (k, v)
                                for k, v in checked.items()
                                if k not in {"limit", "cursor"}
                            )
                        )
                        if set(checked) - {"limit", "cursor"}
                        else ""
                    ),
                    result[1]["items"],
                    int(query.get("limit", ["50"])[0]),
                    query.get("cursor", [None])[0],
                    os.environ.get("ATLAS_PROJECT_SIGNING_KEY", "").encode(),
                ),
            )
        if (
            method == "GET"
            and tail == ["scheduled-tasks"]
            and isinstance(result[1], dict)
        ):
            result[1]["can_manage"] = scope.role in {"owner", "editor"}
        if method != "GET":
            scope.remember(method, path, key, body, result)
        return result


def check_query(
    method: str, tail: list[str], raw: dict[str, list[str]]
) -> dict[str, str]:
    allowed = {"limit", "cursor"} if method == "GET" else set()
    if tail == ["files"] and method in {"GET", "POST"}:
        allowed.add("folder_id")
    if (
        len(tail) == 3
        and tail[0] == "files"
        and tail[2] in {"content", "extraction"}
        and method == "GET"
    ):
        allowed.add("version")
    if method == "DELETE" and tail and tail[0] in {"files", "memory"}:
        allowed.add("approval_id")
    if tail == ["ledger"] and method == "GET":
        allowed.update({"session_id", "after"})
    if tail == ["events"] and method == "GET":
        allowed.add("after")
    if tail == ["ledger", "summary"] and method == "GET":
        allowed.add("session_id")
    if set(raw) - allowed or any(len(v) != 1 for v in raw.values()):
        raise ProjectError(422, "invalid_request")
    query = {k: v[0] for k, v in raw.items()}
    for key in {"folder_id", "version", "approval_id"} & set(query):
        query[key] = str(UUID(query[key]))
    if "session_id" in query:
        query["session_id"] = str(UUID(query["session_id"]))
    if "after" in query and (not query["after"].isdigit() or int(query["after"]) < 0):
        raise ProjectError(422, "invalid_request")
    return query


def ingress(identity: Identity, method: str, path: str) -> int:
    parsed = urlsplit(path)
    if parsed.path == "me":
        return 160000
    org, project, tail = parse_path(parsed.path)
    dsn = os.environ.get("ATLAS_PROJECT_DSN", "")
    if not dsn:
        raise ProjectError(503, "dependency_unavailable")
    with store(dsn).scope(identity, org, project) as scope:
        guard(scope, method, tail)
        query = check_query(
            method, tail, parse_qs(parsed.query, keep_blank_values=True)
        )
        if tail and tail[0] == "skills":
            from packages.skills import ZIP_BYTES
            from services.retrieval.project_skills import authorize

            authorize(scope, method, tail)
            return ZIP_BYTES if method == "POST" and project is None else 160000
        return preflight(scope, method, tail, query) if project else 160000


def index_active_memory(identity: Identity, identifier: str) -> None:
    if not os.environ.get("ATLAS_GATEWAY_URL"):
        return
    from packages.project_memory import pinned
    from services.retrieval.project_memory_search import search

    try:
        with store(os.environ.get("ATLAS_PROJECT_DSN", "")).scope(
            identity, identity.org, identity.project
        ) as scope:
            scope.authorize("POST", ["memory"])
            files = ProjectFiles(scope)
            entry = files.get("memory", identifier)
            Transcript(scope).check_sources(entry["source_file_version_ids"])
            if entry["status"] == "active" and not pinned(entry):
                search(files, entry["text"], [], [entry])
    except (ProjectError, psycopg.Error, OSError, ValueError):
        logging.getLogger(__name__).warning("project_memory_index_unavailable")


def index_created_upload(identity: Identity, file_id: str, version_id: str) -> None:
    from services.retrieval.project_index import index_upload

    try:
        index_upload(
            store(os.environ.get("ATLAS_PROJECT_DSN", "")),
            identity,
            file_id,
            version_id,
        )
    except (ProjectError, psycopg.Error, OSError, ValueError):
        # The upload is already durable. Restrictions/revocation prevent further
        # processing; never turn a committed upload into an ambiguous retry.
        logging.getLogger(__name__).warning("project_index_unavailable")


@router.api_route(
    "/api/atlas/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
)
async def projects(request: Request, path: str) -> Response:
    status = 500
    try:
        # This is a private service credential. Public cookies/proxy credentials
        # are deliberately not accepted by retrieval (the task-F BFF verifies them).
        identity = verify(
            request.headers.get("x-atlas-project-identity", ""),
            os.environ.get("ATLAS_PROJECT_SIGNING_KEY", "").encode(),
        )
        if (
            request.headers.get("origin") is not None
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            raise ProjectError(403, "origin_rejected")
        addressed = path + ("?" + request.url.query if request.url.query else "")
        limit = await asyncio.to_thread(ingress, identity, request.method, addressed)
        body = bytearray()
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                measured = len(body) + len(chunk)
                if measured > limit:
                    raise ProjectError(
                        413,
                        "size_limit",
                        f"request body: measured {measured} bytes; limit {limit} bytes",
                        measurements=[
                            dict(measured=measured, limit=limit, unit="bytes")
                        ],
                    )
                body.extend(chunk)
        status, result = await asyncio.to_thread(
            dispatch,
            identity,
            request.method,
            addressed,
            bytes(body),
            dict(request.headers),
        )
        upload_task = None
        if request.method == "POST" and status == 201 and isinstance(result, dict):
            value = result.get("file")
            if isinstance(value, dict) and "job_id" in result:
                upload_task = BackgroundTask(
                    index_created_upload,
                    replace(identity, project=parse_path(path)[1]),
                    value["id"],
                    value["version_id"],
                )
            elif path.endswith("/versions") and "file_id" in result:
                upload_task = BackgroundTask(
                    index_created_upload,
                    replace(identity, project=parse_path(path)[1]),
                    result["file_id"],
                    result["id"],
                )
        if (
            request.method in {"POST", "PATCH"}
            and isinstance(result, dict)
            and result.get("status") == "active"
            and "/memory" in path
        ):
            upload_task = BackgroundTask(
                index_active_memory,
                replace(identity, project=parse_path(path)[1]),
                result["id"],
            )
        headers = {"Cache-Control": "private, no-store"}
        if (
            isinstance(result, tuple)
            and len(result) == 3
            and isinstance(result[1], bytes)
        ):
            status, content, content_headers = result
            return Response(
                content, status_code=status, headers={**headers, **content_headers}
            )
        if isinstance(result, dict) and "revision" in result:
            headers["ETag"] = f'"{result["revision"]}"'
        return (
            Response(status_code=204, headers=headers)
            if status == 204
            else JSONResponse(
                result,
                status_code=status,
                headers=headers,
                background=(
                    BackgroundTask(
                        run_operation,
                        store(os.environ.get("ATLAS_PROJECT_DSN", "")),
                        replace(identity, project=parse_path(path)[1]),
                        result["operation_id"],
                    )
                    if isinstance(result, dict)
                    and result.get("state") == "running"
                    and "operation_id" in result
                    else upload_task
                ),
            )
        )
    except ProjectError as exc:
        status = exc.status
        return JSONResponse(exc.body, status_code=status)
    except (ValidationError, ValueError, psycopg.IntegrityError):
        status = 422
        return JSONResponse(
            ProjectError(status, "invalid_request").body, status_code=status
        )
    except (psycopg.Error, OSError, TimeoutError):
        status = 503
        return JSONResponse(
            ProjectError(status, "dependency_unavailable").body, status_code=status
        )
    finally:
        logging.getLogger(__name__).info(
            "project_scoped_operation method=%s status=%s", request.method, status
        )


class IdentityResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    subject: str = Field(min_length=1, max_length=320)
    org: UUID | None
    project: UUID | None


def resolve_identity(data: IdentityResolutionRequest) -> dict[str, Any]:
    if data.project is not None and data.org is None:
        raise ProjectError(422, "invalid_request")
    dsn = os.environ.get("ATLAS_PROJECT_DSN", "")
    if not dsn:
        raise ProjectError(503, "dependency_unavailable")
    repository = store(dsn)
    with repository.pool.connection() as db:
        rows = db.execute(
            "SELECT * FROM atlas_projects.identity_memberships(%s)", (data.subject,)
        ).fetchall()
    memberships = []
    for member in rows:
        discovery = Identity(
            data.subject,
            str(member["member_id"]),
            str(member["org_id"]),
            None,
            member["membership_revision"],
            member["policy_revision"],
            int(time.time()) + 60,
        )
        # Display names use the existing RLS-authorized organization read. No
        # bootstrap privilege, storage migration or caller authority is added.
        with repository.scope(discovery, discovery.org, None) as scope:
            memberships.append(
                dict(
                    org_id=discovery.org,
                    role=scope.member["role"],
                    name=scope.org["data"]["name"],
                )
            )
    result: dict[str, Any] = {
        "memberships": memberships,
        "identity": None,
        "expires": None,
    }
    if data.org is None:
        return result
    row = next((row for row in rows if str(row["org_id"]) == str(data.org)), None)
    if row is None:
        raise ProjectError(404, "not_found")
    identity = Identity(
        data.subject,
        str(row["member_id"]),
        str(data.org),
        str(data.project) if data.project else None,
        row["membership_revision"],
        row["policy_revision"],
        int(time.time()) + 60,
    )
    # A lookup race can only fail closed: scope checks current revisions and grant.
    with repository.scope(identity, identity.org, identity.project):
        result["identity"] = sign(
            identity, os.environ.get("ATLAS_PROJECT_SIGNING_KEY", "").encode()
        )
        result["expires"] = identity.expires
    return result


@router.post("/internal/atlas/identity")
async def identity_resolution(request: Request) -> Response:
    status = 500
    headers = {"Cache-Control": "private, no-store"}
    try:
        # A scoped resource credential cannot discover the broker interface.
        scoped = request.headers.get("x-atlas-project-identity")
        if scoped is not None:
            verify(scoped, os.environ.get("ATLAS_PROJECT_SIGNING_KEY", "").encode())
            raise ProjectError(404, "not_found")
        if not request.headers.get("x-atlas-identity-secret"):
            raise ProjectError(401, "unauthenticated")
        authenticate_broker(
            request.headers.get("x-atlas-identity-secret", ""),
            os.environ.get("ATLAS_PROJECT_IDENTITY_SECRET", ""),
        )
        if "origin" in request.headers or any(
            k.startswith("sec-fetch-") for k in request.headers
        ):
            raise ProjectError(403, "origin_rejected")
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 200:
            raise ProjectError(422, "invalid_request")
        body = bytearray()
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(body) + len(chunk) > 4096:
                    raise ProjectError(422, "invalid_request")
                body.extend(chunk)
        data = IdentityResolutionRequest.model_validate_json(bytes(body))
        result = await asyncio.to_thread(resolve_identity, data)
        status = 200
        return JSONResponse(result, headers=headers)
    except ProjectError as exc:
        status = exc.status
        return JSONResponse(exc.body, status_code=status, headers=headers)
    except (ValidationError, ValueError):
        status = 422
    except (psycopg.Error, OSError, TimeoutError):
        status = 503
    finally:
        logging.getLogger(__name__).info(
            "project_identity_resolution status=%s", status
        )
    return JSONResponse(
        ProjectError(
            status, "invalid_request" if status == 422 else "dependency_unavailable"
        ).body,
        status_code=status,
        headers=headers,
    )


EXECUTION_SCHEMAS = {
    "acquire": "ExecutionAcquire",
    "turn": "ExecutionTurn",
    "approvals": "ExecutionApprovalIssue",
    "decisions": "ExecutionApprovalDecision",
    "events": "ExecutionEvents",
    "organize": "ExecutionOrganize",
    "publish": "ExecutionPublish",
    "forge": "ExecutionForge",
    "remote": "RunnerExecution",
}


def execution_dispatch(
    identity: Identity, operation: str, body: bytes, key: str, content_type: str = ""
) -> tuple[int, Any]:
    if operation == "scheduled-publish":
        from services.retrieval.project_schedules import Schedules

        raw = json.loads(body)
        if (
            set(raw) != {"session_id", "request_id", "generation", "content"}
            or not isinstance(raw["content"], str)
            or not 0 < len(raw["content"]) <= 200000
        ):
            raise ProjectError(422, "invalid_request")
        with store(os.environ.get("ATLAS_PROJECT_DSN", "")).scope(
            identity, identity.org, identity.project
        ) as scope:
            Work(scope).fence(raw)
            replay = scope.replay(
                "POST", "/internal/atlas/execution/scheduled-publish", key, body
            )
            if replay is not None:
                return replay
            result = (
                200,
                Schedules(scope).publish_text(raw["session_id"], raw["content"]),
            )
            scope.remember(
                "POST", "/internal/atlas/execution/scheduled-publish", key, body, result
            )
            return result
    if operation not in EXECUTION_SCHEMAS:
        raise ProjectError(404, "not_found")
    package = publication_parts(body, content_type) if operation == "publish" else None
    raw = package[0] if package else json.loads(body)
    if operation == "organize" and isinstance(raw, dict):
        items = raw.get("operations")
        if isinstance(items, list) and len(items) > 100:
            raise ProjectError(
                413,
                "size_limit",
                f"Organization operations: measured {len(items)}; limit 100",
                measurements=[dict(measured=len(items), limit=100, unit="entries")],
            )
    data = validate(EXECUTION_SCHEMAS[operation], raw)
    if package is not None:
        # Multipart boundaries are transport framing, not the idempotent effect.
        body = json.dumps(
            dict(
                metadata=data,
                artifact_sha256=hashlib.sha256(package[1]).hexdigest(),
                preview_sha256=None
                if package[2] is None
                else hashlib.sha256(package[2]).hexdigest(),
                preview_mime=package[3],
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    if identity.project is None:
        raise ProjectError(404, "not_found")
    with store(os.environ.get("ATLAS_PROJECT_DSN", "")).scope(
        identity, identity.org, identity.project
    ) as scope:
        work = Work(scope)
        cleanup = operation == "turn" and data["operation"] in {
            "recover",
            "release",
            "check",
        }
        if cleanup:
            work.grant_stamp(identity.member)
        else:
            scope.authorize("POST", ["work-sessions"])
        if operation == "remote":
            from services.retrieval.project_runners import execute as execute_remote
            from services.runner.contract import Execution

            remote_data = Execution.model_validate(data).model_dump(exclude_none=True)
            path = "/internal/atlas/execution/remote"
            replay = scope.replay("POST", path, key, body)
            if replay is not None:
                work.fence(data)
                return replay
            result = (200, execute_remote(scope, remote_data))
            scope.remember("POST", path, key, body, result)
            return result
        if operation == "forge":
            from services.retrieval.project_work_forge import execute_forge

            return 200, execute_forge(scope, data)
        if operation == "decisions":
            return work.decide(data["approval_id"], data["decision"])
        if package is not None:
            work.fence(data)
        path = "/internal/atlas/execution/" + operation
        replay = scope.replay("POST", path, key, body)
        if replay is not None:
            if operation == "acquire":
                lease = replay[1]["session"]["lease"]
                work.fence(dict(data, generation=lease["generation"]))
            elif not (operation == "turn" and data["operation"] == "release"):
                work.fence(data, cleanup=cleanup)
            else:
                row = work.lease(data["session_id"])
                if (
                    row is None
                    or row["generation"] != data["generation"]
                    or str(row["request_id"]) != data["request_id"]
                    or str(row["holder_member_id"]) != identity.member
                ):
                    raise ProjectError(409, "session_busy")
            if operation == "publish":
                work.files.deliverable(replay[1]["id"])
            return replay
        if package is not None:
            result = (201, work.files.publish_deliverable(*package))
            scope.remember("POST", path, key, body, result)
            return result
        result = (
            200,
            {
                "acquire": work.acquire,
                "turn": work.turn,
                "approvals": work.issue,
                "events": work.events,
                "organize": work.organize,
            }[operation](data),
        )
        scope.remember("POST", path, key, body, result)
        return result


@router.post("/internal/atlas/transcript/{operation}")
@router.post("/internal/atlas/execution/{operation}")
async def execution_broker(request: Request, operation: str) -> Response:
    headers = {"Cache-Control": "private, no-store"}
    transcript = request.url.path.startswith("/internal/atlas/transcript/")
    try:
        credential = request.headers.get("x-atlas-project-identity", "")
        secret = request.headers.get("x-atlas-execution-secret", "")
        if not secret:
            if credential:
                verify(
                    credential, os.environ.get("ATLAS_PROJECT_SIGNING_KEY", "").encode()
                )
                raise ProjectError(404, "not_found")
            raise ProjectError(401, "unauthenticated")
        authenticate_broker(
            secret, os.environ.get("ATLAS_PROJECT_EXECUTION_SECRET", "")
        )
        if (
            request.headers.get("x-atlas-execution-audience")
            != "atlas-retrieval-execution"
        ):
            raise ProjectError(401, "unauthenticated")
        identity = verify(
            credential, os.environ.get("ATLAS_PROJECT_SIGNING_KEY", "").encode()
        )
        if "origin" in request.headers or any(
            k.startswith("sec-fetch-") for k in request.headers
        ):
            raise ProjectError(403, "origin_rejected")
        if identity.project is None:
            raise ProjectError(404, "not_found")
        key = request.headers.get("idempotency-key", "")
        if not key or len(key) > 200:
            raise ProjectError(422, "invalid_request")
        limit = await asyncio.to_thread(
            execution_ingress, identity, operation, transcript
        )
        body = bytearray()
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                measured = len(body) + len(chunk)
                if measured > limit:
                    raise ProjectError(
                        413,
                        "size_limit",
                        f"execution body: measured {measured} bytes; limit {limit} bytes",
                        measurements=[
                            dict(measured=measured, limit=limit, unit="bytes")
                        ],
                    )
                body.extend(chunk)
        status, result = await asyncio.to_thread(
            transcript_dispatch if transcript else execution_dispatch,
            identity,
            operation,
            bytes(body),
            key,
            request.headers.get("content-type", ""),
        )
        from services.retrieval.project_runners import (
            start_command as run_remote_command,
        )

        return JSONResponse(
            result,
            status_code=status,
            headers=headers,
            background=(
                BackgroundTask(
                    run_remote_command,
                    store(os.environ.get("ATLAS_PROJECT_DSN", "")),
                    identity,
                    result["command_id"],
                )
                if operation == "remote"
                and isinstance(result, dict)
                and result.get("state") == "running"
                and json.loads(body).get("operation") == "execute"
                else BackgroundTask(
                    run_operation,
                    store(os.environ.get("ATLAS_PROJECT_DSN", "")),
                    identity,
                    result["operation_id"],
                )
                if operation == "forge"
                and isinstance(result, dict)
                and result.get("state") == "running"
                else None
            ),
        )
    except ProjectError as exc:
        return JSONResponse(exc.body, status_code=exc.status, headers=headers)
    except (ValueError, TypeError, psycopg.IntegrityError):
        return JSONResponse(
            ProjectError(422, "invalid_request").body, status_code=422, headers=headers
        )
    except (psycopg.Error, OSError, TimeoutError):
        return JSONResponse(
            ProjectError(503, "dependency_unavailable").body,
            status_code=503,
            headers=headers,
        )


def execution_ingress(
    identity: Identity, operation: str = "", transcript: bool = False
) -> int:
    dsn = os.environ.get("ATLAS_PROJECT_DSN", "")
    if not dsn:
        raise ProjectError(503, "dependency_unavailable")
    with store(dsn).scope(identity, identity.org, identity.project) as scope:
        if transcript:
            scope.authorize("POST", ["conversations"])
            return 8388608 if operation == "result-put" else 160000
        if operation in {"forge", "remote"}:
            scope.authorize("POST", ["work-sessions"])
            return int(scope.policy["quotas"]["upload_bytes"]) * 2 + 160000
        if operation == "publish":
            scope.authorize("POST", ["work-sessions"])
            folder = scope.project_data["deliverables_folder_id"]
            ProjectFiles(scope).upload_folder(folder)
            Work(scope).folder(folder)
            return (
                int(scope.policy["quotas"]["upload_bytes"]) + 160000 + 1048576 + 65536
            )
        Work(scope).grant_stamp(identity.member)
        return 1200000 if operation == "scheduled-publish" else 160000


def transcript_dispatch(
    identity: Identity, operation: str, body: bytes, key: str, content_type: str = ""
) -> tuple[int, Any]:
    if operation == "schedule-propose":
        from services.retrieval.project_schedules import TaskInput, Schedules

        raw = json.loads(body)
        if set(raw) != {"conversation_id", "request_id", "task"}:
            raise ProjectError(422, "invalid_request")
        task = TaskInput.model_validate(raw["task"])
        with store(os.environ.get("ATLAS_PROJECT_DSN", "")).scope(
            identity, identity.org, identity.project
        ) as scope:
            turn, _ = Transcript(scope).bound(raw)
            if turn["state"] != "running":
                raise ProjectError(409, "session_busy")
            scope.authorize("POST", ["scheduled-tasks"])
            path = "/internal/atlas/transcript/schedule-propose/" + turn["id"]
            replay = scope.replay("POST", path, key, body)
            if replay is not None:
                return replay
            result = (
                201,
                Schedules(scope).create(
                    task, proposal=True, conversation=turn["conversation_id"]
                ),
            )
            scope.remember("POST", path, key, body, result)
            return result
    schemas = {
        "check": "SessionStateRead",
        "memory": "TranscriptMemory",
        "state-events": "TranscriptEvents",
        "state-read": "SessionStateRead",
        "state-write": "SessionStateWrite",
        "compact": "SessionCompaction",
        "begin": "TranscriptBegin",
        "finish": "TranscriptFinish",
        "events": "TranscriptEvents",
        "work-events": "WorkTranscriptEvents",
        "result-put": "TranscriptResultPut",
        "result-open": "TranscriptResultOpen",
    }
    if operation not in schemas or identity.project is None:
        raise ProjectError(404, "not_found")
    data = validate(schemas[operation], json.loads(body))
    with store(os.environ.get("ATLAS_PROJECT_DSN", "")).scope(
        identity, identity.org, identity.project
    ) as scope:
        transcript = Transcript(scope)
        if operation == "begin":
            return transcript.begin(data, key)
        bound_turn, bound_private = transcript.bound(data)
        if operation == "check":
            if bound_turn["state"] != "running":
                raise ProjectError(409, "session_busy", "Turn is no longer running")
            return 200, {"state": "running"}
        if operation in {"state-read", "state-write", "state-events", "compact"}:
            from services.retrieval.project_session_state import SessionStates

            states = SessionStates(transcript)
            if operation == "state-read":
                return 200, states.read(data)
            path = f"/internal/atlas/transcript/{operation}/{identity.project}"
            replay = scope.replay("POST", path, key, canonical(data))
            if replay is not None:
                return replay
            result = (
                200,
                states.write(data)
                if operation == "state-write"
                else states.compact(data)
                if operation == "compact"
                else transcript.events(data, state_update=True),
            )
            scope.remember("POST", path, key, canonical(data), result)
            return result
        if operation == "work-events":
            session_id = bound_private["request"].get("work_session_id")
            if not session_id:
                raise ProjectError(409, "session_busy")
            Work(scope).fence(
                dict(
                    session_id=session_id,
                    request_id=bound_turn["request_id"],
                    generation=data["generation"],
                )
            )
        references = None
        if operation in {"result-put", "result-open", "memory"}:
            from services.retrieval.project_results import ResultReferences

            references = ResultReferences(transcript)
            references.bound(data)
            if operation == "result-open":
                return 200, references.open(data)
        path = f"/internal/atlas/transcript/{operation}/{identity.project}"
        body = canonical(data)
        replay = scope.replay("POST", path, key, body)
        if replay is not None:
            if operation == "memory":
                for entry in replay[1]["entries"]:
                    current = transcript.files.get("memory", entry["id"])
                    transcript.check_sources(current["source_file_version_ids"])
            return replay
        from services.retrieval.project_memory import model_operation

        result = (
            200,
            model_operation(transcript, data)
            if operation == "memory"
            else references.put(data)
            if references is not None
            else transcript.finish(data)
            if operation == "finish"
            else transcript.events(data),
        )
        scope.remember("POST", path, key, body, result)
        return result
