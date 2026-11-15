"""Owner search memo: scripted HTTP failures and bounded engine state."""

import asyncio
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import format_datetime
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web

from packages.images import VisionMessage
from services.orchestrator import searxng as sx
from services.orchestrator.cache import Cache
from services.orchestrator.chat_pipeline import ChatTools
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call
from services.orchestrator.loop import Message
from services.orchestrator.model import GatewayError
from packages.limits import LimitError
from services.orchestrator.tools import Runtime, Search, SearchQuotaError


def rows(engine: str, count: int = 5) -> dict[str, object]:
    return {
        "results": [
            {
                "title": f"{engine} result {i}",
                "url": f"https://example.org/{engine}/{i}",
                "content": "Useful source content",
            }
            for i in range(count)
        ]
    }


class SearxngPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.port = int(os.environ.get("ATLAS_SEARXNG_TEST_PORT", "13780"))
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Cache(Path(self.directory.name) / "cache.sqlite")
        self.state = sx.State()
        # Resolved on every call: a subtest may swap in a fresh state while
        # requests of the previous subtest are still in flight on the old one.
        self.state_patch = patch.object(
            sx, "shared_state", side_effect=lambda *args, **kwargs: self.state
        )
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.env = patch.dict(
            os.environ,
            {
                "ATLAS_SEARCH_PROVIDER": "searxng",
                "ATLAS_SEARXNG_URL": f"http://127.0.0.1:{self.port}",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.scripts: dict[str, str] = {}
        self.seen: list[dict[str, str]] = []
        self.started: list[str] = []
        self.hold: asyncio.Event | None = None
        self.delay = 0.0
        self.starts: list[tuple[str, float]] = []
        self.active = 0
        self.peak = 0
        app = web.Application()
        app.router.add_get("/search", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        self.addAsyncCleanup(self.runner.cleanup)

    async def handle(self, request: web.Request) -> web.Response:
        self.seen.append(dict(request.query))
        engine = request.query["engines"]
        self.started.append(engine)
        self.starts.append((engine, time.monotonic()))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.hold:
                await self.hold.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            action = self.scripts.get(engine, "ok")
            if action == "timeout":
                # Well past every deadline used below, so a loaded CI runner cannot
                # make a timed-out engine look completed.
                await asyncio.sleep(1.0)
                return web.json_response(rows(engine))
            if action in {"403", "429"}:
                return web.Response(status=int(action), headers={"Retry-After": "120"})
            if action == "captcha":
                return web.Response(
                    text="<html>Please solve the CAPTCHA</html>",
                    content_type="text/html",
                )
            if action == "engine_captcha":
                return web.json_response(
                    {
                        "results": [],
                        "unresponsive_engines": [[engine, "CAPTCHA required"]],
                    }
                )
            if action == "empty":
                return web.json_response({"results": []})
            if action == "few":
                return web.json_response(rows(engine, 1))
            if action == "invalid":
                return web.json_response(["unexpected"])
            return web.json_response(rows(engine))
        finally:
            self.active -= 1

    async def search(
        self, query: str = "public release", lang: str = "en", timeout: float = 2
    ) -> dict[str, object]:
        return await sx.search(
            self.cache, f"http://127.0.0.1:{self.port}", query, lang, 5, False, timeout
        )

    async def test_tier_one_and_wikipedia_parallel_then_cache(self) -> None:
        self.hold = asyncio.Event()
        task = asyncio.create_task(self.search())
        for _ in range(100):
            if len(self.started) == 2:
                break
            await asyncio.sleep(0.001)
        self.assertEqual(set(self.started), {sx.ENGINES[0], "wikipedia"})
        self.assertEqual(self.peak, 2)
        self.hold.set()
        output = await task
        self.assertEqual(output["outcome"], "ok")
        results = output["results"]
        assert isinstance(results, list)
        self.assertEqual(len(results), 6)
        self.assertEqual(await self.search(), output)
        self.assertEqual(len(self.seen), 2)

    async def test_sparse_primary_uses_brave_without_other_fallbacks(self) -> None:
        self.scripts[sx.ENGINES[0]] = "few"
        result = await self.search()
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(
            [e for e in self.started if e != "wikipedia"], list(sx.ENGINES[:2])
        )

    async def test_local_cooldown_skip_is_not_partial(self) -> None:
        circuit = self.state.circuits.setdefault(
            f"http://127.0.0.1:{self.port}:{sx.ENGINES[0]}", sx.Circuit()
        )
        circuit.next_call = time.monotonic() + 60
        result = await self.search()
        self.assertEqual(result["outcome"], "ok")
        self.assertNotIn("message", result)
        # The cooling engine is passed over, not asked and not a failure.
        self.assertEqual([e for e in self.started if e != "wikipedia"], [sx.ENGINES[1]])
        engines = result["engines"]
        assert isinstance(engines, list)
        self.assertNotIn(sx.ENGINES[0], [row["engine"] for row in engines])

    def fast_policy(
        self, engines: tuple[str, ...] = sx.ENGINES
    ) -> AbstractContextManager[object]:
        # The real spacing (3 s and 30 s) scaled down; same rules.
        return patch.object(
            sx.Policy,
            "environment",
            return_value=sx.Policy(
                engines=engines, engine_interval=0.2, fallback_interval=1.0
            ),
        )

    def assert_spacing(self, engines: tuple[str, ...] = sx.ENGINES) -> None:
        for engine in {name for name, _ in self.starts}:
            spacing = 0.2 if engine == "wikipedia" or engine in engines[:2] else 1.0
            times = [moment for name, moment in self.starts if name == engine]
            gaps = [later - earlier for earlier, later in zip(times, times[1:])]
            # Arrival times at the server carry connection setup jitter.
            self.assertTrue(all(gap >= spacing - 0.05 for gap in gaps), (engine, gaps))

    async def test_concurrent_searches_queue_for_slots_instead_of_failing(
        self,
    ) -> None:
        # Staging 2026-11-15: three research turns at once found every engine
        # cooling down; searches failed and fell to exhausted paid providers.
        # Calls are shorter than the spacing, as on staging (1-2 s for 3 s).
        self.delay = 0.1
        engines = sx.ENGINES[:3]
        with self.fast_policy(engines):
            results = await asyncio.gather(
                *(self.search(query=f"question {i}", timeout=4) for i in range(9))
            )
        self.assertEqual([r["outcome"] for r in results], ["ok"] * 9)
        self.assert_spacing(engines)
        web = [name for name, _ in self.starts if name != "wikipedia"]
        self.assertEqual(len(web), 9)
        # Primaries take the queue; fallbacks only absorb the overflow.
        self.assertTrue(set(sx.ENGINES[:2]) <= set(web))
        self.assertGreater(sum(name in sx.ENGINES[:2] for name in web), len(web) / 2)

    async def test_short_wait_for_a_primary_rather_than_a_fallback(self) -> None:
        endpoint = f"http://127.0.0.1:{self.port}:"
        first, second = (
            self.state.circuits.setdefault(endpoint + name, sx.Circuit())
            for name in sx.ENGINES[:2]
        )
        first.next_call = time.monotonic() + 0.15
        second.next_call = time.monotonic() + 0.5
        with self.fast_policy():
            result = await self.search(timeout=4)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual([e for e in self.started if e != "wikipedia"], [sx.ENGINES[0]])
        self.started.clear()
        # Both primaries far away: the next fallback answers without waiting.
        first.next_call = second.next_call = time.monotonic() + 30
        with self.fast_policy():
            result = await self.search(query="another question", timeout=4)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual([e for e in self.started if e != "wikipedia"], [sx.ENGINES[2]])

    async def test_no_wait_past_the_deadline(self) -> None:
        for name in (*sx.ENGINES, "wikipedia"):
            circuit = self.state.circuits.setdefault(
                f"http://127.0.0.1:{self.port}:{name}", sx.Circuit()
            )
            circuit.next_call = time.monotonic() + 30
        started = time.monotonic()
        with self.fast_policy():
            result = await self.search(timeout=4)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(result["outcome"], "upstream_blocked")
        self.assertEqual(self.seen, [])
        engines = result["engines"]
        assert isinstance(engines, list)
        self.assertTrue(all(row["reason"] == "cooldown" for row in engines))

    async def test_research_turns_with_exhausted_paid_providers_get_sources(
        self,
    ) -> None:
        # Four concurrent turns of three searches each; Tavily answers 432 and
        # the local SerpAPI reservation is full. Every search must be served by
        # SearXNG, which answers, instead of ending on quota_exceeded. Calls
        # outlast the primary spacing here, so booked slots also overrun.
        self.delay = 0.3
        tavily = AsyncMock(side_effect=SearchQuotaError(432))
        serpapi = AsyncMock(
            side_effect=LimitError("quota_exceeded", 902, 900, "monthly search credits")
        )

        async def turn(number: int) -> list[dict[str, object]]:
            runtime = Runtime(self.cache, Decimal(0))
            outputs = []
            for search in range(3):
                outputs.append(
                    await runtime.execute(
                        Call(
                            f"{number}-{search}",
                            "web_search",
                            json.dumps(
                                {
                                    "query": f"turn {number} search {search}",
                                    "lang": "en",
                                }
                            ),
                        ),
                        15,
                    )
                )
            return outputs

        engines = sx.ENGINES[:3]
        with (
            self.fast_policy(engines),
            patch.dict(
                os.environ,
                {
                    "ATLAS_SEARCH_PROVIDER": "searxng,tavily,serpapi",
                    "TAVILY_API_KEY": "test-key",
                    "SERPAPI_KEY": "test-key",
                },
            ),
            patch.object(Runtime, "tavily", tavily),
            patch.object(Runtime, "serpapi", serpapi),
        ):
            turns = await asyncio.gather(*(turn(number) for number in range(4)))
        outputs = [output for outputs in turns for output in outputs]
        self.assertEqual([output.get("error") for output in outputs], [None] * 12)
        for output in outputs:
            data = output["data"]
            assert isinstance(data, dict)
            self.assertEqual(data["provider"], "searxng")
            self.assertEqual(data["outcome"], "ok")
        self.assertEqual(tavily.await_count, 0)
        self.assert_spacing(engines)

    async def test_fallbacks_are_ordered_and_individual(self) -> None:
        self.scripts = {engine: "empty" for engine in sx.ENGINES[:-1]}
        output = await self.search()
        self.assertEqual(output["outcome"], "ok")
        self.assertEqual(
            [e for e in self.started if e != "wikipedia"], list(sx.ENGINES)
        )
        self.assertTrue(all("," not in request["engines"] for request in self.seen))

    async def test_configurable_order(self) -> None:
        with patch.dict(
            os.environ, {"ATLAS_SEARXNG_ENGINES": "brave,duckduckgo,qwant"}
        ):
            await self.search()
        self.assertEqual([e for e in self.started if e != "wikipedia"], ["brave"])

    async def test_block_signals_do_not_retry_and_read_retry_after(self) -> None:
        for signal in ("403", "429", "captcha", "engine_captcha"):
            with self.subTest(signal=signal):
                self.state.circuits.clear()
                self.seen.clear()
                self.scripts = {engine: signal for engine in (*sx.ENGINES, "wikipedia")}
                result = await self.search(query=signal)
                self.assertEqual(result["outcome"], "upstream_blocked")
                self.assertEqual(len(self.seen), len(sx.ENGINES) + 1)
                again = await self.search(query=signal + " distinct")
                self.assertEqual(again["outcome"], "upstream_blocked")
                self.assertEqual(len(self.seen), len(sx.ENGINES) + 1)
                if signal in {"403", "429"}:
                    self.assertGreater(
                        self.state.circuits[
                            f"http://127.0.0.1:{self.port}:duckduckgo"
                        ].until
                        - time.monotonic(),
                        119,
                    )

    async def test_wikipedia_survives_blocked_web_engines(self) -> None:
        self.scripts = {engine: "429" for engine in sx.ENGINES}
        result = await self.search(lang="fr")
        self.assertEqual(result["outcome"], "partial_results")
        results = result["results"]
        assert isinstance(results, list)
        self.assertEqual(results[0]["engine"], "wikipedia")
        self.assertTrue(all(request["language"] == "fr-FR" for request in self.seen))

    async def test_distinct_empty_timeout_and_invalid_outcomes(self) -> None:
        for action, expected, timeout in (
            ("empty", "empty_results", 2),
            ("timeout", "upstream_timeout", 0.12),
            ("invalid", "upstream_timeout", 2),
        ):
            with self.subTest(action=action):
                # A fresh state, not circuits.clear(): requests still in flight
                # after the 0.12 s timeout case used to recreate circuits in
                # cooldown, and under load the next case read upstream_blocked.
                self.state = sx.State()
                self.scripts = {engine: action for engine in (*sx.ENGINES, "wikipedia")}
                result = await self.search(query=action, timeout=timeout)
                self.assertEqual(result["outcome"], expected)
                self.assertEqual(result["results"], [])
                self.assertIn("message", result)

    async def test_completed_wikipedia_is_preserved_at_web_deadline(self) -> None:
        self.scripts = {engine: "timeout" for engine in sx.ENGINES}
        wikipedia_gate = asyncio.Event()
        original_engine_search = sx.engine_search

        async def gated_engine_search(
            state: sx.State,
            endpoint: str,
            engine: str,
            params: dict[str, str],
            policy: sx.Policy,
            deadline: float,
            slot: float | None = None,
        ) -> Message:
            if engine == "wikipedia":
                result = await original_engine_search(
                    state, endpoint, engine, params, policy, deadline, slot
                )
                wikipedia_gate.set()
                return result
            await wikipedia_gate.wait()
            return {
                "engine": engine,
                "outcome": "upstream_timeout",
                "results": [],
                "reason": "deadline",
            }

        with patch.object(sx, "engine_search", side_effect=gated_engine_search):
            result = await self.search(query="deadline", timeout=0.12)
        self.assertEqual(self.started, ["wikipedia"])
        self.assertEqual(result["outcome"], "partial_results")
        results = result["results"]
        assert isinstance(results, list)
        self.assertEqual(results[0]["engine"], "wikipedia")

    async def test_partial_notice_reaches_final_answer_and_stream(self) -> None:
        from services.orchestrator import chat_pipeline
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.stream_client import sink_context

        item = Interaction()
        request = ChatRequest(
            ui_locale="en",
            messages=[
                VisionMessage(role="user", content="Tell me the current CERN news")
            ],
        )

        async def complete(request: ChatRequest, item: Interaction) -> None:
            item.state = "done"
            item.reponse = "CERN is a physics laboratory."
            item.erreurs.append("partial_results")

        sink = AsyncMock()
        token = sink_context.set(sink)
        try:
            with patch.object(chat_pipeline, "_process", side_effect=complete):
                await chat_pipeline.process(request, item)
        finally:
            sink_context.reset(token)
        self.assertIn("CERN is a physics laboratory.", item.reponse)
        self.assertIn(sx.NOTICES["partial_results"], item.reponse)
        self.assertTrue(
            any(
                sx.NOTICES["partial_results"] in str(call)
                for call in sink.call_args_list
            )
        )

    async def test_single_flight_survives_cancelled_waiter(self) -> None:
        self.hold = asyncio.Event()
        first = asyncio.create_task(self.search())
        second = asyncio.create_task(self.search())
        for _ in range(100):
            if len(self.started) == 2:
                break
            await asyncio.sleep(0.001)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.hold.set()
        self.assertEqual((await second)["outcome"], "ok")
        self.assertEqual(len(self.seen), 2)
        self.assertEqual(len(self.state.searches), 1)
        self.assertEqual(self.state.flights, {})

    async def test_single_flight_waiter_has_its_own_deadline(self) -> None:
        self.hold = asyncio.Event()
        first = asyncio.create_task(self.search())
        for _ in range(100):
            if len(self.started) == 2:
                break
            await asyncio.sleep(0.001)
        result = await self.search(timeout=0.06)
        self.assertEqual(result["outcome"], "upstream_timeout")
        self.assertEqual(result["results"], [])
        self.assertFalse(first.done())
        self.hold.set()
        self.assertEqual((await first)["outcome"], "ok")
        self.assertEqual(len(self.seen), 2)

    async def test_http_concurrency_never_exceeds_three(self) -> None:
        self.hold = asyncio.Event()
        params = {"q": "CERN", "language": "en", "format": "json"}
        tasks = [
            asyncio.create_task(
                sx.engine_search(
                    self.state,
                    f"http://127.0.0.1:{self.port}",
                    engine,
                    params,
                    sx.Policy(),
                    time.monotonic() + 2,
                )
            )
            for engine in sx.ENGINES
        ]
        for _ in range(100):
            if self.active == 3:
                break
            await asyncio.sleep(0.001)
        self.assertEqual(self.active, 3)
        self.hold.set()
        await asyncio.gather(*tasks)
        self.assertEqual(self.peak, 3)

    async def test_runtime_uses_conversation_language_over_tool_argument(self) -> None:
        runtime = Runtime(
            self.cache,
            Decimal(0),
            "Wat is vandaag het belangrijkste nieuws in Nederland?",
        )
        result = await runtime.search(Search(query="CERN", lang="en"), 2)
        self.assertEqual(result["language"], "nl")
        self.assertTrue(all(request["language"] == "nl-NL" for request in self.seen))

    async def test_explicit_locale_without_conversation_and_query_operators(
        self,
    ) -> None:
        runtime = Runtime(self.cache, Decimal(0))
        result = await runtime.search(
            Search(query="!google CERN :auto <999", lang="de"), 2
        )
        self.assertEqual(result["language"], "de")
        self.assertEqual(set(self.started), {sx.ENGINES[0], "wikipedia"})
        self.assertTrue(all(request["language"] == "de-DE" for request in self.seen))
        self.assertTrue(
            all(
                request["q"] == '"!google" CERN ":auto" "<999"' for request in self.seen
            )
        )
        # "categories" next to "engines" would make SearXNG ask every engine.
        self.assertTrue(all("categories" not in request for request in self.seen))

    async def test_independent_cooldowns_and_health_probes_over_http(self) -> None:
        params = {"q": "CERN", "language": "en", "format": "json"}
        endpoint = f"http://127.0.0.1:{self.port}"
        state = self.state

        async def attempt(engine: str) -> dict[str, object]:
            return await sx.engine_search(
                state, endpoint, engine, params, sx.Policy(), time.monotonic() + 2
            )

        self.scripts["duckduckgo"] = "429"
        for _ in range(3):
            circuit = state.circuits.setdefault(endpoint + ":duckduckgo", sx.Circuit())
            circuit.until = circuit.next_call = 0
            circuit.last_start = float("-inf")
            self.assertEqual(
                (await attempt("duckduckgo"))["outcome"], "upstream_blocked"
            )
        self.assertTrue(circuit.quarantined)
        self.assertEqual((await attempt("brave"))["outcome"], "ok")
        count = len(self.seen)
        self.assertEqual((await attempt("duckduckgo"))["reason"], "quarantined")
        self.assertEqual(len(self.seen), count)
        self.scripts["duckduckgo"] = "ok"
        circuit.until = circuit.next_call = 0
        circuit.last_start = float("-inf")
        self.assertEqual((await attempt("duckduckgo"))["outcome"], "ok")
        self.assertTrue(circuit.quarantined)
        self.assertGreater(circuit.until - time.monotonic(), 59)
        self.assertEqual((await attempt("duckduckgo"))["reason"], "quarantined")
        circuit.until = circuit.next_call = 0
        circuit.last_start = float("-inf")
        self.assertEqual((await attempt("duckduckgo"))["outcome"], "ok")
        self.assertFalse(circuit.quarantined)
        self.assertEqual((await attempt("qwant"))["outcome"], "ok")
        self.assertGreater(
            state.circuits[endpoint + ":qwant"].next_call - time.monotonic(), 29
        )
        self.assertEqual((await attempt("qwant"))["reason"], "cooldown")

    async def test_news_time_range_only_where_the_engine_supports_it(self) -> None:
        # SearXNG skips an engine without time-range support when the request
        # carries one: the news search then read as empty for that engine.
        self.scripts = {engine: "empty" for engine in sx.ENGINES}
        await sx.search(
            self.cache, f"http://127.0.0.1:{self.port}", "CERN", "de", 1, True, 2
        )
        wiki = next(
            request for request in self.seen if request["engines"] == "wikipedia"
        )
        self.assertTrue(all("categories" not in request for request in self.seen))
        for request in self.seen:
            self.assertEqual(
                request.get("time_range"),
                "month" if request["engines"] in sx.TIME_RANGE_ENGINES else None,
            )
        self.assertNotIn("time_range", wiki)
        self.assertEqual(wiki["language"], "de-DE")

    async def test_pipeline_preserves_outcomes_and_partial_sources(self) -> None:
        for outcome in sx.OUTCOMES:
            with self.subTest(outcome=outcome):
                item = Interaction()
                tools = ChatTools(item)
                data = {
                    "provider": "searxng",
                    "outcome": outcome,
                    "results": rows("wikipedia", 1)["results"]
                    if outcome == "partial_results"
                    else [],
                    "message": sx.NOTICES[outcome],
                }
                with patch.object(
                    Runtime,
                    "execute",
                    AsyncMock(return_value={"trust": "untrusted", "data": data}),
                ):
                    call = Call(
                        "s", "web_search", json.dumps({"query": "CERN", "lang": "en"})
                    )
                    if outcome == "partial_results":
                        output = await tools.execute(call, 2)
                        result_data = output["data"]
                        assert isinstance(result_data, dict)
                        self.assertEqual(result_data["outcome"], outcome)
                    else:
                        with self.assertRaises(GatewayError) as raised:
                            await tools.execute(call, 2)
                        self.assertEqual(raised.exception.code, outcome)
                        self.assertEqual(raised.exception.detail, sx.NOTICES[outcome])
                if outcome == "partial_results":
                    # Pages were returned: the search worked, no caveat under
                    # the answer (staging 2026-11-12).
                    self.assertNotIn(outcome, item.erreurs)
                else:
                    self.assertIn(outcome, item.erreurs)

    async def test_pipeline_keeps_earlier_sources_and_adds_localized_caveat(
        self,
    ) -> None:
        from services.orchestrator import chat_pipeline
        from services.orchestrator.chat_schema import ChatRequest

        item = Interaction(retrieved_urls=["https://example.test/earlier"])
        tools = ChatTools(item)
        data = {
            "provider": "searxng",
            "outcome": "upstream_timeout",
            "results": [],
            "message": sx.NOTICES["upstream_timeout"],
        }
        with patch.object(
            Runtime,
            "execute",
            AsyncMock(return_value={"trust": "untrusted", "data": data}),
        ):
            output = await tools.execute(
                Call("s", "web_search", json.dumps({"query": "CERN", "lang": "en"})),
                2,
            )
        self.assertEqual(output["data"], data)
        self.assertIn("upstream_timeout", item.erreurs)

        async def complete(request: ChatRequest, interaction: Interaction) -> None:
            interaction.state = "done"
            interaction.reponse = "CERN is a physics laboratory."

        request = ChatRequest(
            ui_locale="fr",
            messages=[VisionMessage(role="user", content="Actualites du CERN")],
        )
        with patch.object(chat_pipeline, "_process", side_effect=complete):
            await chat_pipeline.process(request, item)
        self.assertIn("CERN is a physics laboratory.", item.reponse)
        self.assertIn(
            "Une partie de la recherche web a échoué (la recherche a dépassé le délai) ; cette réponse repose uniquement sur les sources récupérées avant cet échec.",
            item.reponse,
        )

    async def test_pipeline_returns_503_when_no_result_exists(self) -> None:
        item = Interaction()
        data = {
            "provider": "searxng",
            "outcome": "empty_results",
            "results": [],
        }
        with patch.object(
            Runtime,
            "execute",
            AsyncMock(return_value={"trust": "untrusted", "data": data}),
        ):
            with self.assertRaisesRegex(GatewayError, "empty_results") as raised:
                await ChatTools(item).execute(
                    Call(
                        "s", "web_search", json.dumps({"query": "CERN", "lang": "en"})
                    ),
                    2,
                )
        self.assertEqual(
            raised.exception.detail,
            "The search engines returned no matching results. This does not establish that no information exists.",
        )

    async def test_pipeline_keeps_earlier_sources_after_a_quota_refusal(
        self,
    ) -> None:
        # Staging 2026-11-14: a first search was served with five pages, a
        # second one reached an exhausted provider, and the whole sourced
        # answer was refused with 503. The refusal now closes the research and
        # the answer is written from the pages already retrieved.
        for code, status in (("quota_exceeded", 432), ("search_rate_limited", 429)):
            with self.subTest(code=code):
                refusal = {
                    "error": code,
                    "provider_status": status,
                    "attempts": [
                        {"provider": "searxng", "outcome": "empty_results"},
                        {"provider": "tavily", "outcome": code},
                    ],
                }
                item = Interaction(retrieved_urls=["https://example.test/earlier"])
                with patch.object(Runtime, "execute", AsyncMock(return_value=refusal)):
                    output = await ChatTools(item).execute(
                        Call(
                            "s",
                            "web_search",
                            json.dumps({"query": "CERN", "lang": "en"}),
                        ),
                        2,
                    )
                self.assertEqual(output, refusal)
                self.assertIn(code, item.erreurs)
                self.assertEqual(item.retrieved_urls, ["https://example.test/earlier"])

    async def test_pipeline_refuses_honestly_when_no_provider_served(self) -> None:
        item = Interaction()
        with patch.object(
            Runtime,
            "execute",
            AsyncMock(
                return_value={
                    "error": "quota_exceeded",
                    "provider_status": 432,
                    "attempts": [
                        {"provider": "searxng", "outcome": "empty_results"},
                        {"provider": "tavily", "outcome": "quota_exceeded"},
                        {"provider": "serpapi", "outcome": "global_monthly_search"},
                    ],
                }
            ),
        ):
            with self.assertRaises(GatewayError) as raised:
                await ChatTools(item).execute(
                    Call(
                        "s", "web_search", json.dumps({"query": "CERN", "lang": "en"})
                    ),
                    2,
                )
        self.assertEqual(raised.exception.code, "quota_exceeded")
        self.assertEqual(raised.exception.status, 503)
        self.assertIn("quota exhausted. Provider HTTP 432.", raised.exception.detail)
        self.assertNotIn("provider failed", raised.exception.detail)

    async def test_near_identical_queries_share_one_upstream_search(self) -> None:
        first = await self.search("Public release, history of the printing press")
        self.assertEqual(len(self.seen), 2)
        # Case, accents, punctuation, stop words and order: same cache entry.
        again = await self.search("printing PRESS history: public release")
        self.assertEqual(again, first)
        self.assertEqual(len(self.seen), 2)
        # Another language is another search.
        self.state = sx.State()
        await self.search("printing press history public release", lang="fr")
        self.assertEqual(len(self.seen), 4)

    async def test_concurrent_near_identical_queries_share_one_flight(self) -> None:
        self.hold = asyncio.Event()
        tasks = [
            asyncio.create_task(self.search(query))
            for query in ("Public release!", "release public", "the public release")
        ]
        for _ in range(100):
            if len(self.started) == 2:
                break
            await asyncio.sleep(0.001)
        self.hold.set()
        outputs = await asyncio.gather(*tasks)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])
        self.assertEqual(len(self.seen), 2)

    async def test_cache_lifetime_follows_time_sensitivity_and_degradation(
        self,
    ) -> None:
        lifetimes: list[int] = []
        put = self.cache.put

        def record(key: str, value: dict[str, object], ttl: int) -> None:
            lifetimes.append(ttl)
            put(key, value, ttl)

        async def fresh(query: str, **scripts: str) -> None:
            # A fresh engine state per search: no spacing between them.
            self.state = sx.State()
            self.scripts = scripts
            await self.search(query)

        with patch.object(self.cache, "put", side_effect=record):
            await fresh("history of the printing press")
            await fresh("printing press news today")
            # A short answer with every engine answering is kept as long.
            await fresh("rare manuscript catalogue", **{e: "few" for e in sx.ENGINES})
            # An engine failure makes the result degraded: retried sooner.
            await fresh("medieval bridge engineering", **{sx.ENGINES[0]: "403"})
            await fresh("bridge closures today", **{sx.ENGINES[0]: "403"})
        self.assertEqual(lifetimes, [3 * 86400, 3600, 3 * 86400, 3600, 60])

    async def test_search_asks_the_healthy_engines_first(self) -> None:
        endpoint = f"http://127.0.0.1:{self.port}"
        self.state.health.setdefault(
            endpoint + ":" + sx.ENGINES[0], sx.Health()
        ).demoted_until = time.monotonic() + 600
        with self.assertLogs(sx.__name__, "WARNING") as logs:
            output = await self.search()
        self.assertEqual(output["outcome"], "ok")
        self.assertEqual(
            [engine for engine in self.started if engine != "wikipedia"],
            [sx.ENGINES[1]],
        )
        line = next(entry for entry in logs.output if "searxng_outcome" in entry)
        logged = json.loads(line.split("searxng_outcome ", 1)[1])
        self.assertEqual(logged["order"][-1], sx.ENGINES[0])
        self.assertEqual(logged["cache_ttl"], 3 * 86400)
        self.assertTrue(any("searxng_health" in entry for entry in logs.output))


class SearxngStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_ten_uncached_searches_per_rolling_minute(self) -> None:
        state = sx.State()
        now = [100.0]
        waits: list[float] = []

        async def sleep(delay: float) -> None:
            waits.append(delay)
            now[0] += delay

        with (
            patch.object(time, "monotonic", side_effect=lambda: now[0]),
            patch.object(asyncio, "sleep", side_effect=sleep),
        ):
            for _ in range(11):
                await state.admit(10)
        self.assertEqual(waits, [60])
        self.assertEqual(list(state.searches), [160])

    async def test_admission_never_waits_past_the_deadline(self) -> None:
        state = sx.State()
        now = [100.0]
        with patch.object(time, "monotonic", side_effect=lambda: now[0]):
            for _ in range(10):
                await state.admit(10, 200)
            with self.assertRaises(TimeoutError):
                await state.admit(10, 115)
        self.assertEqual(len(state.searches), 10)
        self.assertEqual(sx.Policy.environment().searches_per_minute, 30)

    def test_three_blocks_quarantine_then_two_spaced_probes(self) -> None:
        circuit = sx.Circuit()
        policy = sx.Policy()
        for now in (100, 160, 220):
            circuit.record("upstream_blocked", now, 0, policy)
        self.assertTrue(circuit.quarantined)
        self.assertEqual(circuit.until, 1120)
        circuit.record("ok", 1120, 0, policy)
        self.assertTrue(circuit.quarantined)
        self.assertEqual(circuit.until, 1180)
        circuit.record("ok", 1180, 0, policy)
        self.assertFalse(circuit.quarantined)
        self.assertEqual(circuit.blocks, 0)

    def test_probe_failure_and_retry_after_extend_quarantine(self) -> None:
        circuit = sx.Circuit(blocks=3, quarantined=True)
        circuit.record("ok", 1000, 0, sx.Policy())
        circuit.record("upstream_blocked", 1060, 4000, sx.Policy())
        self.assertEqual(circuit.until, 5060)
        self.assertEqual(circuit.probes, 0)
        self.assertTrue(circuit.quarantined)
        self.assertEqual(sx.retry_after("120"), 120)
        self.assertEqual(
            sx.retry_after(
                format_datetime(datetime.fromtimestamp(1120, timezone.utc)), 1000
            ),
            120,
        )
        self.assertEqual(sx.retry_after("invalid"), 0)

    def test_language_history_locale_and_six_languages(self) -> None:
        samples = {
            "en": "What are the most recent scientific discoveries in Europe?",
            "fr": "Quelles sont les dernieres decouvertes scientifiques en France ?",
            "de": "Was sind die neuesten wissenschaftlichen Entdeckungen in Deutschland?",
            "es": "Cuales son los descubrimientos cientificos mas recientes en Espana?",
            "it": "Quali sono le scoperte scientifiche piu recenti in Italia?",
            "nl": "Wat zijn de meest recente wetenschappelijke ontdekkingen in Nederland?",
        }
        for language, text in samples.items():
            with self.subTest(language=language):
                messages = [
                    VisionMessage(role="user", content=text.upper()),
                    VisionMessage(role="user", content="CERN?"),
                ]
                self.assertEqual(sx.search_language(messages, "en"), language)
        self.assertEqual(
            sx.search_language([VisionMessage(role="user", content="CERN?")], "nl-NL"),
            "nl",
        )
        self.assertEqual(sx.search_language([], "auto"), "en")
        self.assertEqual(sx.search_language([], "all"), "en")

    def test_language_regions_cover_supported_search_languages(self) -> None:
        self.assertEqual(
            {language: sx.search_region(language) for language in sx.LANGUAGES},
            {
                "fr": "fr-FR",
                "en": "en-US",
                "de": "de-DE",
                "es": "es-ES",
                "it": "it-IT",
                "nl": "nl-NL",
            },
        )

    def test_dutch_ui_locale_is_available_to_search(self) -> None:
        from services.orchestrator.chat_schema import ChatRequest
        from packages.language import conversation_language

        request = ChatRequest(
            ui_locale="nl", messages=[VisionMessage(role="user", content="CERN?")]
        )
        self.assertEqual(
            sx.search_language(request.conversation_messages, request.ui_locale), "nl"
        )
        self.assertEqual(
            conversation_language(request.conversation_messages, request.ui_locale),
            "en",
        )
        continuation = ChatRequest.model_validate(
            {
                "ui_locale": "nl",
                "messages": [
                    {"role": "user", "content": "CERN?"},
                    {"role": "assistant", "content": "CERN is a laboratory."},
                ],
            }
        )
        self.assertTrue(continuation.continuation)

    def test_foreign_engine_rows_cannot_be_misattributed(self) -> None:
        data = rows("google")
        results = data["results"]
        assert isinstance(results, list)
        for row in results:
            row["engine"] = "google"
        self.assertEqual(sx.parse_results(data, "brave")["results"], [])
        self.assertEqual(sx.parse_results(data, "google")["outcome"], "ok")
        for row in results:
            row["engines"] = None
        self.assertEqual(sx.parse_results(data, "brave")["results"], [])

    def test_sidecar_settings_load_every_engine_and_no_default(self) -> None:
        # An engine name SearXNG does not load, or "categories" next to
        # "engines", falls back to the default category. With every engine
        # disabled that fallback reaches no engine instead of all of them.
        import yaml

        settings = yaml.safe_load(Path("infra/searxng/settings.yml").read_text())
        kept = settings["use_default_settings"]["engines"]["keep_only"]
        entries = {entry["name"]: entry for entry in settings["engines"]}
        self.assertEqual(set(kept), {*sx.SUPPORTED, "wikipedia"})
        self.assertEqual(set(entries), set(kept))
        for name, entry in entries.items():
            self.assertIs(entry["disabled"], True, name)
            self.assertIs(entry.get("inactive", False), False, name)
        self.assertEqual(entries["wikipedia"]["display_type"], ["list"])
        self.assertFalse(settings["server"]["limiter"])
        self.assertLessEqual(sx.TIME_RANGE_ENGINES, {*sx.SUPPORTED})
        # Engine lists already deployed in instance environments stay valid.
        with patch.dict(
            os.environ,
            {"ATLAS_SEARXNG_ENGINES": "duckduckgo,bing,brave,qwant,google,startpage"},
        ):
            self.assertEqual(sx.Policy.environment().engines[1], "bing")

    def test_policy_limits_and_circuit_independence(self) -> None:
        self.assertEqual(sx.Policy.environment().fallback_interval, 30)
        self.assertEqual(sx.Policy.environment().engine_interval, 3)
        for setting, value in (
            ("QUARANTINE_SECONDS", "899"),
            ("QUARANTINE_SECONDS", "3601"),
            ("PROBE_SECONDS", "0"),
            ("ENGINE_SECONDS", "2"),
            ("ENGINES", "ecosia"),
        ):
            with patch.dict(os.environ, {"ATLAS_SEARXNG_" + setting: value}):
                with self.assertRaisesRegex(ValueError, "invalid_searxng_policy"):
                    sx.Policy.environment()
        one, two = sx.Circuit(), sx.Circuit()
        one.record("upstream_blocked", 0, 60, sx.Policy())
        self.assertEqual(two.blocks, 0)
        self.assertEqual(two.until, 0)

    def test_frequent_refusals_move_an_engine_to_the_end_then_it_recovers(
        self,
    ) -> None:
        state = sx.State()
        endpoint = "http://searx.test"
        engines = ("startpage", "duckduckgo", "mojeek", "seznam")
        health = state.health.setdefault(endpoint + ":startpage", sx.Health())
        circuit = sx.Circuit()
        demoted = []
        # One refusal in two: the circuit never sees three in a row.
        for index, outcome in enumerate(
            ("upstream_blocked", "ok", "upstream_blocked", "ok")
        ):
            circuit.record(outcome, 100 + index, 0, sx.Policy())
            demoted.append(health.record(outcome, 100 + index))
        self.assertFalse(circuit.quarantined)
        self.assertEqual(demoted, [False, False, False, True])
        self.assertEqual(
            sx.engine_order(state, endpoint, engines, 104),
            ("duckduckgo", "mojeek", "seznam", "startpage"),
        )
        # Calls during the cooldown are recorded but never demote again.
        self.assertFalse(health.record("upstream_blocked", 200))
        self.assertEqual(health.counts(200)["demoted_seconds"], 1703)
        # After the cooldown the configured order returns with a fresh window.
        self.assertEqual(sx.engine_order(state, endpoint, engines, 1903), engines)
        self.assertEqual(health.counts(1903)["asked"], 0)
        self.assertFalse(health.record("upstream_blocked", 1904))

    def test_demotion_needs_enough_calls_and_never_drops_an_engine(self) -> None:
        state = sx.State()
        endpoint = "http://searx.test"
        engines = ("startpage", "duckduckgo")
        for engine in engines:
            health = state.health.setdefault(endpoint + ":" + engine, sx.Health())
            # Three refusals are below the minimum number of calls.
            for now in (1, 2, 3):
                self.assertFalse(health.record("upstream_blocked", now))
        self.assertEqual(sx.engine_order(state, endpoint, engines, 4), engines)
        # Old calls leave the window: they do not count toward a demotion.
        health = state.health[endpoint + ":startpage"]
        self.assertFalse(health.record("upstream_blocked", 1803.5))
        self.assertEqual(health.counts(1803.5)["asked"], 1)
        # Every engine demoted: the configured order applies unchanged.
        for engine in engines:
            state.health[endpoint + ":" + engine].demoted_until = 5000
        self.assertEqual(sx.engine_order(state, endpoint, engines, 10), engines)
        state.health[endpoint + ":startpage"].demoted_until = 0
        self.assertEqual(sx.engine_order(state, endpoint, engines, 10), engines)
        state.health[endpoint + ":startpage"].demoted_until = 5000
        state.health[endpoint + ":duckduckgo"].demoted_until = 0
        self.assertEqual(
            sx.engine_order(state, endpoint, engines, 10), ("duckduckgo", "startpage")
        )
