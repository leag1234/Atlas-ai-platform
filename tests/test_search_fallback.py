"""Offline provider-chain contract tests."""

from decimal import Decimal
import os
from pathlib import Path
import tempfile
from typing import cast
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
            self.assertEqual(
                await self.runtime.search(self.request, 1),
                {
                    **tavily_result,
                    "attempts": [
                        {"provider": "serpapi", "outcome": "quota_exceeded"},
                        {"provider": "tavily", "outcome": "ok"},
                    ],
                },
            )
        serpapi.assert_awaited_once()
        tavily.assert_awaited_once()
        for provider in (serpapi, tavily):
            call = provider.await_args
            assert call is not None
            self.assertEqual(call.args[0], self.request)
            self.assertGreater(call.args[1], 0)
            self.assertLessEqual(call.args[1], 1)

    async def test_spent_allowance_of_one_provider_moves_to_the_next(self) -> None:
        empty = {"provider": "searxng", "results": [], "outcome": "empty_results"}
        found = {"provider": "serpapi", "results": [], "consulted_at": "now"}
        spent = BudgetRefusal("daily_search", 20, 20, "en")
        env = {
            "ATLAS_SEARCH_PROVIDER": "searxng,tavily,serpapi",
            "SERPAPI_KEY": "fixture",
            "TAVILY_API_KEY": "fixture",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(self.runtime, "searxng", new=AsyncMock(return_value=empty)),
            patch.object(self.runtime, "tavily", new=AsyncMock(side_effect=spent)),
            patch.object(
                self.runtime, "serpapi", new=AsyncMock(return_value=found)
            ) as serpapi,
        ):
            result = await self.runtime.search(self.request, 5)
        serpapi.assert_awaited_once()
        self.assertEqual(result["provider"], "serpapi")
        self.assertEqual(
            result["attempts"],
            [
                {"provider": "searxng", "outcome": "empty_results"},
                {"provider": "tavily", "outcome": "daily_search"},
                {"provider": "serpapi", "outcome": "ok"},
            ],
        )

    async def test_spent_allowance_everywhere_is_still_reported_as_such(self) -> None:
        empty = {"provider": "searxng", "results": [], "outcome": "empty_results"}
        spent = BudgetRefusal("daily_search", 20, 20, "en")
        env = {"ATLAS_SEARCH_PROVIDER": "searxng,tavily", "TAVILY_API_KEY": "fixture"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(self.runtime, "searxng", new=AsyncMock(return_value=empty)),
            patch.object(self.runtime, "tavily", new=AsyncMock(side_effect=spent)),
        ):
            with self.assertRaises(BudgetRefusal) as raised:
                await self.runtime.search(self.request, 5)
        self.assertIs(raised.exception, spent)

    async def test_non_cooldown_searxng_outcomes_fall_through_without_retry(
        self,
    ) -> None:
        for outcome in (
            "upstream_blocked",
            "upstream_timeout",
            "empty_results",
        ):
            with self.subTest(outcome=outcome):
                fallback = {
                    "provider": "tavily",
                    "results": [{"link": "https://example.test/result"}],
                    "consulted_at": "now",
                }
                with (
                    patch.dict(
                        os.environ,
                        {
                            "ATLAS_SEARCH_PROVIDER": "searxng,tavily",
                            "TAVILY_API_KEY": "fixture",
                        },
                        clear=True,
                    ),
                    patch.object(
                        self.runtime,
                        "searxng",
                        new=AsyncMock(
                            return_value={
                                "provider": "searxng",
                                "outcome": outcome,
                                "results": [],
                            }
                        ),
                    ) as searxng,
                    patch.object(
                        self.runtime, "tavily", new=AsyncMock(return_value=fallback)
                    ) as tavily,
                ):
                    result = await self.runtime.search(self.request, 5)
                self.assertEqual(result["provider"], "tavily")
                attempts = cast(list[dict[str, object]], result["attempts"])
                self.assertEqual(
                    [str(attempt["outcome"]) for attempt in attempts],
                    [outcome, "ok"],
                )
                self.assertEqual(searxng.await_count, 1)
                tavily.assert_awaited_once()

    async def test_searxng_leaves_time_for_the_next_provider(self) -> None:
        # Staging 2026-11-12: slow engines took all but 2 s of a 15 s search,
        # Tavily (3 to 5 s) timed out and the whole turn failed.
        fallback = {
            "provider": "tavily",
            "results": [{"link": "https://example.test/result"}],
            "consulted_at": "now",
        }
        with (
            patch.dict(
                os.environ,
                {
                    "ATLAS_SEARCH_PROVIDER": "searxng,tavily",
                    "TAVILY_API_KEY": "fixture",
                },
                clear=True,
            ),
            patch.object(
                self.runtime,
                "searxng",
                new=AsyncMock(
                    return_value={
                        "provider": "searxng",
                        "outcome": "upstream_timeout",
                        "results": [],
                    }
                ),
            ) as searxng,
            patch.object(
                self.runtime, "tavily", new=AsyncMock(return_value=fallback)
            ) as tavily,
        ):
            result = await self.runtime.search(self.request, 15)
        self.assertEqual(result["provider"], "tavily")
        searxng_budget = cast(tuple[object, float], searxng.call_args_list[-1].args)[1]
        tavily_budget = cast(tuple[object, float], tavily.call_args_list[-1].args)[1]
        self.assertLessEqual(searxng_budget, 8.01)
        self.assertGreaterEqual(tavily_budget, 6.9)

    async def test_short_search_budget_is_split_in_half(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "ATLAS_SEARCH_PROVIDER": "searxng,tavily",
                    "TAVILY_API_KEY": "fixture",
                },
                clear=True,
            ),
            patch.object(
                self.runtime,
                "searxng",
                new=AsyncMock(
                    return_value={"provider": "searxng", "outcome": "ok", "results": []}
                ),
            ) as searxng,
        ):
            await self.runtime.search(self.request, 6)
        self.assertLessEqual(
            cast(tuple[object, float], searxng.call_args_list[-1].args)[1], 3.01
        )

    async def test_searxng_alone_keeps_the_whole_budget(self) -> None:
        with (
            patch.dict(os.environ, {"ATLAS_SEARCH_PROVIDER": "searxng"}, clear=True),
            patch.object(
                self.runtime,
                "searxng",
                new=AsyncMock(
                    return_value={"provider": "searxng", "outcome": "ok", "results": []}
                ),
            ) as searxng,
        ):
            await self.runtime.search(self.request, 15)
        self.assertGreater(
            cast(tuple[object, float], searxng.call_args_list[-1].args)[1], 14.9
        )

    async def test_short_local_cooldown_waits_then_retries(self) -> None:
        cooldown = {
            "provider": "searxng",
            "outcome": "upstream_blocked",
            "results": [],
            "engines": [
                {
                    "engine": "duckduckgo",
                    "outcome": "upstream_blocked",
                    "reason": "cooldown",
                    "retry_after": 2.47,
                }
            ],
        }
        recovered = {
            "provider": "searxng",
            "outcome": "ok",
            "results": [{"link": "https://example.test/recovered"}],
        }
        with (
            patch.dict(os.environ, {"ATLAS_SEARCH_PROVIDER": "searxng"}, clear=True),
            patch.object(
                self.runtime,
                "searxng",
                new=AsyncMock(side_effect=[cooldown, recovered]),
            ) as searxng,
            patch(
                "services.orchestrator.tools.asyncio.sleep", new=AsyncMock()
            ) as sleep,
        ):
            result = await self.runtime.search(self.request, 5)
        self.assertEqual(result["results"], recovered["results"])
        self.assertEqual(
            result["attempts"],
            [
                {"provider": "searxng", "outcome": "upstream_blocked"},
                {"provider": "searxng", "outcome": "ok", "retry": True},
            ],
        )
        self.assertEqual(searxng.await_count, 2)
        sleep.assert_awaited_once_with(2.47)

    async def test_partial_searxng_results_are_successful(self) -> None:
        partial = {
            "provider": "searxng",
            "outcome": "partial_results",
            "results": [{"link": "https://example.test/partial"}],
        }
        with (
            patch.dict(
                os.environ, {"ATLAS_SEARCH_PROVIDER": "searxng,tavily"}, clear=True
            ),
            patch.object(self.runtime, "searxng", new=AsyncMock(return_value=partial)),
            patch.object(self.runtime, "tavily", new=AsyncMock()) as tavily,
        ):
            result = await self.runtime.search(self.request, 5)
        self.assertEqual(result["results"], partial["results"])
        self.assertEqual(result["outcome"], "partial_results")
        self.assertNotIn("attempts", result)
        tavily.assert_not_awaited()

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
            self.assertEqual(
                await self.runtime.search(self.request, 1),
                result,
            )
            self.assertEqual(
                self.runtime.estimate(Call("1", "web_search", "{}")),
                Reservation(0, Decimal("2")),
            )
        serpapi.assert_not_awaited()
        tavily.assert_awaited_once()
        tavily_call = tavily.await_args
        assert tavily_call is not None
        self.assertEqual(tavily_call.args[0], self.request)
        self.assertGreater(tavily_call.args[1], 0)
        self.assertLessEqual(tavily_call.args[1], 1)

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
        self.assertEqual(
            result,
            {
                "error": "quota_exceeded",
                "provider_status": 432,
                "attempts": [
                    {"provider": "serpapi", "outcome": "quota_exceeded"},
                    {"provider": "tavily", "outcome": "quota_exceeded"},
                ],
            },
        )

    async def test_default_is_the_unchanged_single_serpapi_provider(self) -> None:
        result = {"provider": "serpapi", "results": [], "consulted_at": "now"}
        with (
            patch.dict(os.environ, {"SERPAPI_KEY": "fixture"}, clear=True),
            patch.object(
                self.runtime, "serpapi", new=AsyncMock(return_value=result)
            ) as serpapi,
            patch.object(self.runtime, "tavily", new=AsyncMock()) as tavily,
        ):
            self.assertEqual(
                await self.runtime.search(self.request, 1),
                result,
            )
        serpapi.assert_awaited_once()
        serpapi_call = serpapi.await_args
        assert serpapi_call is not None
        self.assertEqual(serpapi_call.args[0], self.request)
        self.assertGreater(serpapi_call.args[1], 0)
        self.assertLessEqual(serpapi_call.args[1], 1)
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
