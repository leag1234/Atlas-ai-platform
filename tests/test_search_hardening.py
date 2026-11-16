"""Search hardening (2026-11-16): sidecar suspensions, shared paid flights,
test traffic, per-user and per-conversation caps, provider circuit breakers.

No live search: providers are fakes or a local scripted SearXNG.
"""

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
import yaml

from services.orchestrator import search_guard
from services.orchestrator import searxng as sx
from services.orchestrator.cache import Cache
from services.orchestrator.chat_pipeline import ChatTools
from services.orchestrator.daily_budget import DailyLedger
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call, Message
from services.orchestrator.tools import (
    Runtime,
    Search,
    SearchQuotaError,
    break_seconds,
    provider_breakers,
)

FOUND: Message = {
    "provider": "searxng",
    "outcome": "ok",
    "results": [{"link": "https://example.test/a", "title": "A"}],
}


class SuspensionTests(unittest.IsolatedAsyncioTestCase):
    """The sidecar and Atlas both wait SearXNG's default suspensions."""

    def test_settings_restore_searxng_default_suspensions(self) -> None:
        settings = yaml.safe_load(Path("infra/searxng/settings.yml").read_text())
        times = settings["search"]["suspended_times"]
        # https://docs.searxng.org/admin/settings/settings_search.html
        self.assertEqual(
            times,
            {
                "SearxEngineAccessDenied": 86400,
                "SearxEngineCaptcha": 86400,
                "SearxEngineTooManyRequests": 3600,
                "cf_SearxEngineCaptcha": 1296000,
                "cf_SearxEngineAccessDenied": 86400,
                "recaptcha_SearxEngineCaptcha": 604800,
            },
        )
        for kind, name in sx.SUSPENDED_SETTINGS.items():
            self.assertEqual(sx.SUSPENDED_SECONDS[kind], times[name], kind)
        self.assertGreaterEqual(settings["search"]["ban_time_on_fail"], 5)
        self.assertGreaterEqual(settings["search"]["max_ban_time_on_fail"], 120)

    def test_refusal_kinds_from_sidecar_reasons(self) -> None:
        for reason, engine, kind in (
            ("Suspended: CAPTCHA", "startpage", "captcha"),
            ("CAPTCHA", "google", "recaptcha"),
            ("Suspended: access denied", "qwant", "access_denied"),
            ("too many requests", "brave", "too_many_requests"),
            ("Suspended: timeout", "mojeek", ""),
            ("timeout", "mojeek", ""),
        ):
            self.assertEqual(sx.refusal_kind(reason, engine), kind, reason)
        parsed = sx.parse_results(
            {"results": [], "unresponsive_engines": [["startpage", "CAPTCHA"]]},
            "startpage",
        )
        self.assertEqual(parsed["refusal"], "captcha")
        self.assertNotIn(
            "refusal",
            sx.parse_results(
                {"results": [], "unresponsive_engines": [["mojeek", "timeout"]]},
                "mojeek",
            ),
        )

    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Cache(Path(self.directory.name) / "cache.sqlite")
        self.state = sx.State()
        state_patch = patch.object(sx, "shared_state", return_value=self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.calls: list[str] = []
        self.answers: dict[str, object] = {}
        app = web.Application()
        app.router.add_get("/search", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.addAsyncCleanup(self.runner.cleanup)
        port = self.runner.addresses[0][1]
        self.endpoint = f"http://127.0.0.1:{port}"

    async def handle(self, request: web.Request) -> web.Response:
        engine = request.query["engines"]
        self.calls.append(engine)
        return web.json_response(self.answers[engine])

    async def test_a_captcha_keeps_the_engine_unasked_for_a_day(self) -> None:
        self.answers = {
            "startpage": {
                "results": [],
                "unresponsive_engines": [["startpage", "Suspended: CAPTCHA"]],
            },
            "duckduckgo": {
                "results": [
                    {"url": f"https://example.test/{i}", "title": "t", "content": ""}
                    for i in range(5)
                ]
            },
        }
        env = {"ATLAS_SEARXNG_ENGINES": "startpage,duckduckgo"}
        with patch.dict(os.environ, env):
            first = await sx.search(
                self.cache, self.endpoint, "fictional query one", "en", 5, False, 8
            )
            circuit = self.state.circuits[self.endpoint + ":startpage"]
            self.assertGreater(circuit.until - time.monotonic(), 86000)
            self.assertEqual(first["outcome"], "partial_results")
            engines = first["engines"]
            assert isinstance(engines, list)
            self.assertEqual(engines[0]["refusal"], "captcha")
            self.assertGreater(engines[0]["retry_after"], 86000)
            await sx.search(
                self.cache, self.endpoint, "fictional query two", "en", 5, False, 8
            )
        # The suspended engine was asked once, never during its suspension.
        self.assertEqual(self.calls.count("startpage"), 1)
        self.assertEqual(self.calls.count("duckduckgo"), 2)

    async def test_too_many_requests_waits_an_hour_and_retry_after_wins(self) -> None:
        circuit = sx.Circuit()
        circuit.record("upstream_blocked", 0, 3600, sx.Policy())
        self.assertEqual(circuit.until, 3600)
        self.answers = {
            "duckduckgo": {
                "results": [],
                "unresponsive_engines": [["duckduckgo", "too many requests"]],
            }
        }
        with patch.dict(os.environ, {"ATLAS_SEARXNG_ENGINES": "duckduckgo,startpage"}):
            self.answers["startpage"] = {"results": []}
            await sx.search(self.cache, self.endpoint, "fictional", "en", 5, False, 8)
        until = self.state.circuits[self.endpoint + ":duckduckgo"].until
        self.assertAlmostEqual(until - time.monotonic(), 3600, delta=30)

    async def test_admission_runs_only_for_upstream_work(self) -> None:
        self.answers = {
            "duckduckgo": {
                "results": [
                    {"url": f"https://example.test/{i}", "title": "t", "content": ""}
                    for i in range(5)
                ]
            }
        }
        admit = MagicMock()
        with patch.dict(os.environ, {"ATLAS_SEARXNG_ENGINES": "duckduckgo"}):
            await sx.search(
                self.cache, self.endpoint, "Fictional query", "en", 5, False, 8, admit
            )
            # Same normalized query: a cache hit, nothing counted.
            await sx.search(
                self.cache, self.endpoint, "fictional QUERY?", "en", 5, False, 8, admit
            )
            refused = MagicMock(
                side_effect=search_guard.SearchCapRefusal("daily_search", 5, 5, "en")
            )
            with self.assertRaises(search_guard.SearchCapRefusal):
                await sx.search(
                    self.cache, self.endpoint, "other query", "en", 5, False, 8, refused
                )
        admit.assert_called_once()
        self.assertEqual(self.calls, ["duckduckgo"])


class SharedPaidFlightTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_paid_searches_share_one_upstream_call(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            cache = Cache(Path(root) / "cache.sqlite")
            one, two = Runtime(cache, Decimal(0)), Runtime(cache, Decimal(0))
            gate = asyncio.Event()
            answer: Message = {
                "provider": "serpapi",
                "results": [],
                "consulted_at": "x",
            }

            async def upstream(request: Search, timeout: float, key: str) -> Message:
                await gate.wait()
                return answer

            fake = AsyncMock(side_effect=upstream)
            with (
                patch.object(Runtime, "_serpapi_upstream", fake),
                patch.dict(os.environ, {"SERPAPI_KEY": "fixture"}),
            ):
                first = asyncio.create_task(
                    one.serpapi(Search(query="Fictional release?", lang="en"), 5)
                )
                second = asyncio.create_task(
                    two.serpapi(Search(query="fictional RELEASE", lang="en"), 5)
                )
                await asyncio.sleep(0.01)
                gate.set()
                self.assertEqual(await first, answer)
                self.assertEqual(await second, answer)
            self.assertEqual(fake.await_count, 1)

    async def test_a_refused_starter_does_not_refuse_the_joiners(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            cache = Cache(Path(root) / "cache.sqlite")
            capped, other = Runtime(cache, Decimal(0)), Runtime(cache, Decimal(0))
            answer: Message = {
                "provider": "serpapi",
                "results": [],
                "consulted_at": "x",
            }
            refusal = search_guard.SearchCapRefusal("daily_search", 1, 1, "en")
            with (
                patch.object(
                    capped, "_admitted_reserve", MagicMock(side_effect=refusal)
                ),
                patch.object(other, "_admitted_reserve", MagicMock()),
                patch.object(
                    Runtime, "_serpapi_upstream", AsyncMock(return_value=answer)
                ),
            ):
                with self.assertRaises(search_guard.SearchCapRefusal):
                    await capped.serpapi(Search(query="fictional", lang="en"), 5)
                self.assertEqual(
                    await other.serpapi(Search(query="fictional", lang="en"), 5),
                    answer,
                )


class GuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.ledger = DailyLedger(self.root / "daily.sqlite")

    def runtime(
        self, user: str = "user-a", conversation: str = "", *identities: str
    ) -> Runtime:
        runtime = Runtime(Cache(self.root / "cache.sqlite"), Decimal(0))
        runtime.daily_ledger = self.ledger  # type: ignore[attr-defined]
        runtime.daily_user = user  # type: ignore[attr-defined]
        runtime.search_conversation = conversation
        runtime.search_identities = identities
        return runtime

    @staticmethod
    def counting(runtime: Runtime, result: Message) -> AsyncMock:
        """A provider fake that counts like a real upstream call."""

        async def provider(request: Search, timeout: float) -> Message:
            runtime._admit_upstream()
            return result

        return AsyncMock(side_effect=provider)

    def test_test_identities_are_configured_never_built_in(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(search_guard.is_test(["probe-a@example.test"]))
        with patch.dict(
            os.environ,
            {
                "ATLAS_SEARCH_TEST_PREFIXES": "probe-, ",
                "ATLAS_SEARCH_TEST_IDENTITIES": " api-key:k7,atl_0a1b2c3d",
            },
        ):
            self.assertTrue(search_guard.is_test(["Probe-A@example.test"]))
            self.assertTrue(search_guard.is_test(["x", "api-key:k7"]))
            self.assertTrue(search_guard.is_test(["atl_0a1b2c3d"]))
            self.assertFalse(search_guard.is_test(["api-key:k77", "alice"]))
            # An empty entry never makes every request test traffic.
            self.assertFalse(search_guard.is_test(["anyone", "unknown"]))
            self.assertTrue(search_guard.scope("u1", "", ["probe-b"]).test)
        self.assertEqual(
            search_guard.test_providers(("searxng", "tavily", "serpapi")), ("searxng",)
        )
        self.assertEqual(search_guard.test_providers(("serpapi",)), ("serpapi",))
        with patch.dict(os.environ, {"ATLAS_SEARCH_TEST_PROVIDERS": "searxng,bogus"}):
            with self.assertRaisesRegex(ValueError, "invalid_search_provider"):
                search_guard.test_providers(("searxng",))

    async def test_test_traffic_has_its_own_cap_and_no_metered_provider(self) -> None:
        env = {
            "ATLAS_SEARCH_PROVIDER": "searxng,tavily",
            "TAVILY_API_KEY": "fixture",
            "ATLAS_SEARCH_TEST_PREFIXES": "probe-",
            "ATLAS_SEARCH_TEST_DAILY": "2",
            "ATLAS_SEARCH_USER_DAILY": "1",
        }
        empty: Message = {
            "provider": "searxng",
            "results": [],
            "outcome": "empty_results",
        }
        with patch.dict(os.environ, env, clear=True):
            probe = self.runtime("probe-a@example.test")
            searxng = self.counting(probe, empty)
            tavily = AsyncMock()
            with (
                patch.object(probe, "searxng", searxng),
                patch.object(probe, "tavily", tavily),
            ):
                for query in ("one", "two"):
                    await probe.search(Search(query=query, lang="en"), 5)
                with self.assertRaises(search_guard.SearchCapRefusal) as refused:
                    await probe.search(Search(query="three", lang="en"), 5)
            tavily.assert_not_awaited()
            self.assertEqual(refused.exception.category, "test_daily_search")
            self.assertIn(
                "for test traffic today (2 of 2)",
                search_guard.refusal_text(refused.exception),
            )
            # The test searches did not use the real user's allowance.
            user = self.runtime("user-b")
            with patch.object(user, "searxng", self.counting(user, FOUND)):
                self.assertEqual(
                    await user.search(Search(query="four", lang="en"), 5), FOUND
                )

    async def test_one_logical_search_counts_once_across_the_chain(self) -> None:
        env = {
            "ATLAS_SEARCH_PROVIDER": "searxng,serpapi",
            "SERPAPI_KEY": "fixture",
            "ATLAS_SEARCH_USER_DAILY": "2",
        }
        empty: Message = {
            "provider": "searxng",
            "results": [],
            "outcome": "empty_results",
        }
        paid: Message = {"provider": "serpapi", "results": [], "consulted_at": "x"}
        with patch.dict(os.environ, env, clear=True):
            runtime = self.runtime("user-c")
            with (
                patch.object(runtime, "searxng", self.counting(runtime, empty)),
                patch.object(runtime, "serpapi", self.counting(runtime, paid)),
            ):
                await runtime.search(Search(query="one", lang="en"), 5)
                await runtime.search(Search(query="two", lang="en"), 5)
                with self.assertRaises(search_guard.SearchCapRefusal):
                    await runtime.search(Search(query="three", lang="en"), 5)

    async def test_conversation_cap_reaches_the_model_as_a_limit(self) -> None:
        env = {
            "ATLAS_SEARCH_PROVIDER": "searxng",
            "ATLAS_SEARCH_CONVERSATION_DAILY": "1",
            "ATLAS_SEARCH_USER_DAILY": "10",
            "ATLAS_WEB_CACHE": str(self.root / "web.sqlite"),
        }
        with patch.dict(os.environ, env, clear=True):
            tools = ChatTools(Interaction())
            tools.daily_ledger = self.ledger
            tools.daily_user = "user-d"
            tools.search_conversation = "chat-1"
            call = Call("1", "web_search", json.dumps({"query": "q", "lang": "en"}))
            with patch.object(tools, "searxng", self.counting(tools, FOUND)):
                first = await tools.execute(call, 5)
                second = await tools.execute(
                    Call("2", "web_search", json.dumps({"query": "r", "lang": "en"})), 5
                )
                # Another conversation of the same person still searches.
                tools.search_conversation = "chat-2"
                third = await tools.execute(
                    Call("3", "web_search", json.dumps({"query": "s", "lang": "en"})), 5
                )
        self.assertNotIn("error", first)
        # D5: a limit, stated as such, never "search unavailable".
        self.assertEqual(
            second,
            {
                "error": "daily_search_limit",
                "message": "search limit reached for this conversation today (1 of 1)",
            },
        )
        self.assertNotIn("error", third)

    async def test_user_cap_and_unknown_identity(self) -> None:
        env = {"ATLAS_SEARCH_PROVIDER": "searxng", "ATLAS_SEARCH_USER_DAILY": "1"}
        with patch.dict(os.environ, env, clear=True):
            runtime = self.runtime("user-e")
            with patch.object(runtime, "searxng", self.counting(runtime, FOUND)):
                await runtime.search(Search(query="one", lang="en"), 5)
                with self.assertRaises(search_guard.SearchCapRefusal) as refused:
                    await runtime.search(Search(query="two", lang="en"), 5)
            self.assertEqual(refused.exception.category, "daily_search")
            self.assertEqual(
                search_guard.refusal_text(refused.exception),
                "search limit reached today (1 of 1)",
            )
            # Requests without an identity are not pooled into one user bucket.
            anonymous = self.runtime("unknown")
            with patch.object(anonymous, "searxng", self.counting(anonymous, FOUND)):
                for query in ("a", "b", "c"):
                    await anonymous.search(Search(query=query, lang="en"), 5)
            # 0 disables the cap.
            with patch.dict(os.environ, {"ATLAS_SEARCH_USER_DAILY": "0"}):
                with patch.object(runtime, "searxng", self.counting(runtime, FOUND)):
                    await runtime.search(Search(query="three", lang="en"), 5)

    async def test_replay_answers_test_searches_without_upstream(self) -> None:
        replay = self.root / "replay.jsonl"
        recorded: Message = {**FOUND, "consulted_at": "2026-11-16T10:00:00+00:00"}
        replay.write_text(
            json.dumps(
                {
                    "query": "Fictional Town",
                    "lang": "en",
                    "news": False,
                    "result": recorded,
                }
            )
            + "\n"
        )
        env = {
            "ATLAS_SEARCH_PROVIDER": "searxng",
            "ATLAS_SEARCH_TEST_PREFIXES": "probe-",
            "ATLAS_SEARCH_REPLAY": str(replay),
        }
        with patch.dict(os.environ, env, clear=True):
            probe = self.runtime("probe-a")
            searxng = self.counting(probe, FOUND)
            with patch.object(probe, "searxng", searxng):
                hit = await probe.search(Search(query="fictional town?", lang="en"), 5)
                self.assertEqual(hit, {**recorded, "replayed": True})
                with self.assertRaisesRegex(ValueError, "search_replay_miss"):
                    await probe.search(Search(query="unrecorded", lang="en"), 5)
                searxng.assert_not_awaited()
                with patch.dict(os.environ, {"ATLAS_SEARCH_REPLAY_MISS": "live"}):
                    await probe.search(Search(query="unrecorded", lang="en"), 5)
                searxng.assert_awaited_once()
            # Real users stay live unless the replay scope is "all".
            user = self.runtime("user-f")
            live = self.counting(user, FOUND)
            with patch.object(user, "searxng", live):
                await user.search(Search(query="fictional town", lang="en"), 5)
                live.assert_awaited_once()
                with patch.dict(os.environ, {"ATLAS_SEARCH_REPLAY_SCOPE": "all"}):
                    replayed = await user.search(
                        Search(query="fictional town", lang="en"), 5
                    )
                self.assertTrue(replayed["replayed"])
                live.assert_awaited_once()

    async def test_only_test_searches_are_recorded(self) -> None:
        record = self.root / "record.jsonl"
        env = {
            "ATLAS_SEARCH_PROVIDER": "searxng",
            "ATLAS_SEARCH_TEST_PREFIXES": "probe-",
            "ATLAS_SEARCH_RECORD": str(record),
        }
        with patch.dict(os.environ, env, clear=True):
            for user in ("probe-a", "user-g"):
                runtime = self.runtime(user)
                with patch.object(runtime, "searxng", AsyncMock(return_value=FOUND)):
                    await runtime.search(Search(query=f"query {user}", lang="en"), 5)
            lines = record.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["query"], "query probe-a")
            # A recording is a valid replay.
            with patch.dict(os.environ, {"ATLAS_SEARCH_REPLAY": str(record)}):
                probe = self.runtime("probe-b")
                self.assertTrue(
                    (await probe.search(Search(query="Query probe-a", lang="en"), 5))[
                        "replayed"
                    ]
                )


class BreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_refusing_provider_is_skipped_for_its_window(self) -> None:
        env = {
            "ATLAS_SEARCH_PROVIDER": "tavily,serpapi",
            "TAVILY_API_KEY": "fixture",
            "SERPAPI_KEY": "fixture",
        }
        paid: Message = {"provider": "serpapi", "results": [], "consulted_at": "x"}
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, env, clear=True),
        ):
            runtime = Runtime(Cache(Path(root) / "cache.sqlite"), Decimal(0))
            tavily = AsyncMock(side_effect=SearchQuotaError(432, retry_after=120))
            serpapi = AsyncMock(side_effect=SearchQuotaError(429, "quota_exceeded"))
            with (
                patch.object(runtime, "tavily", tavily),
                patch.object(runtime, "serpapi", serpapi),
                self.assertLogs("services.orchestrator.tools", "WARNING") as logs,
            ):
                first = await runtime.execute(
                    Call("1", "web_search", '{"query":"q","lang":"en"}'), 5
                )
                second = await runtime.execute(
                    Call("2", "web_search", '{"query":"r","lang":"en"}'), 5
                )
            self.assertEqual(tavily.await_count, 1)
            self.assertEqual(serpapi.await_count, 1)
            self.assertEqual(first["error"], "quota_exceeded")
            # D5: skipped providers still report their quota refusal.
            self.assertEqual(second["error"], "quota_exceeded")
            self.assertEqual(second["provider_status"], 429)
            attempts = second["attempts"]
            assert isinstance(attempts, list)
            self.assertEqual(
                [(a["provider"], a["breaker"]) for a in attempts],
                [("tavily", "open"), ("serpapi", "open")],
            )
            self.assertLessEqual(attempts[0]["retry_in"], 120)
            chains = [
                json.loads(line[line.index("{") :])
                for line in logs.output
                if '"search_chain"' in line
            ]
            self.assertEqual(
                chains[0]["breakers_opened"],
                {"tavily": 120, "serpapi": 3600},
            )
            # The window over, the provider is asked again.
            breakers = provider_breakers()
            for provider, breaker in list(breakers.items()):
                breakers[provider] = type(breaker)(0, breaker.code, breaker.status)
            serpapi.side_effect = None
            serpapi.return_value = paid
            with (
                patch.object(runtime, "tavily", tavily),
                patch.object(runtime, "serpapi", serpapi),
            ):
                third = await runtime.search(Search(query="s", lang="en"), 5)
            self.assertEqual(third["provider"], "serpapi")
            self.assertEqual(tavily.await_count, 2)

    def test_break_windows(self) -> None:
        self.assertEqual(break_seconds("quota_exceeded"), 3600)
        self.assertEqual(break_seconds("search_rate_limited"), 60)
        self.assertEqual(break_seconds("search_rate_limited", 15), 15)
        self.assertEqual(break_seconds("quota_exceeded", 10**9), 86400)


if __name__ == "__main__":
    unittest.main()
