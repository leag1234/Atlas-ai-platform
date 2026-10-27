"""REQ-PROJ-07: organization-owned packages and owner-selected project skills."""

from __future__ import annotations

import base64
from typing import Any

from psycopg.types.json import Jsonb
from pydantic import Field

from packages.skills import Skill, SkillError, builtins, cap, from_zip, parse, safe_path
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_policy import Closed
from services.retrieval.project_store import Scope


class Enabled(Closed):
    enabled: bool


class Selection(Closed):
    digests: list[str] = Field(max_length=64)


def catalog(scope: Scope) -> dict[str, Skill]:
    result = dict(builtins())
    for digest, entry in scope.org["data"].get("skills", {}).items():
        skill = parse(
            {p: base64.b64decode(v, validate=True) for p, v in entry["files"].items()}
        )
        if skill.digest != digest or skill.name in {s.name for s in result.values()}:
            raise ProjectError(503, "invalid_skill_catalog")
        result[digest] = skill
    return result


def allowed(scope: Scope, skills: dict[str, Skill]) -> dict[str, Skill]:
    enabled = scope.org["data"].get("skill_enabled", [])
    selected = scope.project_data.get("skill_digests", []) if scope.project else enabled
    return {
        d: s
        for d, s in skills.items()
        if d in enabled and d in selected and d in scope.policy["skill_allowlist"]
    }


def authorize(scope: Scope, method: str, tail: list[str]) -> None:
    if scope.project is None:
        if tail == ["skills"] or method != "GET":
            scope.admin()
    elif method != "GET" and scope.role != "owner":
        raise ProjectError(403, "permission_denied")


def save_org(scope: Scope, data: dict[str, Any]) -> None:
    scope.db.execute(
        "UPDATE atlas_projects.organizations SET data=%s,revision=revision+1 WHERE org_id=%s",
        (Jsonb(data), scope.identity.org),
    )
    scope.org["data"] = data
    scope.org["revision"] += 1
    scope.event("skill_catalog_changed")


def api(
    scope: Scope, method: str, tail: list[str], body: bytes, expected: str | None
) -> tuple[int, Any]:
    authorize(scope, method, tail)
    try:
        return execute(scope, method, tail, body, expected)
    except SkillError as exc:
        raise ProjectError(
            413 if exc.limit else 422,
            "skill_size_limit" if exc.limit else "invalid_skill",
            str(exc),
            measurements=[dict(measured=exc.measured, limit=exc.limit, unit=exc.unit)]
            if exc.limit
            else [],
        ) from None


def execute(
    scope: Scope, method: str, tail: list[str], body: bytes, expected: str | None
) -> tuple[int, Any]:
    skills = catalog(scope)
    effective = allowed(scope, skills)
    revision = int(
        scope.project["revision"] if scope.project else scope.org["revision"]
    )
    if method == "GET" and tail in (["skills"], ["skills", "enabled"]):
        listed = effective if scope.project or tail[-1] == "enabled" else skills
        return 200, dict(
            items=[
                dict(s.metadata(), enabled=d in effective, builtin=d in builtins())
                for d, s in sorted(listed.items(), key=lambda pair: pair[1].name)
            ],
            digests=scope.project_data.get("skill_digests", [])
            if scope.project
            else scope.org["data"].get("skill_enabled", []),
            revision=revision,
            next_cursor=None,
        )
    if method == "POST" and tail == ["skills"] and scope.project is None:
        skill = from_zip(body)
        if skill.digest in skills:
            return 200, dict(skill.metadata(), revision=revision)
        if skill.name in {s.name for s in skills.values()}:
            raise ProjectError(409, "skill_name_conflict")
        data = dict(scope.org["data"])
        uploaded = dict(data.get("skills", {}))
        cap("uploaded packages", len(uploaded) + 1, 32)
        cap(
            "catalog expanded bytes",
            sum(
                s.metadata()["size_bytes"]
                for d, s in skills.items()
                if d not in builtins()
            )
            + skill.metadata()["size_bytes"],
            8388608,
        )
        uploaded[skill.digest] = dict(
            files={p: base64.b64encode(v).decode() for p, v in skill.files.items()}
        )
        data["skills"] = uploaded
        save_org(scope, data)
        return 201, dict(
            skill.metadata(), enabled=False, revision=scope.org["revision"]
        )
    if method == "PATCH" and len(tail) == 2 and scope.project is None:
        scope.match(scope.org, expected)
        change = Enabled.model_validate_json(body)
        digest = tail[1]
        if digest not in skills:
            raise ProjectError(404, "not_found")
        if change.enabled and digest not in scope.policy["skill_allowlist"]:
            raise ProjectError(
                403,
                "policy_denied",
                "Skill digest is not in the effective organization allowlist",
            )
        data = dict(scope.org["data"])
        enabled = set(data.get("skill_enabled", []))
        enabled.add(digest) if change.enabled else enabled.discard(digest)
        data["skill_enabled"] = sorted(enabled)
        save_org(scope, data)
        return 200, dict(
            skills[digest].metadata(),
            enabled=change.enabled,
            revision=scope.org["revision"],
        )
    if method == "PUT" and tail == ["skills"] and scope.project is not None:
        scope.match(scope.project, expected)
        selection = Selection.model_validate_json(body).digests
        if len(selection) != len(set(selection)):
            raise ProjectError(422, "invalid_request", "Duplicate skill digest")
        org_allowed = set(scope.org["data"].get("skill_enabled", [])) & set(
            scope.policy["skill_allowlist"]
        )
        if set(selection) - (org_allowed & skills.keys()):
            raise ProjectError(403, "policy_denied")
        data = dict(scope.project_data, skill_digests=sorted(selection))
        scope.db.execute(
            "UPDATE atlas_projects.projects SET data=%s,revision=revision+1 WHERE org_id=%s AND id=%s",
            (Jsonb(data), scope.identity.org, scope.project_id),
        )
        scope.event("project_skills_changed", scope.project_id)
        return 200, dict(digests=sorted(selection), revision=revision + 1)
    if method == "GET" and len(tail) >= 3:
        selected_skill = effective.get(tail[1])
        if selected_skill is None:
            raise ProjectError(404, "not_found")
        if tail[2:] == ["body"]:
            return 200, dict(
                selected_skill.metadata(),
                body=selected_skill.body,
                resources=sorted(p for p in selected_skill.files if p != "SKILL.md"),
            )
        if tail[2] == "resource" and len(tail) > 3:
            path = safe_path("/".join(tail[3:]))
            if path not in selected_skill.files:
                raise ProjectError(404, "not_found")
            return 200, dict(
                path=path,
                content_base64=base64.b64encode(selected_skill.files[path]).decode(),
            )
    raise ProjectError(404, "not_found")
