"""Archive matching active projects through the public Atlas API.

The account JSON stays outside the repository.  It may provide ``base_url``,
``org_id`` and either a bearer ``token`` or a ``headers`` object.  No credential
or project content is printed; output is only aggregate counts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(
    url: str, method: str, headers: dict[str, str], payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {**headers, "Content-Type": "application/json"}
    try:
        with urlopen(Request(url, data=body, method=method, headers=headers), timeout=30) as response:
            value = json.loads(response.read())
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from None
    if not isinstance(value, dict):
        raise RuntimeError("invalid_response")
    return value


def authenticate(base: str, account: dict[str, Any]) -> dict[str, str]:
    headers = dict(account.get("headers", {}))
    token = account.get("token")
    if token and "Authorization" not in headers:
        headers["Authorization"] = "Bearer " + str(token)
    if headers:
        return headers
    email, password = account.get("email"), account.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        raise SystemExit("account_file_requires_auth")
    signed_in = request(
        base + "/api/v1/auths/signin", "POST", {}, {"email": email, "password": password}
    )
    token = signed_in.get("token")
    if not isinstance(token, str):
        raise SystemExit("signin_failed")
    return {"Authorization": "Bearer " + token}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-file", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    args = parser.parse_args()
    account = json.loads(args.account_file.read_text())
    if not isinstance(account, dict):
        raise SystemExit("invalid_account_file")
    base = str(account.get("base_url") or account.get("url") or "https://test-atlas.indidea.org:10443").rstrip("/")
    headers = authenticate(base, account)
    org = str(account.get("org_id") or account.get("org") or "")
    if not org:
        memberships = request(base + "/api/atlas/me", "GET", headers).get("memberships", [])
        if not isinstance(memberships, list) or len(memberships) != 1:
            raise SystemExit("account_file_requires_org_id")
        org = str(memberships[0].get("org_id", ""))
    if not org:
        raise SystemExit("account_file_requires_org_id")
    collection = f"{base}/api/atlas/orgs/{org}/projects"
    items = request(collection, "GET", headers).get("items", [])
    archived = 0
    for project in items:
        if not isinstance(project, dict) or project.get("state") != "active":
            continue
        if not str(project.get("name", "")).startswith(args.prefix):
            continue
        identifier, revision = project.get("id"), project.get("revision")
        if not isinstance(identifier, str) or not isinstance(revision, int):
            raise SystemExit("invalid_project_response")
        result = request(
            f"{collection}/{identifier}", "PATCH",
            {**headers, "If-Match": f'"{revision}"'},
            {"state": "archived"},
        )
        if result.get("state") != "archived":
            raise SystemExit("archive_not_confirmed")
        archived += 1
    print(json.dumps({"archived": archived}))


if __name__ == "__main__":
    main()
