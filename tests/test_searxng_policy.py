"""Owner search memo: scripted HTTP failures and bounded engine state."""

import asyncio
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
from services.orchestrator.model import GatewayError
from services.orchestrator.tools import Runtime, Search


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
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Cache(Path(self.directory.name) / "cache.sqlite")
        self.state = sx.State()
        self.state_patch = patch.object(sx, "shared_state", return_value=self.state)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.env = patch.dict(
            os.environ,
            {
                "ATLAS_SEARCH_PROVIDER": "searxng",
                "ATLAS_SEARXNG_URL": "http://127.0.0.1:13780",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.scripts: dict[str, str] = {}
        self.seen: list[dict[str, str]] = []
        self.started: list[str] = []
        self.hold: asyncio.Event | None = None
        self.active = 0
        self.peak = 0
        app = web.Application()
        app.router.add_get("/search", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 13780).start()
        self.addAsyncCleanup(self.runner.cleanup)

    async def handle(self, request: web.Request) -> web.Response:
        self.seen.append(dict(request.query))
        engine = request.query["engines"]
        self.started.append(engine)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.hold:
                await self.hold.wait()
            action = self.scripts.get(engine, "ok")
            if action == "timeout":
                await asyncio.sleep(0.2)
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
            self.cache, "http://127.0.0.1:13780", query, lang, 5, False, timeout
        )

    async def test_tier_one_and_wikipedia_parallel_then_cache(self) -> None:
        self.hold = asyncio.Event()
        task = asyncio.create_task(self.search())
        for _ in range(100):
            if len(self.started) == 2:
                break
            await asyncio.sleep(0.001)
        self.assertEqual(set(self.started), {"duckduckgo", "wikipedia"})
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
        self.scripts["duckduckgo"] = "few"
        result = await self.search()
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(
            [e for e in self.started if e != "wikipedia"], ["duckduckgo", "brave"]
        )

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
                self.assertEqual(len(self.seen), 7)
                again = await self.search(query=signal + " distinct")
                self.assertEqual(again["outcome"], "upstream_blocked")
                self.assertEqual(len(self.seen), 7)
                if signal in {"403", "429"}:
                    self.assertGreater(
                        self.state.circuits["http://127.0.0.1:13780:duckduckgo"].until
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
        self.assertTrue(all(request["language"] == "fr" for request in self.seen))

    async def test_distinct_empty_timeout_and_invalid_outcomes(self) -> None:
        for action, expected, timeout in (
            ("empty", "empty_results", 2),
            ("timeout", "upstream_timeout", 0.12),
            ("invalid", "upstream_timeout", 2),
        ):
            with self.subTest(action=action):
                self.state.circuits.clear()
                self.scripts = {engine: action for engine in (*sx.ENGINES, "wikipedia")}
                result = await self.search(query=action, timeout=timeout)
                self.assertEqual(result["outcome"], expected)
                self.assertEqual(result["results"], [])
                self.assertIn("message", result)

    async def test_completed_wikipedia_is_preserved_at_web_deadline(self) -> None:
        self.scripts = {engine: "timeout" for engine in sx.ENGINES}
        result = await self.search(query="deadline", timeout=0.12)
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
                    "http://127.0.0.1:13780",
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
        self.assertTrue(all(request["language"] == "nl" for request in self.seen))

    async def test_explicit_locale_without_conversation_and_query_operators(
        self,
    ) -> None:
        runtime = Runtime(self.cache, Decimal(0))
        result = await runtime.search(
            Search(query="!google CERN :auto <999", lang="de"), 2
        )
        self.assertEqual(result["language"], "de")
        self.assertEqual(set(self.started), {"duckduckgo", "wikipedia"})
        self.assertTrue(all(request["language"] == "de" for request in self.seen))
        self.assertTrue(
            all(
                request["q"] == '"!google" CERN ":auto" "<999"' for request in self.seen
            )
        )
        self.assertTrue(all("categories" not in request for request in self.seen))

    async def test_independent_cooldowns_and_health_probes_over_http(self) -> None:
        params = {"q": "CERN", "language": "en", "format": "json"}
        endpoint = "http://127.0.0.1:13780"
        state = self.state

        async def attempt(engine: str) -> dict[str, object]:
            return await sx.engine_search(
                state, endpoint, engine, params, sx.Policy(), time.monotonic() + 2
            )

        self.scripts["duckduckgo"] = "429"
        for _ in range(3):
            circuit = state.circuits.setdefault(endpoint + ":duckduckgo", sx.Circuit())
            circuit.until = circuit.next_call = 0
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
        self.assertEqual((await attempt("duckduckgo"))["outcome"], "ok")
        self.assertTrue(circuit.quarantined)
        self.assertGreater(circuit.until - time.monotonic(), 59)
        self.assertEqual((await attempt("duckduckgo"))["reason"], "quarantined")
        circuit.until = circuit.next_call = 0
        self.assertEqual((await attempt("duckduckgo"))["outcome"], "ok")
        self.assertFalse(circuit.quarantined)
        self.assertEqual((await attempt("qwant"))["outcome"], "ok")
        self.assertGreater(
            state.circuits[endpoint + ":qwant"].next_call - time.monotonic(), 29
        )
        self.assertEqual((await attempt("qwant"))["reason"], "cooldown")

    async def test_news_keeps_wikipedia_in_general_category(self) -> None:
        await sx.search(self.cache, "http://127.0.0.1:13780", "CERN", "de", 1, True, 2)
        wiki = next(
            request for request in self.seen if request["engines"] == "wikipedia"
        )
        self.assertNotIn("categories", wiki)
        self.assertTrue(all("categories" not in request for request in self.seen))
        self.assertNotIn("time_range", wiki)
        self.assertEqual(wiki["language"], "de")

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
            "Une partie de la recherche web a échoué (upstream_timeout) ; cette réponse repose uniquement sur les sources récupérées avant cet échec.",
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

    async def test_pipeline_keeps_quota_refusal_terminal_with_earlier_sources(
        self,
    ) -> None:
        item = Interaction(retrieved_urls=["https://example.test/earlier"])
        with patch.object(
            Runtime,
            "execute",
            AsyncMock(
                return_value={
                    "error": "quota_exceeded",
                    "provider_status": 429,
                    "attempts": [{"provider": "tavily", "outcome": "quota_exceeded"}],
                }
            ),
        ):
            with self.assertRaisesRegex(GatewayError, "quota_exceeded"):
                await ChatTools(item).execute(
                    Call(
                        "s", "web_search", json.dumps({"query": "CERN", "lang": "en"})
                    ),
                    2,
                )


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
                await state.admit()
        self.assertEqual(waits, [60])
        self.assertEqual(list(state.searches), [160])

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

    def test_policy_limits_and_circuit_independence(self) -> None:
        self.assertEqual(sx.Policy.environment().fallback_interval, 30)
        for setting, value in (
            ("QUARANTINE_SECONDS", "899"),
            ("QUARANTINE_SECONDS", "3601"),
            ("PROBE_SECONDS", "0"),
            ("ENGINES", "ecosia"),
        ):
            with patch.dict(os.environ, {"ATLAS_SEARXNG_" + setting: value}):
                with self.assertRaisesRegex(ValueError, "invalid_searxng_policy"):
                    sx.Policy.environment()
        one, two = sx.Circuit(), sx.Circuit()
        one.record("upstream_blocked", 0, 60, sx.Policy())
        self.assertEqual(two.blocks, 0)
        self.assertEqual(two.until, 0)
