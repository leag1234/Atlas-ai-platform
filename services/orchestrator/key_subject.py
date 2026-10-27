"""Resolve administrator-supplied logins through the host's authenticated directory."""

import os
from urllib.parse import urlsplit

import aiohttp

from packages.ports import atlas_ports


class SubjectError(ValueError):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code, self.status = code, status
        super().__init__(code)


async def resolve_login(login: str) -> tuple[str, str]:
    base = os.environ.get("ATLAS_WEBUI_URL") or atlas_ports().url("chat_ui")
    url = urlsplit(base)
    token = os.environ.get("ATLAS_WEBUI_ADMIN_TOKEN", "")
    if (
        not token
        or url.scheme not in {"http", "https"}
        or url.username
        or url.password
        or url.query
        or url.fragment
        or (
            url.scheme == "http"
            and url.hostname not in {"127.0.0.1", "localhost", "::1"}
        )
    ):
        raise SubjectError("user_directory_unconfigured", 503)
    matches: dict[str, str] = {}
    wanted = login.strip().casefold()
    try:
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
        ) as client:
            # Pagination is required: a local part may match several domains.
            for page in range(1, 101):
                async with client.get(
                    base.rstrip("/") + "/api/v1/users/",
                    params={"query": wanted, "page": str(page)},
                    headers={"Authorization": "Bearer " + token},
                    allow_redirects=False,
                ) as reply:
                    if reply.status != 200:
                        raise SubjectError("user_directory_unavailable", 503)
                    raw = await reply.content.read(1000001)
                    if len(raw) > 1000000:
                        raise SubjectError("user_directory_invalid", 503)
                    import json

                    data = json.loads(raw)
                if not isinstance(data, dict) or not isinstance(
                    data.get("users"), list
                ):
                    raise SubjectError("user_directory_invalid", 503)
                for user in data["users"]:
                    email, identifier = user.get("email"), user.get("id")
                    if not isinstance(email, str) or not isinstance(identifier, str):
                        raise SubjectError("user_directory_invalid", 503)
                    if wanted == (
                        email.casefold()
                        if "@" in wanted
                        else email.split("@")[0].casefold()
                    ):
                        if user.get("role") not in {"admin", "user"}:
                            raise SubjectError("user_inactive", 403)
                        matches[identifier] = email
                total = data.get("total")
                if not isinstance(total, int):
                    raise SubjectError("user_directory_invalid", 503)
                if page * 30 >= total:
                    break
            else:
                raise SubjectError("user_directory_limit", 503)
    except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError) as exc:
        if isinstance(exc, SubjectError):
            raise
        raise SubjectError("user_directory_unavailable", 503) from None
    if not matches:
        raise SubjectError("user_never_signed_in", 404)
    if len(matches) != 1:
        raise SubjectError("ambiguous_login_use_email", 409)
    identifier, email = next(iter(matches.items()))
    return email, identifier
