"""Offline provider-chain contract tests."""

from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator.cache import Cache
from services.orchestrator.daily_budget import BudgetRefusal, DailyLedger
from services.orchestrator.loop import Call, Reservation
from services.orchestrator.tools import Runtime, Search, SearchQuotaError


class SearchFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Runtime(
            Cache(Path(self.directory.name) / "cache.sqlite"), Decimal("1")
        )
        self.request = Search(query="public release", lang="en")

    async def test_chain_tries_serpapi_before_tavily_after_quota(self) -> None:
        tavily_result = {"provider": "tavily", "results": [], "consulted_at": "now"}
        with (
            patch.dict(
                os.environ,
                {
                    "ATLAS_SEARCH_PROVIDER": "serpapi,tavily",
                    "SERPAPI_KEY": "fixture",
                    "TAVILY_API_KEY": "fixture",
                },
                clear=True,
            ),
            patch.object(
                self.runtime,
                "serpapi",
                new=AsyncMock(side_effect=SearchQuotaError(429)),
            ) as serpapi,
            patch.object(
                self.runtime, "tavily", new=AsyncMock(return_value=tavily_result)
            ) as tavily,
        ):
            self.assertEqual(await self.runtime.search(self.request, 1), tavily_result)
        serpapi.assert_awaited_once_with(self.request, 1)
        tavily.assert_awaited_once_with(self.request, 1)

    async def test_invalid_query_does_not_fall_back(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "ATLAS_SEARCH_PROVIDER": "tavily,serpapi",
                    "TAVILY_API_KEY": "fixture",
                    "SERPAPI_KEY": "fixture",
                },
                clear=True,
            ),
            patch.object(
                self.runtime,
                "tavily",
                new=AsyncMock(side_effect=ValueError("invalid_search_query")),
            ),
            patch.object(self.runtime, "serpapi", new=AsyncMock()) as serpapi,
        ):
            with self.assertRaisesRegex(ValueError, "invalid_search_query"):
                await self.runtime.search(self.request, 1)
        serpapi.assert_not_awaited()

    async def test_missing_key_is_skipped_when_another_provider_is_available(
        self,
    ) -> None:
        result = {"provider": "tavily", "results": [], "consulted_at": "now"}
        with (
            patch.dict(
                os.environ,
                {
                    "ATLAS_SEARCH_PROVIDER": "serpapi,tavily",
                    "TAVILY_API_KEY": "fixture",
                },
                clear=True,
            ),
            patch.object(self.runtime, "serpapi", new=AsyncMock()) as serpapi,
            patch.object(
                self.runtime, "tavily", new=AsyncMock(return_value=result)
            ) as tavily,
        ):
            self.assertEqual(await self.runtime.search(self.request, 1), result)
            self.assertEqual(
                self.runtime.estimate(Call("1", "web_search", "{}")),
                Reservation(0, Decimal("2")),
            )
        serpapi.assert_not_awaited()
        tavily.assert_awaited_once_with(self.request, 1)

    async def test_all_quota_failures_keep_the_provider_status(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "ATLAS_SEARCH_PROVIDER": "serpapi,tavily",
                    "SERPAPI_KEY": "fixture",
                    "TAVILY_API_KEY": "fixture",
                },
                clear=True,
            ),
            patch.object(
                self.runtime,
                "serpapi",
                new=AsyncMock(side_effect=SearchQuotaError(429)),
            ),
            patch.object(
                self.runtime, "tavily", new=AsyncMock(side_effect=SearchQuotaError(432))
            ),
        ):
            result = await self.runtime.execute(
                Call("1", "web_search", '{"query":"public release","lang":"en"}'), 1
            )
        self.assertEqual(result, {"error": "quota_exceeded", "provider_status": 432})

    async def test_default_is_the_unchanged_single_serpapi_provider(self) -> None:
        result = {"provider": "serpapi", "results": [], "consulted_at": "now"}
        with (
            patch.dict(os.environ, {"SERPAPI_KEY": "fixture"}, clear=True),
            patch.object(
                self.runtime, "serpapi", new=AsyncMock(return_value=result)
            ) as serpapi,
            patch.object(self.runtime, "tavily", new=AsyncMock()) as tavily,
        ):
            self.assertEqual(await self.runtime.search(self.request, 1), result)
        serpapi.assert_awaited_once_with(self.request, 1)
        tavily.assert_not_awaited()

    async def test_invalid_provider_name_is_rejected(self) -> None:
        with patch.dict(
            os.environ, {"ATLAS_SEARCH_PROVIDER": "serpapi,invalid"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "invalid_search_provider"):
                await self.runtime.search(self.request, 1)


class SearchFallbackBudgetTests(unittest.TestCase):
    def test_cache_keys_and_daily_caps_are_provider_specific(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            cache = Cache(Path(root) / "cache.sqlite")
            request = Search(query="same query", lang="en")
            self.assertNotEqual(
                cache.key(["serpapi-search-v1", 4096, request.model_dump()]),
                cache.key(
                    ["tavily-advanced-contextual-excerpts", request.model_dump()]
                ),
            )
            with patch.dict(
                os.environ,
                {
                    "ATLAS_SERPAPI_USER_DAILY_SEARCHES": "1",
                    "ATLAS_TAVILY_USER_DAILY_SEARCHES": "1",
                },
                clear=True,
            ):
                ledger = DailyLedger(Path(root) / "daily.sqlite")
                ledger.reserve_search("user", "en", "serpapi")
                ledger.reserve_search("user", "en", "tavily")
                with self.assertRaises(BudgetRefusal):
                    ledger.reserve_search("user", "en", "serpapi")
