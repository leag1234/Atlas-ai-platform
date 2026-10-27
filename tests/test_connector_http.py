"""Local MCP HTTP server exercises real wire framing, discovery and bounded calls."""

import asyncio
import json
import os
import unittest
from unittest.mock import patch
from typing import Any

from aiohttp import web

from packages.limits import LimitError
from services.orchestrator.mcp_http import HTTP, endpoint, ConnectorResolver
from services.orchestrator.mcp_transport import VERSION


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        environment = patch.dict(os.environ, ATLAS_MCP_ALLOW_LOCAL="1")
        environment.start()
        self.addCleanup(environment.stop)
        self.calls: list[dict[str, Any]] = []
        self.sse = False
        self.bad = ""
        self.paginated = False
        app = web.Application()
        app.router.add_post("/mcp", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.url = "http://127.0.0.1:" + str(self.runner.addresses[0][1]) + "/mcp"

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        data = await request.json()
        self.calls.append(data)
        self.assertEqual(
            request.headers["Accept"], "application/json, text/event-stream"
        )
        if data["method"] == "initialize":
            result: dict[str, Any] = dict(
                protocolVersion=VERSION, capabilities=dict(tools={})
            )
        else:
            self.assertEqual(request.headers["Mcp-Session-Id"], "test-session")
            self.assertEqual(request.headers["MCP-Protocol-Version"], VERSION)
            if data["method"] == "notifications/initialized":
                return web.Response(status=202)
            if data["method"] == "tools/list":
                result = dict(
                    tools=[
                        dict(
                            name="search",
                            description="Search fixed public documents",
                            inputSchema=dict(
                                type="object",
                                properties=dict(query=dict(type="string")),
                                required=["query"],
                                additionalProperties=False,
                            ),
                            annotations=dict(readOnlyHint=True),
                        )
                    ]
                )
                if self.paginated:
                    if data["params"].get("cursor") == "second":
                        result["tools"][0]["name"] = "second"
                    else:
                        result["nextCursor"] = "second"
            else:
                result = dict(
                    content=[dict(type="text", text="The launch date is 12 November.")]
                )
        if self.bad == "oversize":
            result = dict(content="x" * 300000)
        if self.bad == "echo":
            result = dict(content=request.headers.get("Authorization", ""))
        if self.bad == "delay":
            await asyncio.sleep(0.1)
        body = dict(jsonrpc="2.0", id=data["id"], result=result)
        if self.bad == "redirect":
            return web.Response(status=307, headers={"Location": self.url})
        if self.bad == "id":
            body["id"] = 99
        if self.sse:
            return web.Response(
                text="event: message\r\ndata: " + json.dumps(body) + "\r\n\r\n",
                content_type="text/event-stream",
                headers={"Mcp-Session-Id": "test-session"},
            )
        return web.json_response(body, headers={"Mcp-Session-Id": "test-session"})

    async def test_json_and_sse_discovery_and_read(self) -> None:
        for sse in (False, True):
            self.sse = sse
            result = await HTTP(self.url).exchange("tools/list", {})
            self.assertEqual(result["tools"][0]["name"], "search")
            result = await HTTP(self.url).exchange(
                "tools/call", dict(name="search", arguments=dict(query="launch"))
            )
            self.assertIn("12 November", result["content"][0]["text"])

    async def test_limits_protocol_and_secret_echo(self) -> None:
        for bad, error in (
            ("oversize", LimitError),
            ("id", ValueError),
            ("echo", ValueError),
        ):
            self.bad = bad
            with self.assertRaises(error):
                await HTTP(self.url, "generated-test-credential").exchange(
                    "tools/list", {}
                )
        self.bad = "delay"
        with self.assertRaises(TimeoutError):
            await HTTP(self.url).exchange("tools/list", {}, 0.01)

    async def test_discovery_pagination_and_no_redirect_retry(self) -> None:
        self.paginated = True
        result = await HTTP(self.url).exchange("tools/list", {})
        self.assertEqual(
            [tool["name"] for tool in result["tools"]], ["search", "second"]
        )
        self.bad = "redirect"
        count = len(self.calls)
        with self.assertRaises(ValueError):
            await HTTP(self.url).exchange("tools/list", {})
        self.assertEqual(len(self.calls), count + 1)

    async def test_network_boundary_blocks_private_metadata_and_dns_rebinding(
        self,
    ) -> None:
        for url in (
            "https://169.254.169.254/metadata",
            "https://10.1.2.3/mcp",
            "https://[::1]/mcp",
        ):
            with self.assertRaises(ValueError):
                endpoint(url)
        self.assertEqual(endpoint(self.url, local=True), self.url)
        resolver = ConnectorResolver()
        self.addAsyncCleanup(resolver.close)
        with patch(
            "aiohttp.resolver.ThreadedResolver.resolve",
            return_value=[dict(host="10.1.2.3")],
        ):
            with self.assertRaises(ValueError):
                await resolver.resolve("attacker.example")
