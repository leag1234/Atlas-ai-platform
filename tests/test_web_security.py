"""POC-W3: reject private networks, redirects and robots exclusions."""

import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator.web import PublicResolver, Web, public_ip, validate_url


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_addresses(self) -> None:
        for address in [
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.1.1",
            "169.254.169.254",
            "0.0.0.0",
            "::1",
            "fc00::1",
            "fe80::1",
            "::ffff:127.0.0.1",
            "224.0.0.1",
            "100.64.0.1",
        ]:
            with self.subTest(address=address), self.assertRaises(ValueError):
                public_ip(address)
        public_ip("1.1.1.1")
        public_ip("2606:4700:4700::1111")

    def test_urls(self) -> None:
        for url in [
            "http://localhost",
            "http://127.0.0.1",
            "http://[::1]",
            "file:///etc/passwd",
            "https://a:b@example.org",
            "https://example.org:22",
            "https://example.org\\@127.0.0.1",
            "https://example.org/\n",
            "ecb.europa.eu",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)
        validate_url("https://example.org/news")

    async def test_mixed_dns(self) -> None:
        with patch(
            "aiohttp.resolver.ThreadedResolver.resolve",
            new=AsyncMock(return_value=[{"host": "1.1.1.1"}, {"host": "127.0.0.1"}]),
        ):
            with self.assertRaisesRegex(ValueError, "ssrf_denied"):
                await PublicResolver().resolve("example.org")

    async def test_robots_denied(self) -> None:
        web = Web()
        with patch.object(
            web,
            "_request",
            new=AsyncMock(return_value=(200, "", b"User-agent: *\nDisallow: /")),
        ) as request:
            with self.assertRaisesRegex(ValueError, "robots_denied"):
                await web.fetch("https://example.org/article")
            self.assertEqual(request.await_count, 1)

    async def test_redirect_private(self) -> None:
        web = Web()
        with patch.object(
            web,
            "_request",
            new=AsyncMock(
                side_effect=[
                    (404, "", b""),
                    (302, "http://169.254.169.254/latest/", b""),
                ]
            ),
        ) as request:
            with self.assertRaisesRegex(ValueError, "ssrf_denied"):
                await web.fetch("https://example.org/article")
            self.assertEqual(request.await_count, 2)

    async def test_robots_failure(self) -> None:
        for status in (403, 500):
            web = Web()
            with patch.object(
                web, "_request", new=AsyncMock(return_value=(status, "", b""))
            ):
                with self.assertRaisesRegex(ValueError, "robots_unavailable"):
                    await web.fetch("https://example.org")

    async def test_success(self) -> None:
        web = Web()
        with patch.object(
            web,
            "_request",
            new=AsyncMock(
                side_effect=[
                    (200, "", b"User-agent: *\nAllow: /"),
                    (200, "", b"<p>real body</p>"),
                ]
            ),
        ):
            self.assertEqual(
                await web.fetch("https://example.org"),
                ("https://example.org", b"<p>real body</p>"),
            )
