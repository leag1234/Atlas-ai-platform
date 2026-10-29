"""MCP protected-resource discovery and member-bound authorization code + S256."""

import base64
import hashlib
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit, parse_qsl, urlunsplit

import aiohttp

from services.orchestrator.mcp_http import bounded, checked, endpoint, transport
from services.retrieval.connector_store import BrokerStore, Connector


def safe(url: str) -> str:
    return endpoint(url, os.environ.get("ATLAS_MCP_ALLOW_LOCAL") == "1")


async def metadata(client: aiohttp.ClientSession, url: str) -> dict[str, Any]:
    async with client.get(safe(url), allow_redirects=False) as reply:
        if reply.status != 200:
            raise ValueError("oauth_metadata_unavailable")
        return checked(await bounded(reply, 65536), [])


async def start(
    store: BrokerStore, org: str, member: str, connector: Connector
) -> dict[str, Any]:
    if connector.auth != "oauth":
        raise ValueError("oauth_not_configured")
    url = urlsplit(connector.url)
    origin = f"{url.scheme}://{url.netloc}"
    async with aiohttp.ClientSession(
        connector=transport(),
        trust_env=False,
        auto_decompress=False,
        headers={"Accept-Encoding": "identity"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as client:
        resource: dict[str, Any] = {}
        addresses = [
            origin + "/.well-known/oauth-protected-resource" + url.path,
            origin + "/.well-known/oauth-protected-resource",
        ]
        scope = ""
        async with client.get(safe(connector.url), allow_redirects=False) as reply:
            challenge = reply.headers.get("WWW-Authenticate", "")
            match = re.search(r'resource_metadata="([^" ]+)"', challenge)
            if reply.status == 401 and match:
                addresses.insert(0, safe(match[1]))
            requested = re.search(r'scope="([^"\r\n]+)"', challenge)
            if reply.status == 401 and requested:
                scope = requested[1]
        for address in addresses:
            try:
                resource = await metadata(client, address)
                break
            except ValueError:
                continue
        if resource.get("resource") != connector.url:
            raise ValueError("oauth_resource_mismatch")
        issuers = resource.get("authorization_servers")
        if (
            not isinstance(issuers, list)
            or not issuers
            or not isinstance(issuers[0], str)
        ):
            raise ValueError("oauth_issuer_missing")
        issuer = safe(issuers[0])
        parsed = urlsplit(issuer)
        issuer_origin = f"{parsed.scheme}://{parsed.netloc}"
        config: dict[str, Any] = {}
        for address in (
            issuer_origin
            + "/.well-known/oauth-authorization-server"
            + parsed.path.rstrip("/"),
            issuer_origin
            + "/.well-known/openid-configuration"
            + parsed.path.rstrip("/"),
            issuer.rstrip("/") + "/.well-known/openid-configuration",
        ):
            try:
                config = await metadata(client, address)
                break
            except ValueError:
                continue
        if config.get("issuer") != issuer or "S256" not in config.get(
            "code_challenge_methods_supported", []
        ):
            raise ValueError("oauth_pkce_required")
        for field in ("authorization_endpoint", "token_endpoint"):
            address = safe(str(config.get(field, "")))
            if urlsplit(address).netloc != parsed.netloc:
                raise ValueError("oauth_endpoint_origin")
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        store.put(
            org,
            "oauth",
            member,
            state,
            dict(
                connector=connector.model_dump(),
                verifier=verifier,
                token_endpoint=config["token_endpoint"],
                issuer=issuer,
                expires=time.time() + 300,
            ),
        )
        params = dict(
            response_type="code",
            client_id=connector.client_id,
            redirect_uri=connector.redirect_uri,
            state=state,
            code_challenge=challenge,
            code_challenge_method="S256",
            resource=connector.url,
        )
        scopes = resource.get("scopes_supported", [])
        if not isinstance(scopes, list) or any(
            not isinstance(value, str) for value in scopes
        ):
            raise ValueError("oauth_scopes_invalid")
        if scope or scopes:
            params["scope"] = scope or " ".join(scopes)
        address = urlsplit(config["authorization_endpoint"])
        return dict(
            authorization_url=urlunsplit(
                address._replace(
                    query=urlencode([*parse_qsl(address.query), *params.items()])
                )
            )
        )


async def finish(
    store: BrokerStore,
    org: str,
    member: str,
    connector: Connector,
    state: str,
    code: str,
) -> dict[str, Any]:
    pending = store.get(org, "oauth", member, state, take=True)
    if (
        pending is None
        or pending["expires"] < time.time()
        or pending["connector"] != connector.model_dump()
    ):
        raise ValueError("oauth_state_invalid")
    async with aiohttp.ClientSession(
        connector=transport(),
        trust_env=False,
        auto_decompress=False,
        headers={"Accept-Encoding": "identity"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as client:
        async with client.post(
            pending["token_endpoint"],
            data=dict(
                grant_type="authorization_code",
                code=code,
                code_verifier=pending["verifier"],
                client_id=connector.client_id,
                redirect_uri=connector.redirect_uri,
                resource=connector.url,
            ),
            allow_redirects=False,
        ) as reply:
            if reply.status != 200:
                raise ValueError("oauth_exchange_failed")
            token = checked(await bounded(reply, 16384), [])
    access = token.get("access_token")
    expires = token.get("expires_in", 3600)
    if (
        not isinstance(token.get("token_type"), str)
        or token["token_type"].lower() != "bearer"
        or not isinstance(access, str)
        or not access
        or len(access) > 8192
        or any(c in access for c in "\r\n")
        or type(expires) is not int
        or expires <= 0
    ):
        raise ValueError("oauth_token_invalid")
    # Refresh is deliberately not automatic: expired accounts reconnect through PKCE.
    store.put(
        org,
        "credential",
        member,
        connector.id,
        dict(token=access, expires=time.time() + expires),
    )
    return dict(state="connected")
