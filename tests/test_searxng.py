"""Recorded SearXNG JSON provider contract; no live engine is used in CI."""

from collections.abc import AsyncIterator
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services.orchestrator.cache import Cache
from services.orchestrator.loop import Call
from services.orchestrator.tools import Runtime, Search


class SearxngTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Cache(Path(self.directory.name) / "cache.sqlite")

    @staticmethod
    def client(body: bytes, status: int = 200) -> MagicMock:
        async def pieces(size: int) -> AsyncIterator[bytes]:
            yield body

        response = MagicMock(status=status)
        response.content.iter_chunked = pieces
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        session = MagicMock()
        session.get.return_value = context
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=session)
        return client

    async def test_recorded_json_is_normalized_localized_and_cached(self) -> None:
        body = Path("tests/fixtures/searxng-search.json").read_bytes()
        client = self.client(body)
        runtime = Runtime(self.cache, Decimal(0))
        request = Search(query="latest public release", lang="fr", news=True)
        with (
            patch.dict(os.environ, {"ATLAS_SEARCH_PROVIDER": "searxng"}),
            patch(
                "services.orchestrator.tools.aiohttp.ClientSession", return_value=client
            ),
        ):
            first = await runtime.search(request, 30)
            second = await runtime.search(request, 30)
        self.assertEqual(first, second)
        self.assertEqual(first["provider"], "searxng")
        results = first["results"]
        assert isinstance(results, list)
        self.assertEqual(results[0]["link"], "https://example.org/releases")
        params = client.__aenter__.return_value.get.call_args.kwargs["params"]
        self.assertEqual(params["language"], "fr")
        self.assertEqual(params["categories"], "news")
        self.assertEqual(params["time_range"], "month")
        self.assertEqual(client.__aenter__.return_value.get.call_count, 1)

    async def test_empty_or_error_retries_once_then_is_not_disguised(self) -> None:
        empty = self.client(b'{"results": [], "unresponsive_engines": ["brave"]}')
        runtime = Runtime(self.cache, Decimal(0))
        with (
            patch.dict(os.environ, {"ATLAS_SEARCH_PROVIDER": "searxng"}),
            patch(
                "services.orchestrator.tools.aiohttp.ClientSession", return_value=empty
            ),
        ):
            result = await runtime.execute(
                Call("search", "web_search", '{"query":"current event","lang":"en"}'),
                30,
            )
        self.assertEqual(result, {"error": "search_unavailable"})
        self.assertEqual(empty.__aenter__.return_value.get.call_count, 2)
        calls = empty.__aenter__.return_value.get.call_args_list
        self.assertNotEqual(
            calls[0].kwargs["params"]["engines"], calls[1].kwargs["params"]["engines"]
        )

    async def test_cache_keys_and_quota_do_not_mix_with_metered_providers(self) -> None:
        request = Search(query="same query", lang="en")
        searx_key = self.cache.key(
            [
                "searxng-json-v1",
                request.model_dump(),
                (("duckduckgo", "brave", "wikipedia"), ("qwant", "bing", "google")),
            ]
        )
        serp_key = self.cache.key(["serpapi-search-v1", 4096, request.model_dump()])
        self.assertNotEqual(searx_key, serp_key)
        for _ in range(900):
            self.cache.reserve_search("serpapi")
        self.cache.reserve_search("searxng")

    async def test_unavailable_search_stops_before_a_memory_answer(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import GatewayError

        with (
            patch.dict(os.environ, {"ATLAS_SEARCH_PROVIDER": "searxng"}),
            patch.object(
                Runtime,
                "execute",
                new=AsyncMock(return_value={"error": "search_unavailable"}),
            ),
        ):
            with self.assertRaisesRegex(GatewayError, "search_unavailable"):
                await ChatTools(Interaction()).execute(
                    Call(
                        "search", "web_search", '{"query":"current event","lang":"en"}'
                    ),
                    1,
                )
