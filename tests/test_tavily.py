"""Search content, provider quota separation and safe failures (M24/D8)."""

from collections.abc import AsyncIterator
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator.cache import Cache
from services.orchestrator.tools import Fetch, Runtime, Search


class TavilyTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_quota_status_survives_tool_boundary(self) -> None:
        from services.orchestrator.loop import Call

        for status in (429, 432):
            response = MagicMock(status=status)
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=response)
            session = MagicMock()
            session.post.return_value = context
            client = MagicMock()
            client.__aenter__ = AsyncMock(return_value=session)
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as root,
                patch.dict(
                    os.environ,
                    {"ATLAS_SEARCH_PROVIDER": "tavily", "TAVILY_API_KEY": "test-only"},
                ),
                patch(
                    "services.orchestrator.tools.aiohttp.ClientSession",
                    return_value=client,
                ),
            ):
                runtime = Runtime(Cache(Path(root) / "cache.sqlite"), Decimal(0))
                result = await runtime.execute(
                    Call("1", "web_search", '{"query":"public news","lang":"en"}'), 1
                )
                self.assertEqual(
                    result, {"error": "quota_exceeded", "provider_status": status}
                )
                self.assertEqual(session.post.call_count, 1)

    async def test_serpapi_limits_reach_public_error(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Call
        from services.orchestrator.model import GatewayError

        for message, code, detail in (
            ("You have run out of searches.", "quota_exceeded", "quota exhausted"),
            (
                "Your account has exceeded the hourly limit.",
                "search_rate_limited",
                "hourly limit",
            ),
        ):

            async def pieces(size: int) -> AsyncIterator[bytes]:
                yield json.dumps({"error": message}).encode()

            response = MagicMock(status=429)
            response.content.iter_chunked = pieces
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=response)
            session = MagicMock()
            session.get.return_value = context
            client = MagicMock()
            client.__aenter__ = AsyncMock(return_value=session)
            with (
                self.subTest(code=code),
                tempfile.TemporaryDirectory() as root,
                patch.dict(
                    os.environ,
                    {
                        "ATLAS_SEARCH_PROVIDER": "serpapi",
                        "SERPAPI_KEY": "test-only",
                        "ATLAS_WEB_CACHE": root + "/cache.sqlite",
                    },
                ),
                patch(
                    "services.orchestrator.tools.aiohttp.ClientSession",
                    return_value=client,
                ),
            ):
                with self.assertRaises(GatewayError) as caught:
                    await ChatTools(Interaction()).execute(
                        Call("1", "web_search", '{"query":"public news","lang":"en"}'),
                        1,
                    )
                self.assertIn(code, str(caught.exception))
                self.assertIn(detail, caught.exception.detail)
                self.assertIn("429", caught.exception.detail)
                self.assertNotIn(message, caught.exception.detail)
                self.assertEqual(session.get.call_count, 1)

    async def test_content_reused_without_fetch_and_provider_key_not_cached(
        self,
    ) -> None:
        async def pieces(size: int) -> AsyncIterator[bytes]:
            yield json.dumps(
                {
                    "results": [
                        {
                            "title": "CPC timing",
                            "url": "https://example.org/timing",
                            "content": "Machine-specific timing evidence.",
                        }
                    ]
                }
            ).encode()

        response = MagicMock(status=200)
        response.content.iter_chunked = pieces
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        session = MagicMock()
        session.post.return_value = context
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=session)
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_SEARCH_PROVIDER": "tavily",
                    "TAVILY_API_KEY": "test-only-secret",
                },
            ),
            patch(
                "services.orchestrator.tools.aiohttp.ClientSession", return_value=client
            ),
        ):
            cache = Cache(Path(root) / "cache.sqlite")
            runtime = Runtime(cache, Decimal(0))
            request = Search(
                query='"OUTI" "OTIR" "Instruction Timings" site:cpctech.cpcwiki.de',
                lang="fr",
            )
            result = await runtime.search(request, 1)
            self.assertEqual(result["provider"], "tavily")
            self.assertIn("Machine-specific", str(result))
            self.assertNotIn("test-only-secret", str(result))
            again = await runtime.search(request, 1)
            self.assertEqual(result, again)
            self.assertEqual(session.post.call_count, 1)
            body = session.post.call_args.kwargs["json"]
            self.assertEqual(body["include_domains"], ["cpctech.cpcwiki.de"])
            self.assertEqual(body["search_depth"], "advanced")
            self.assertEqual(body["query"], '"OUTI" "OTIR" "Instruction Timings"')
            self.assertEqual(
                session.post.call_args.kwargs["json"]["api_key"], "test-only-secret"
            )
            with patch.object(
                runtime.web,
                "fetch",
                new=AsyncMock(side_effect=AssertionError("must not refetch")),
            ) as fetch:
                page = await runtime.fetch(Fetch(url="https://example.org/timing"), 1)
            self.assertIn("Machine-specific", str(page))
            fetch.assert_not_awaited()
            self.assertNotIn(b"test-only-secret", cache.path.read_bytes())

    async def test_raw_content_fallback_bounds_and_cache_migration(self) -> None:
        for raw in (None, "", "Full page evidence." + "x" * 17000, 123):
            with self.subTest(raw_type=type(raw).__name__):

                async def pieces(size: int) -> AsyncIterator[bytes]:
                    yield json.dumps(
                        {
                            "results": [
                                {
                                    "title": "Evidence",
                                    "url": "https://example.org/evidence",
                                    "content": "Context " * 60
                                    + "Measurement is 73 units.",
                                    "raw_content": raw,
                                }
                            ]
                        }
                    ).encode()

                response = MagicMock(status=200)
                response.content.iter_chunked = pieces
                context = MagicMock()
                context.__aenter__ = AsyncMock(return_value=response)
                session = MagicMock()
                session.post.return_value = context
                client = MagicMock()
                client.__aenter__ = AsyncMock(return_value=session)
                with (
                    tempfile.TemporaryDirectory() as root,
                    patch.dict(os.environ, {"TAVILY_API_KEY": "test-only"}),
                    patch(
                        "services.orchestrator.tools.aiohttp.ClientSession",
                        return_value=client,
                    ),
                ):
                    cache = Cache(Path(root) / "cache.sqlite")
                    request = Search(query="evidence", lang="en")
                    cache.put(
                        cache.key(["tavily", request.model_dump()]),
                        {"results": "obsolete excerpts"},
                        3600,
                    )
                    runtime = Runtime(cache, Decimal(0))
                    if raw == 123:
                        with self.assertRaisesRegex(
                            ValueError, "invalid_search_response"
                        ):
                            await runtime.tavily(request, 1)
                        continue
                    result = await runtime.tavily(request, 1)
                    self.assertTrue(
                        session.post.call_args.kwargs["json"]["include_raw_content"]
                    )
                    assert raw is None or isinstance(raw, str)
                    expected = (raw or ("Context " * 60 + "Measurement is 73 units."))[
                        :16000
                    ]
                    items = result["results"]
                    assert isinstance(items, list)
                    self.assertEqual(items[0]["content"], expected)
                    # Preserve the provider's query-relevant excerpt even when
                    # raw page text starts with navigation or unrelated material.
                    self.assertIn("Measurement is 73 units.", items[0]["snippet"])
                    page = cache.get(
                        cache.key(["tavily-page", "https://example.org/evidence"])
                    )
                    self.assertIsNotNone(page)
                    assert page is not None
                    self.assertEqual(
                        page["text"],
                        raw or ("Context " * 60 + "Measurement is 73 units."),
                    )
                    self.assertFalse(page["truncated"])

    async def test_combined_page_content_leaves_room_for_tool_followup(self) -> None:
        async def pieces(size: int) -> AsyncIterator[bytes]:
            yield json.dumps(
                {
                    "results": [
                        {
                            "title": f"Page {n}",
                            "url": f"https://example.org/{n}",
                            "content": "excerpt",
                            "raw_content": "evidence " * 2000,
                        }
                        for n in range(5)
                    ]
                }
            ).encode()

        response = MagicMock(status=200)
        response.content.iter_chunked = pieces
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        session = MagicMock()
        session.post.return_value = context
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=session)
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"TAVILY_API_KEY": "test-only"}),
            patch(
                "services.orchestrator.tools.aiohttp.ClientSession", return_value=client
            ),
        ):
            runtime = Runtime(Cache(Path(root) / "cache.sqlite"), Decimal(0))
            result = await runtime.tavily(Search(query="evidence", lang="en"), 1)
            self.assertEqual(
                session.post.call_args.kwargs["json"]["search_depth"], "advanced"
            )
            items = result["results"]
            assert isinstance(items, list)
            self.assertEqual(len(items), 5)
            self.assertLessEqual(sum(len(item["content"]) for item in items), 32000)
            self.assertTrue(
                all(item["content"].startswith("evidence") for item in items)
            )

    async def test_provider_failure_and_unknown_switch_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            runtime = Runtime(Cache(Path(root) / "cache.sqlite"), Decimal(0))
            with (
                patch.dict(os.environ, {"ATLAS_SEARCH_PROVIDER": "unknown"}),
                patch(
                    "services.orchestrator.tools.aiohttp.ClientSession",
                    side_effect=AssertionError("network must not be called"),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "invalid_search_provider"):
                    await runtime.search(Search(query="x", lang="en"), 1)

    async def test_monthly_provider_quotas_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            cache = Cache(Path(root) / "cache.sqlite")
            for _ in range(900):
                cache.reserve_search()
            with self.assertRaisesRegex(ValueError, "quota_exceeded"):
                cache.reserve_search()
            cache.reserve_search("tavily")

    async def test_advanced_search_reserves_two_credits_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            cache = Cache(Path(root) / "cache.sqlite")
            for _ in range(899):
                cache.reserve_search("tavily")
            with self.assertRaisesRegex(ValueError, "quota_exceeded"):
                cache.reserve_search("tavily", credits=2)
            cache.reserve_search("tavily")
            with self.assertRaisesRegex(ValueError, "quota_exceeded"):
                cache.reserve_search("tavily")

    async def test_failed_search_stops_before_memory_answer(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Call
        from services.orchestrator.model import GatewayError

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_SEARCH_PROVIDER": "tavily",
                    "ATLAS_WEB_CACHE": root + "/cache.sqlite",
                },
            ),
        ):
            item = Interaction()
            tools = ChatTools(item)
            with patch.object(
                Runtime,
                "execute",
                new=AsyncMock(return_value={"error": "quota_exceeded"}),
            ):
                with self.assertRaisesRegex(GatewayError, "quota_exceeded") as caught:
                    await tools.execute(
                        Call("search", "web_search", '{"query":"CPC","lang":"fr"}'), 1
                    )
            self.assertIn("quota exhausted", caught.exception.detail)
            self.assertIn("quota_exceeded", item.erreurs)

    async def test_http_quota_failure_and_oversize_response(self) -> None:
        from services.orchestrator.loop import Call

        cases = [
            (429, b"provider-private-detail", "quota_exceeded"),
            (401, b"provider-private-detail", "search_unavailable"),
            (200, b'{"results": "wrong"}', "invalid_search_response"),
            (200, b"x" * 2000001, "response_too_large"),
        ]
        for status, body, expected in cases:

            async def pieces(size: int) -> AsyncIterator[bytes]:
                yield body

            response = MagicMock(status=status)
            response.content.iter_chunked = pieces
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=response)
            session = MagicMock()
            session.post.return_value = context
            client = MagicMock()
            client.__aenter__ = AsyncMock(return_value=session)
            with (
                self.subTest(status=status, expected=expected),
                tempfile.TemporaryDirectory() as root,
                patch.dict(
                    os.environ,
                    {
                        "ATLAS_SEARCH_PROVIDER": "tavily",
                        "TAVILY_API_KEY": "test-only-secret",
                    },
                ),
                patch(
                    "services.orchestrator.tools.aiohttp.ClientSession",
                    return_value=client,
                ),
            ):
                runtime = Runtime(Cache(Path(root) / "cache.sqlite"), Decimal(0))
                result = await runtime.execute(
                    Call("search", "web_search", '{"query":"CPC","lang":"en"}'), 1
                )
                self.assertEqual(
                    result,
                    {
                        "error": expected,
                        **(
                            {"provider_status": status}
                            if expected == "quota_exceeded"
                            else {}
                        ),
                        **(
                            {
                                "message": "response_too_large: measured 2000001 bytes; limit 2000000 bytes"
                            }
                            if expected == "response_too_large"
                            else {}
                        ),
                    },
                )

    async def test_cached_long_page_uses_hierarchy_and_retains_source(self) -> None:
        text = "Background material. " * 20000 + "\nZephyr warranty: 73 months."
        url = "https://example.org/long-report"
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_SEARCH_PROVIDER": "tavily"}),
        ):
            cache = Cache(Path(root) / "cache.sqlite")
            cache.put(
                cache.key(["tavily-page", url]),
                {
                    "url": url,
                    "text": text,
                    "consulted_at": "2026-09-21",
                    "provider": "tavily",
                },
                3600,
            )
            runtime = Runtime(cache, Decimal(0), "Zephyr warranty")
            with patch.object(runtime.web, "fetch", new=AsyncMock()) as fetch:
                page = await runtime.fetch(Fetch(url=url), 5)
            fetch.assert_not_awaited()
            self.assertIn("73 months", str(page["text"]))
            tokens = page["estimated_tokens"]
            assert isinstance(tokens, int)
            self.assertLessEqual(tokens, 4000)
            self.assertTrue(page["selected"])
            self.assertEqual(page["provider"], "tavily")
            self.assertEqual(page["consulted_at"], "2026-09-21")
            original = cache.get(str(page["handle"]))
            self.assertIsNotNone(original)
            assert original is not None
            self.assertEqual(original["text"], text)
