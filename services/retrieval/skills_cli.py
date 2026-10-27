"""Skills admin API client. Credentials are read from environment, never printed."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb

from packages.skills import builtins, from_zip
from services.retrieval.project_policy import Policy


def enroll(digest: str) -> None:
    """Operator enrollment changes installation ceilings, never organization consent."""
    import re

    if not re.fullmatch("[a-f0-9]{64}", digest):
        raise ValueError("Invalid skill digest")
    with psycopg.connect(os.environ["ATLAS_RETRIEVAL_DSN"]) as db:
        role = db.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        if not role or not role[0]:
            raise ValueError("Skill enrollment requires the database operator")
        row = db.execute(
            "SELECT initial_org,initial_admin,ceilings FROM atlas_projects.installation FOR UPDATE"
        ).fetchone()
        if row is None:
            raise ValueError("Project installation is required")
        policy = Policy.model_validate(row[2]).model_dump()
        policy["skill_allowlist"] = sorted(set(policy["skill_allowlist"]) | {digest})
        db.execute(
            "UPDATE atlas_projects.installation SET ceilings=%s", (Jsonb(policy),)
        )
        db.execute(
            "INSERT INTO atlas_projects.audit(org_id,id,member_id,kind) VALUES(%s,%s,%s,'installation_skill_allowed')",
            (row[0], str(uuid4()), row[1]),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "builtins",
            "validate",
            "enroll",
            "list",
            "upload",
            "enable",
            "disable",
            "select",
        ],
    )
    parser.add_argument("value", nargs="?", default="")
    parser.add_argument("--url", default=os.environ.get("ATLAS_ADMIN_URL", ""))
    parser.add_argument("--org", default="")
    parser.add_argument("--project", default="")
    parser.add_argument("--revision", type=int)
    args = parser.parse_args()
    if args.action == "builtins":
        print(json.dumps([s.metadata() for s in builtins().values()], indent=2))
        return
    if args.action == "validate":
        print(json.dumps(from_zip(Path(args.value).read_bytes()).metadata(), indent=2))
        return
    if args.action == "enroll":
        enroll(args.value)
        print("Installation skill digest enrolled")
        return
    if not args.url:
        parser.error("--url or ATLAS_ADMIN_URL is required")
    path = f"/api/atlas/orgs/{UUID(args.org)}"
    if args.project:
        path += f"/projects/{UUID(args.project)}"
    path += "/skills"
    method, body = "GET", b""
    headers = {"Idempotency-Key": str(uuid4())}
    # A short-lived signed identity targets retrieval directly; a browser cookie
    # and Origin target the existing authenticated edge proxy.
    for name, environment in (
        ("X-Atlas-Project-Identity", "ATLAS_ADMIN_IDENTITY"),
        ("Cookie", "ATLAS_ADMIN_COOKIE"),
        ("Origin", "ATLAS_ADMIN_ORIGIN"),
    ):
        if os.environ.get(environment):
            headers[name] = os.environ[environment]
    if args.revision is not None:
        headers["If-Match"] = f'"{args.revision}"'
    payload: dict[str, Any] | None = None
    if args.action == "upload":
        body = Path(args.value).read_bytes()
        from_zip(body)
        method, headers["Content-Type"] = "POST", "application/zip"
    elif args.action in {"enable", "disable"}:
        path += "/" + args.value
        method, payload = "PATCH", {"enabled": args.action == "enable"}
    elif args.action == "select":
        method, payload = (
            "PUT",
            {"digests": args.value.split(",") if args.value else []},
        )
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=30) as client:
        response = client.request(
            method, args.url.rstrip("/") + path, headers=headers, content=body
        )
    if response.status_code >= 300:
        raise SystemExit(f"Skills API failed: HTTP {response.status_code}")
    print(json.dumps(response.json(), indent=2))


if __name__ == "__main__":
    main()
