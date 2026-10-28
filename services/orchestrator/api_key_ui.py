"""REQ-KEY-01..06: browser session management of the guest organization's keys."""

import asyncio
import os
from pathlib import Path
from typing import Any, Literal

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.ports import atlas_ports
from services.orchestrator.api_keys import configured_store
from services.orchestrator.key_subject import resolve_login, SubjectError
from services.orchestrator.project_ui import browser_origin, bounded_json, public_error
from services.retrieval.project_identity import ProjectError

router = APIRouter(prefix="/api/atlas/keys")


class Create(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=100)
    lifetime_days: Literal[30, 90, 365] = 90
    scopes: list[Literal["assistant", "model"]] = Field(
        default=["assistant"], min_length=1
    )
    login: str | None = Field(default=None, min_length=1, max_length=320)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["off", "admin_only", "self_service"]
    daily_requests: int = Field(ge=1, le=1000000)
    daily_micro_eur: int = Field(ge=1, le=1000000000)
    max_lifetime_days: int = Field(ge=30, le=365)
    active_key_limit: int = Field(ge=1, le=100)


@router.get("/client.js")
@router.get("/style.css")
def asset(request: Request) -> Response:
    css = request.url.path.endswith(".css")
    return Response(
        Path(__file__).with_name("api-key-ui." + ("css" if css else "js")).read_text(),
        media_type="text/css" if css else "text/javascript",
        headers={"X-Content-Type-Options": "nosniff"},
    )


async def session_user(request: Request) -> dict[str, str]:
    browser_origin(request)
    token = request.cookies.get("token", "")
    if not token or len(token) > 8192 or any(c in token for c in "\r\n"):
        raise ProjectError(401, "unauthenticated")
    url = os.environ.get("ATLAS_WEBUI_URL") or atlas_ports().url("chat_ui")
    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=5)
    ) as client:
        async with client.get(
            url.rstrip("/") + "/api/v1/auths/",
            headers={"Authorization": "Bearer " + token},
            allow_redirects=False,
        ) as reply:
            if reply.status != 200:
                raise ProjectError(401, "unauthenticated")
            user = await bounded_json(reply)
    for field in ("id", "email", "role"):
        if not isinstance(user.get(field), str) or not 1 <= len(user[field]) <= 320:
            raise ProjectError(401, "unauthenticated")
    if user["role"] not in {"user", "admin"}:
        raise ProjectError(403, "user_inactive")
    return {field: user[field] for field in ("id", "email", "role")}


async def body(request: Request) -> bytes:
    value = bytearray()
    async with asyncio.timeout(5):
        async for chunk in request.stream():
            value.extend(chunk)
            if len(value) > 4096:
                raise ProjectError(413, "request_size_exceeded")
    return bytes(value)


@router.api_route("", methods=["GET", "POST"])
@router.api_route("/{key_id}", methods=["PUT", "DELETE"])
async def control(request: Request, key_id: str = "") -> Response:
    try:
        user = await session_user(request)
        # This is the existing single guest organization, never a caller selector.
        org = os.environ.get("ATLAS_API_KEY_ORG_ID", "stable")
        store = configured_store()
        policy = store.policy(org)
        admin = user["role"] == "admin"
        if not admin and (
            policy["mode"] == "off"
            or (request.method != "GET" and policy["mode"] != "self_service")
            or request.query_params.get("all") == "true"
            or key_id == "policy"
        ):
            store.audit(
                "failure", None, actor=user["id"], reason="policy_denied", org_id=org
            )
            raise ProjectError(403, "policy_denied")
        result: dict[str, Any]
        status = 200
        if request.method == "GET":
            keys = [
                {**key, "usage": store.usage(key["id"])}
                for key in store.list_keys()
                if key["org_id"] == org
                and (
                    key["member_id"] == user["id"]
                    or (admin and request.query_params.get("all") == "true")
                )
            ]
            store.audit("list", None, actor=user["id"], org_id=org)
            result = dict(data=keys, policy=policy, admin=admin)
        elif request.method == "POST":
            values = Create.model_validate_json(await body(request))
            subject, member = user["email"], user["id"]
            if values.login is not None:
                if not admin:
                    raise ProjectError(403, "administrator_required")
                subject, member = await resolve_login(values.login)
            metadata, secret = store.create(
                org_id=org,
                member_id=member,
                subject=subject,
                budget_user=member,
                name=values.name,
                scopes=list(values.scopes),
                lifetime_days=values.lifetime_days,
                created_by=user["id"],
                browser_policy=True,
                administrator=admin,
            )
            result, status = dict(**metadata, key=secret), 201
        elif request.method == "PUT" and key_id == "policy":
            values_policy = Policy.model_validate_json(await body(request))
            store.set_policy(org, values_policy.model_dump(), user["id"])
            result = dict(policy=store.policy(org))
        elif request.method == "DELETE" and key_id != "policy":
            key = store.get(key_id)
            if key["org_id"] != org or (not admin and key["member_id"] != user["id"]):
                store.audit(
                    "failure",
                    None,
                    actor=user["id"],
                    reason="key_not_found",
                    org_id=org,
                )
                raise ProjectError(404, "key_not_found")
            store.revoke(key_id)
            store.audit("management_revoke", key_id, actor=user["id"], org_id=org)
            return Response(
                status_code=204, headers={"Cache-Control": "private, no-store"}
            )
        else:
            raise ProjectError(404, "not_found")
        return JSONResponse(
            result, status_code=status, headers={"Cache-Control": "private, no-store"}
        )
    except ValidationError:
        return public_error(ProjectError(422, "invalid_key_policy"))
    except SubjectError as exc:
        return public_error(ProjectError(exc.status, exc.code))
    except KeyError:
        return public_error(ProjectError(404, "key_not_found"))
    except PermissionError:
        return public_error(ProjectError(403, "policy_denied"))
    except ValueError as exc:
        code = (
            str(exc)
            if str(exc)
            in {"active_key_limit", "expiry_exceeds_policy", "invalid_key_policy"}
            else "key_service_unavailable"
        )
        return public_error(
            ProjectError(400 if code != "key_service_unavailable" else 503, code)
        )
    except ProjectError as exc:
        return public_error(exc)
    except (aiohttp.ClientError, TimeoutError, OSError):
        return public_error(ProjectError(503, "key_service_unavailable"))
