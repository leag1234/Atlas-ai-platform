"""Exercise the real connector and HTTP reader with local test-only routing."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from services.orchestrator.web import MAX_BYTES, PublicResolver, Web, http_timeout


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hits: list[str] = []
        self.mode = "ok"

        async def handler(request: web.Request) -> web.StreamResponse:
            self.hits.append(request.path)
            if request.path == "/robots.txt":
                return web.Response(text="User-agent: *\nAllow: /\n")
            if self.mode == "size":
                return web.Response(body=b"x" * (MAX_BYTES + 1))
            if self.mode == "chunked":
                response = web.StreamResponse()
                response.enable_chunked_encoding()
                await response.prepare(request)
                try:
                    for _ in range(125):
                        await response.write(b"x" * 16384)
                except ConnectionResetError:
                    pass  # Test client intentionally rejects the oversized body.
                return response
            if self.mode == "redirect":
                return web.Response(
                    status=302, headers={"Location": "http://10.0.0.1/"}
                )
            if self.mode == "encoding":
                return web.Response(
                    body=b"compressed", headers={"Content-Encoding": "gzip"}
                )
            return web.Response(text="public article")

        app = web.Application()
        app.router.add_get("/{path:.*}", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = self.runner.addresses[0][1]

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def test_reader_limits_and_redirect_validation(self) -> None:
        async def resolve(
            self_: PublicResolver, host: str, port: int = 0, family: int = 0
        ) -> list[dict[str, object]]:
            return [
                {
                    "hostname": host,
                    "host": "127.0.0.1",
                    "port": self.port,
                    "family": 2,
                    "proto": 0,
                    "flags": 0,
                }
            ]

        with patch.object(PublicResolver, "resolve", resolve):
            self.assertEqual(
                (await Web().fetch("http://public.example/article"))[1],
                b"public article",
            )
            for mode, error in (
                ("size", "response_too_large"),
                ("chunked", "response_too_large"),
                ("redirect", "ssrf_denied"),
                ("encoding", "unsupported_encoding"),
            ):
                self.mode = mode
                with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, error):
                    await Web().fetch("http://public.example/article")

    async def test_real_connector_rejects_private_dns(self) -> None:
        with (
            patch(
                "aiohttp.resolver.ThreadedResolver.resolve",
                new=AsyncMock(return_value=[{"host": "127.0.0.1"}]),
            ),
            self.assertRaisesRegex(ValueError, "ssrf_denied"),
        ):
            await Web().fetch("http://public.example/article")
        self.assertEqual(self.hits, [])


class TimeoutTests(unittest.IsolatedAsyncioTestCase):
    def test_independent_transport_budgets(self) -> None:
        from services.orchestrator.web import PAGE_TIMEOUT, SEARCH_TIMEOUT

        search = http_timeout(120, SEARCH_TIMEOUT)
        page = http_timeout(120, PAGE_TIMEOUT)
        self.assertEqual((search.total, search.connect, search.sock_read), (15, 5, 15))
        self.assertEqual((page.total, page.connect, page.sock_read), (30, 5, 30))
        short = http_timeout(0.01, PAGE_TIMEOUT)
        self.assertEqual((short.total, short.connect, short.sock_read), (0.01,) * 3)

    async def test_page_budget_and_caller_deadline(self) -> None:
        transport = Web()
        with (
            patch.object(transport, "_robots", new=AsyncMock()),
            patch.object(
                transport, "_request", new=AsyncMock(return_value=(200, "", b"ok"))
            ),
            patch(
                "services.orchestrator.web.asyncio.timeout", wraps=asyncio.timeout
            ) as deadline,
        ):
            await transport.fetch("https://example.org", 120)
            deadline.assert_called_once_with(30)

        async def slow_robots(url: str, timeout: float) -> None:
            await asyncio.sleep(1)

        with (
            patch.object(transport, "_robots", slow_robots),
            self.assertRaises(TimeoutError),
        ):
            await transport.fetch("https://example.org", 0.01)
