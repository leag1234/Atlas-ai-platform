"""REQ-KEY-02: administrator-only loopback API and its secret-safe CLI client."""

import argparse
from collections.abc import Awaitable, Callable
import hmac
import ipaddress
import json
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from urllib.request import Request as HTTPRequest, urlopen
from urllib.error import HTTPError

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from services.orchestrator.api_keys import configured_store
from services.orchestrator.key_subject import SubjectError, resolve_login
from services.orchestrator.dev_auth import Store as DeveloperStore

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class CreateKey(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    org_id: str = Field(default="stable", min_length=1, max_length=320)
    member_id: str | None = Field(default=None, min_length=1, max_length=320)
    subject: str | None = Field(default=None, min_length=1, max_length=320)
    budget_user: str | None = Field(default=None, min_length=1, max_length=200)
    login: str | None = Field(default=None, min_length=1, max_length=320)
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Literal["assistant", "model"]] = Field(min_length=1)
    lifetime_days: int = Field(default=90, ge=1, le=365)
    daily_requests: int = Field(default=20, ge=1)
    daily_micro_eur: int = Field(default=1000000, ge=1)


@app.middleware("http")
async def administrator(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    secret = os.environ.get("ATLAS_API_KEY_ADMIN_TOKEN", "")
    try:
        loopback = (
            request.client is not None
            and ipaddress.ip_address(request.client.host).is_loopback
        )
    except ValueError:
        loopback = False
    if (
        not loopback
        or len(secret.encode()) < 32
        or len(request.headers.getlist("authorization")) != 1
        or not hmac.compare_digest(
            request.headers.get("authorization", "").encode(),
            ("Bearer " + secret).encode(),
        )
        or any(
            h in request.headers
            for h in ("origin", "cookie", "x-forwarded-for", "x-atlas-proxy-secret")
        )
    ):
        return JSONResponse({"error": "administrator_required"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/keys")
def list_keys() -> dict[str, object]:
    return {"data": configured_store().list_keys()}


async def subject_values(
    body: CreateKey, *, require_login: bool = True
) -> dict[str, Any]:
    values = body.model_dump(exclude={"login"})
    if body.login:
        email, identifier = await resolve_login(body.login)
        values.update(subject=email, budget_user=identifier)
        if os.environ.get("ATLAS_PROJECT_MODE") != "1":
            values.update(member_id=identifier)
    elif require_login and os.environ.get("ATLAS_STABLE") == "1":
        raise SubjectError("login_required")
    if not all(values[field] for field in ("member_id", "subject", "budget_user")):
        raise SubjectError("subject_identity_required")
    return values


@app.post("/keys")
async def create_key(body: CreateKey) -> JSONResponse:
    try:
        metadata, secret = configured_store().create(
            **await subject_values(body),
            created_by=os.environ.get("ATLAS_API_KEY_ADMIN_SUBJECT", "operator"),
        )
    except SubjectError as exc:
        return JSONResponse({"error": exc.code}, status_code=exc.status)
    except ValueError:
        return JSONResponse({"error": "invalid_key_policy"}, status_code=400)
    return JSONResponse({"key": secret, **metadata}, status_code=201)


@app.delete("/keys/{key_id}")
def revoke_key(key_id: str) -> Response:
    configured_store().revoke(key_id)
    return Response(status_code=204)


@app.post("/imports/{developer}")
async def import_key(developer: str, body: CreateKey) -> Response:
    try:
        values = await subject_values(body, require_login=False)
    except SubjectError as exc:
        return JSONResponse({"error": exc.code}, status_code=exc.status)
    result = configured_store().import_developer(
        DeveloperStore(
            Path(os.environ.get("ATLAS_DEVAPI_DB", "BRAIN/devapi/usage.sqlite"))
        ),
        developer,
        org_id=values["org_id"],
        member_id=values["member_id"],
        subject=values["subject"],
        budget_user=values["budget_user"],
        created_by=os.environ.get("ATLAS_API_KEY_ADMIN_SUBJECT", "operator"),
    )

    return JSONResponse(result)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Atlas administrator client; secrets go only to an exclusive 0600 file"
    )
    parser.add_argument("operation", choices=("create", "list", "revoke", "import"))
    parser.add_argument("--id")
    parser.add_argument(
        "--admin-url",
        default=os.environ.get("ATLAS_API_KEY_ADMIN_URL", "http://127.0.0.1:8031"),
    )
    parser.add_argument("--key-file", type=Path)
    for field in ("org-id", "member-id", "subject", "budget-user", "login", "name"):
        parser.add_argument("--" + field)
    parser.add_argument("--scope", action="append", choices=("assistant", "model"))
    parser.add_argument("--lifetime-days", type=int, default=90)
    parser.add_argument("--daily-requests", type=int, default=20)
    parser.add_argument("--daily-micro-eur", type=int, default=1000000)
    args = parser.parse_args()
    url = urlsplit(args.admin_url)
    if (
        url.scheme != "http"
        or url.hostname not in {"127.0.0.1", "::1"}
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        parser.error("admin URL must be loopback HTTP")
    body = None
    path, method = "/keys", "GET"
    fd: int | None = None
    if args.operation in {"create", "import"}:
        values = {
            field: getattr(args, field)
            for field in (
                "org_id",
                "member_id",
                "subject",
                "budget_user",
                "login",
                "name",
                "lifetime_days",
                "daily_requests",
                "daily_micro_eur",
            )
        }
        values = {k: v for k, v in values.items() if v is not None}
        values["scopes"] = args.scope or ["assistant"]
        try:
            body = CreateKey.model_validate(values).model_dump_json().encode()
        except ValueError:
            parser.error(
                "create/import requires org, member, subject, budget-user and name"
            )
        method = "POST"
        if args.operation == "create":
            if args.key_file is None:
                parser.error("--key-file required")
            fd = os.open(
                args.key_file,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
            )
        else:
            if not args.id or not all(c.isalnum() or c in "-_" for c in args.id):
                parser.error("--id developer required")
            path = "/imports/" + args.id
    elif args.operation == "revoke":
        if not args.id or not all(c.isalnum() or c in "-_" for c in args.id):
            parser.error("--id required")
        method, path = "DELETE", "/keys/" + args.id
    try:
        request = HTTPRequest(
            args.admin_url.rstrip("/") + path,
            data=body,
            method=method,
            headers={
                "Authorization": "Bearer "
                + os.environ.get("ATLAS_API_KEY_ADMIN_TOKEN", ""),
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=15) as reply:
            result = json.loads(reply.read()) if reply.status != 204 else {}
        if fd is not None:
            with os.fdopen(fd, "w") as output:
                fd = None
                output.write(result.pop("key") + "\n")
        print(json.dumps(result))
    except HTTPError as exc:
        if fd is not None:
            os.close(fd)
            args.key_file.unlink(missing_ok=True)
        try:
            code = json.loads(exc.read(4096)).get("error")
        except (ValueError, OSError):
            code = None
        allowed = {
            "user_never_signed_in",
            "ambiguous_login_use_email",
            "user_inactive",
            "login_required",
            "user_directory_unconfigured",
            "user_directory_unavailable",
            "subject_identity_required",
        }
        raise SystemExit(
            code
            if isinstance(code, str) and code in allowed
            else "Key administration failed"
        ) from None
    except (OSError, ValueError, KeyError):
        # No HTTP body, request headers, URL or secret in diagnostics.
        if fd is not None:
            os.close(fd)
            args.key_file.unlink(missing_ok=True)
        raise SystemExit(
            "Key administration failed; inspect administrator service status"
        ) from None


if __name__ == "__main__":
    main()
