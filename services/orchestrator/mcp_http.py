"""Bounded Streamable HTTP JSON-RPC; calls are never retried after dispatch."""

import asyncio
import ipaddress
import json
import os
import re
import socket
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import ResolveResult
from aiohttp.resolver import ThreadedResolver

from packages.limits import LimitError
from services.orchestrator.mcp_transport import LIMIT, VERSION
from services.orchestrator.web import public_ip


def network_address(address: str, local: bool) -> None:
    if local and ipaddress.ip_address(address).is_loopback:
        return
    public_ip(address)


class ConnectorResolver(ThreadedResolver):
    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[ResolveResult]:
        results = await super().resolve(host, port, socket.AddressFamily(family))
        if not results:
            raise ValueError("mcp_dns_empty")
        for result in results:
            network_address(
                result["host"], os.environ.get("ATLAS_MCP_ALLOW_LOCAL") == "1"
            )
        return results


def transport() -> aiohttp.TCPConnector:
    # Validate the addresses actually used for the connection, including OAuth metadata.
    return aiohttp.TCPConnector(resolver=ConnectorResolver(), use_dns_cache=False)


def endpoint(url: str, local: bool = False) -> str:
    parsed = urlsplit(url)
    if (
        len(url) > 4096
        or any(ord(c) <= 32 for c in url)
        or "\\" in url
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not parsed.hostname
        or parsed.query
        or (
            parsed.scheme != "https"
            and not (
                local
                and parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            )
        )
    ):
        raise ValueError("mcp_endpoint_invalid")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        if "%" in parsed.hostname:
            raise ValueError("mcp_endpoint_invalid") from None
    else:
        network_address(str(address), local)
    return url


async def bounded(reply: aiohttp.ClientResponse, limit: int = LIMIT) -> bytes:
    if reply.headers.get("Content-Encoding", "identity") != "identity":
        raise ValueError("mcp_content_encoding")
    raw = bytearray()
    async for chunk in reply.content.iter_chunked(8192):
        raw.extend(chunk)
        if len(raw) > limit:
            raise LimitError("mcp_response_limit", len(raw), limit, "bytes")
    return bytes(raw)


def checked(raw: bytes, secrets: list[str]) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict) or any(
        secret and secret in json.dumps(value, ensure_ascii=False) for secret in secrets
    ):
        raise ValueError("mcp_invalid_or_secret_response")
    return value


class HTTP:
    def __init__(self, url: str, token: str = "") -> None:
        self.url = endpoint(url, os.environ.get("ATLAS_MCP_ALLOW_LOCAL") == "1")
        self.token = token
        self.session = ""
        self.consumed = 0

    async def post(
        self, client: aiohttp.ClientSession, message: dict[str, Any]
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": VERSION,
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        async with client.post(
            self.url,
            json={"jsonrpc": "2.0", **message},
            headers=headers,
            allow_redirects=False,
        ) as reply:
            if reply.status not in {200, 202}:
                raise ValueError("mcp_http_refused")
            if reply.headers.get("Content-Encoding", "identity") != "identity":
                raise ValueError("mcp_content_encoding")
            session = reply.headers.get("Mcp-Session-Id")
            if session is not None:
                if not re.fullmatch(r"[\x21-\x7e]{1,256}", session):
                    raise ValueError("mcp_session_invalid")
                if self.session and session != self.session:
                    raise ValueError("mcp_session_changed")
                self.session = session
            if "method" not in message or "id" not in message:
                if reply.status != 202:
                    raise ValueError("mcp_notification_refused")
                return {}
            if reply.content_type == "application/json":
                raw = await bounded(reply, LIMIT - self.consumed)
                self.consumed += len(raw)
                values = [checked(raw, [self.token])]
            elif reply.content_type == "text/event-stream":
                buffer = b""
                values = []
                events = 0
                async for chunk in reply.content.iter_chunked(8192):
                    self.consumed += len(chunk)
                    if self.consumed > LIMIT:
                        raise LimitError(
                            "mcp_response_limit", self.consumed, LIMIT, "bytes"
                        )
                    buffer += chunk
                    # Normalize CRLF only when a complete line is available.
                    buffer = buffer.replace(b"\r\n", b"\n")
                    while b"\n\n" in buffer:
                        event, buffer = buffer.split(b"\n\n", 1)
                        data = b"\n".join(
                            line[5:].lstrip(b" ")
                            for line in event.split(b"\n")
                            if line.startswith(b"data:")
                        )
                        if not data:
                            continue
                        events += 1
                        if events > 32:
                            raise ValueError("mcp_notification_limit")
                        value = checked(data, [self.token])
                        if value.get("jsonrpc") != "2.0":
                            raise ValueError("mcp_protocol")
                        if "method" in value:
                            if "id" in value:
                                await self.post(
                                    client,
                                    {
                                        "id": value["id"],
                                        **(
                                            {"result": {}}
                                            if value["method"] == "ping"
                                            else {
                                                "error": {
                                                    "code": -32601,
                                                    "message": "unsupported",
                                                }
                                            }
                                        ),
                                    },
                                )
                            continue
                        values = [value]
                        break
                    if values:
                        break
            else:
                raise ValueError("mcp_content_type")
            if len(values) != 1:
                raise ValueError("mcp_response_missing")
            value = values[0]
            if (
                value.get("jsonrpc") != "2.0"
                or type(value.get("id")) is not int
                or value["id"] != message["id"]
                or "error" in value
                or not isinstance(value.get("result"), dict)
            ):
                raise ValueError("mcp_protocol")
            return dict(value["result"])

    async def exchange(
        self, method: str, params: dict[str, Any], timeout: float = 15
    ) -> dict[str, Any]:
        async with (
            asyncio.timeout(min(timeout, 15)),
            aiohttp.ClientSession(
                connector=transport(),
                trust_env=False,
                auto_decompress=False,
                headers={"Accept-Encoding": "identity"},
                timeout=aiohttp.ClientTimeout(total=min(timeout, 15)),
            ) as client,
        ):
            hello = await self.post(
                client,
                dict(
                    id=1,
                    method="initialize",
                    params=dict(
                        protocolVersion=VERSION,
                        capabilities={},
                        clientInfo=dict(name="atlas", version="0.8"),
                    ),
                ),
            )
            if (
                hello.get("protocolVersion") != VERSION
                or not isinstance(hello.get("capabilities"), dict)
                or "tools" not in hello["capabilities"]
            ):
                raise ValueError("mcp_capabilities")
            await self.post(client, dict(method="notifications/initialized"))
            result = await self.post(client, dict(id=2, method=method, params=params))
            if method == "tools/list":
                tools = result.get("tools")
                if not isinstance(tools, list):
                    raise ValueError("mcp_tools_invalid")
                cursor = result.get("nextCursor")
                for page in range(3, 11):
                    if cursor is None:
                        break
                    if not isinstance(cursor, str) or len(cursor) > 1024:
                        raise ValueError("mcp_cursor_invalid")
                    result = await self.post(
                        client, dict(id=page, method=method, params=dict(cursor=cursor))
                    )
                    if not isinstance(result.get("tools"), list):
                        raise ValueError("mcp_tools_invalid")
                    tools.extend(result["tools"])
                    cursor = result.get("nextCursor")
                if cursor is not None or len(tools) > 128:
                    raise ValueError("mcp_discovery_limit")
                return dict(tools=tools)
            if result.get("isError"):
                raise ValueError("mcp_tool_error")
            return result
