"""REQ-NEARBY-01: source failures, public recordings and scoped tool discovery."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator.cache import Cache
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.location import Location, Position
from services.orchestrator.loop import Call
from services.orchestrator.nearby import NearbyError, OSM
from services.orchestrator.nearby_chat import NearbyTools, process_nearby, render
from services.orchestrator.tools import Runtime

FIXTURES = Path(__file__).parent / "fixtures/nearby"


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = tempfile.TemporaryDirectory(dir="BRAIN")
        self.addCleanup(self.root.cleanup)
        environment = patch.dict(
            os.environ,
            {
                "ATLAS_OSM_STATE": self.root.name,
                "ATLAS_OVERPASS_URLS": "https://one.example/api,https://two.example/api",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.osm = OSM()
        self.now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
        self.origin = Position(latitude=48.8566, longitude=2.3522)

    async def test_chain_one_attempt_each_language_tags_and_cache(self) -> None:
        record = json.loads((FIXTURES / "paris-bakery.json").read_text())
        response = next(
            x["response"] for x in record["exchanges"] if x["service"] == "geocode"
        )
        with patch.object(
            self.osm,
            "request",
            AsyncMock(
                side_effect=[
                    NearbyError("osm_unavailable"),
                    NearbyError("osm_http_502"),
                    response,
                ]
            ),
        ) as request:
            result = await self.osm.candidates(
                self.origin, "boulangerie", 3000, self.now, "fr"
            )
        self.assertEqual(request.await_count, 3)
        self.assertEqual(
            [c.args[0] for c in request.await_args_list],
            ["overpass", "overpass", "geocode"],
        )
        args = request.await_args_list[2].args[1]
        self.assertEqual(args["q"], "boulangerie")
        self.assertEqual(args["bounded"], "1")
        self.assertEqual(args["extratags"], "1")
        self.assertEqual(self.osm.served_by, "nominatim")
        self.assertTrue(result)
        self.assertTrue(
            all(
                p["category"] == "bakery" and p["provider"] == "nominatim"
                for p in result
            )
        )
        with (
            patch.dict(os.environ, {"ATLAS_OVERPASS_URLS": ""}),
            patch.object(self.osm, "request", AsyncMock()) as cached_request,
        ):
            repeated = await self.osm.candidates(
                self.origin, "boulangerie", 3000, self.now, "fr"
            )
        cached_request.assert_not_called()
        self.assertEqual(result, repeated)
        with self.osm.cache.connection() as db:
            rows = db.execute("select key,value from cache").fetchall()
        self.assertNotIn("distance_m", str(rows))
        self.assertNotIn(str(self.origin.longitude), str(rows))
        self.assertNotIn("position", str(rows))

    async def test_web_fallback_and_closure_conflict(self) -> None:
        runtime = Runtime(Cache(Path(self.root.name) / "web.sqlite"), Decimal("0.01"))
        tool = NearbyTools(
            Location(consent="once", position=self.origin), runtime, self.osm, self.now
        )
        tool.area = "Paris"
        answer: Any
        with (
            patch.object(self.osm, "candidates", AsyncMock(return_value=[])),
            patch.object(
                runtime,
                "execute",
                AsyncMock(
                    return_value={
                        "data": {
                            "results": [
                                {
                                    "title": "Public pharmacy",
                                    "link": "https://example.org/pharmacy",
                                    "snippet": "Call for current hours",
                                }
                            ]
                        }
                    }
                ),
            ) as search,
        ):
            answer = await tool.execute(
                Call("n", "nearby_places", '{"category":"pharmacy"}'), 10
            )
        self.assertEqual(answer["data"]["source"], "web_search")
        self.assertIn("Public pharmacy", render(answer["data"], "en"))
        self.assertIn("hours are unknown", render(answer["data"], "en"))
        self.assertNotIn(str(self.origin.latitude), search.call_args.args[0].arguments)
        self.assertEqual(tool.cost, Decimal("0.01"))

        record = json.loads((FIXTURES / "paris-bakery.json").read_text())
        candidate = next(
            p for p in record["candidates"] if p["opening"]["status"] == "open"
        )
        other = NearbyTools(
            Location(consent="once", position=self.origin), runtime, self.osm, self.now
        )
        with (
            patch.object(self.osm, "candidates", AsyncMock(return_value=[candidate])),
            patch.object(
                runtime,
                "execute",
                AsyncMock(
                    return_value={
                        "data": {"results": [{"snippet": "Temporarily closed"}]}
                    }
                ),
            ),
        ):
            answer = await other.execute(
                Call("n", "nearby_places", '{"category":"bakery"}'), 10
            )
        self.assertEqual(answer["data"]["places"][0]["opening"]["status"], "unknown")
        self.assertIn("web_schedule_conflict", answer["data"]["warnings"])

    async def test_cancel_clears_location(self) -> None:
        location = Location(consent="once", position=self.origin)
        request = ChatRequest(
            messages=[ChatMessage(role="user", content="Nearby bakery open?")],
            location=location,
        )
        with patch(
            "services.orchestrator.nearby_chat.NearbyTools.execute",
            AsyncMock(side_effect=asyncio.CancelledError),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await process_nearby(request, Interaction())
        self.assertIsNone(request.location)
        self.assertIsNone(location.position)

    async def test_project_retains_scoped_lifecycle(self) -> None:
        from services.orchestrator.project_chat import process_project

        request = ChatRequest(
            messages=[ChatMessage(role="user", content="Nearby bakery open?")],
            org_id="00000000-0000-0000-0000-000000000001",
            project_id="00000000-0000-0000-0000-000000000002",
            conversation_id="00000000-0000-0000-0000-000000000003",
        )
        with (
            patch(
                "services.orchestrator.project_transcript.process_scoped_project",
                AsyncMock(),
            ) as scoped,
            patch(
                "services.orchestrator.nearby_chat.process_nearby", AsyncMock()
            ) as nearby,
        ):
            await process_project(request, Interaction())
        scoped.assert_awaited_once()
        nearby.assert_not_called()

    async def test_keyless_recorded_places_and_language(self) -> None:
        for case in (
            "paris-bakery",
            "paris-convenience",
            "lyon-pharmacy",
            "rural",
            "abroad",
            "english",
        ):
            with self.subTest(case=case):
                record = json.loads((FIXTURES / (case + ".json")).read_text())
                self.assertEqual(record["source"], "nominatim")
                self.assertTrue(record["candidates"])
                self.assertTrue(any(x.get("response") for x in record["exchanges"]))
                public = record["candidates"]
                answer = render(
                    dict(
                        places=public[:5],
                        open_count=sum(
                            p["opening"]["status"] == "open" for p in public
                        ),
                        warnings=[],
                        source="nominatim",
                    ),
                    record["language"],
                )
                self.assertIn(public[0]["name"], answer)
                self.assertIn("OpenStreetMap", answer)
                self.assertIn("m —", answer)
                self.assertNotIn("issues de sources .", answer)

    async def test_policy_shared_rate_lock_identification_and_chunked_http(
        self,
    ) -> None:
        from aiohttp import web
        import time

        arrivals: list[float] = []
        agents: list[str] = []

        async def search(request: web.Request) -> web.StreamResponse:
            arrivals.append(time.monotonic())
            agents.append(request.headers.get("User-Agent", ""))
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(b'[{"osm_id":')
            await asyncio.sleep(0.01)
            await response.write(b"123}]")
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_get("/search", search)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            address = runner.addresses[0]
            endpoint = f"http://127.0.0.1:{address[1]}/search"
            first, second = OSM(), OSM()
            with patch.dict(os.environ, {"ATLAS_NOMINATIM_URL": endpoint}):
                results = await asyncio.gather(
                    first.request("geocode", {"q": "public town"}),
                    second.request("geocode", {"q": "bakery"}),
                )
            self.assertEqual(results, [[{"osm_id": 123}], [{"osm_id": 123}]])
            self.assertGreaterEqual(arrivals[1] - arrivals[0], 1.0)
            self.assertTrue(
                all(
                    "https://github.com/leag1234/atlas-ai-platform" in agent
                    for agent in agents
                )
            )
        finally:
            await runner.cleanup()

    def test_attachment_paths_and_unrelated_nearby_are_not_location_intent(
        self,
    ) -> None:
        from services.orchestrator.location import location_dependent

        self.assertFalse(
            location_dependent(
                '<attached_files><file url="/nearby/bakery.pdf" /></attached_files> Summarize the report'
            )
        )
        self.assertFalse(location_dependent("Find the closest integer to 1.2"))
        self.assertTrue(location_dependent("Nearest bakery open?"))

    async def test_work_calls_do_not_reuse_another_category_result(self) -> None:
        runtime = Runtime(Cache(Path(self.root.name) / "web.sqlite"), Decimal(0))
        tools = NearbyTools(
            Location(consent="once", position=self.origin), runtime, self.osm
        )
        with patch.object(
            self.osm, "candidates", AsyncMock(return_value=[])
        ) as candidates:
            await tools.execute(
                Call("first", "nearby_places", '{"category":"bakery"}'), 10
            )
            await tools.execute(
                Call("second", "nearby_places", '{"category":"pharmacy"}'), 10
            )
            await tools.execute(
                Call("repeat", "nearby_places", '{"category":"pharmacy"}'), 10
            )
        self.assertEqual(candidates.await_count, 2)
        self.assertEqual(candidates.await_args_list[1].args[1], "pharmacy")

    def test_location_marker_uses_closed_transcript_content_contract(self) -> None:
        from services.orchestrator.project_transcript import answer_refs
        from services.orchestrator.location import LEDGER_LABEL

        item = Interaction(
            reponse="Public answer. " + LEDGER_LABEL, location_used=LEDGER_LABEL
        )
        refs = answer_refs(item)
        self.assertEqual(
            refs,
            [
                dict(kind="answer", text="Public answer.", file_version_id=None),
                dict(kind="answer", text=LEDGER_LABEL, file_version_id=None),
            ],
        )
        self.assertNotIn("location_used", refs[0])
        self.assertEqual(
            answer_refs(Interaction(reponse="Public answer.")),
            [dict(kind="answer", text="Public answer.", file_version_id=None)],
        )

    def test_real_scoped_finish_persists_only_coarse_location_event(self) -> None:
        from test_project_conversations import ProjectConversationTests
        from services.orchestrator.project_transcript import answer_refs
        from services.orchestrator.location import LEDGER_LABEL
        from services.retrieval.project_scoped_api import store

        fixture = ProjectConversationTests("runTest")
        fixture.setUpClass()
        try:
            fixture.setUp()
            self.assertEqual(fixture.begin().status_code, 201)
            body = fixture.finish_body()
            body["content_refs"] = answer_refs(
                Interaction(
                    reponse="Checked public places. " + LEDGER_LABEL,
                    location_used=LEDGER_LABEL,
                )
            )
            finished = fixture.broker("finish", body, key="nearby-finish")
            self.assertEqual(finished.status_code, 200, finished.text)
            repeated = fixture.broker("finish", body, key="nearby-finish")
            self.assertEqual(repeated.status_code, 200)
            with store(fixture.app_dsn).scope(
                fixture.identity("viewer"), fixture.org, fixture.project
            ) as scope:
                rows = scope.db.execute(
                    "SELECT data FROM atlas_projects.ledger_events WHERE data->>'kind'='location_used'"
                ).fetchall()
                self.assertEqual(len(rows), 1)
                event = rows[0]["data"]
                self.assertEqual(event["source"], dict(kind="device", ref=LEDGER_LABEL))
                self.assertEqual(event["resource_refs"], [])
                self.assertIsNone(event["model_dispatch"])
                self.assertEqual(event["conversation_id"], fixture.conversation)
                self.assertNotIn("latitude", json.dumps(event))
                self.assertNotIn("longitude", json.dumps(event))
        finally:
            fixture.doCleanups()
            fixture.doClassCleanups()
