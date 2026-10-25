"""Request-bound tool tests; synthetic provider data lives only in tests."""

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
from services.orchestrator.location import LEDGER_LABEL, Location, Position
from services.orchestrator.loop import Call
from services.orchestrator.nearby import OSM
from services.orchestrator.nearby_chat import NearbyTools, render
from services.orchestrator.tools import Runtime


class NearbyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_boundary_and_private_cache(self) -> None:
        with (
            tempfile.TemporaryDirectory(dir="BRAIN") as root,
            patch.dict(os.environ, {"ATLAS_OSM_STATE": root}),
        ):
            osm = OSM()
            origin = Position(latitude=48.856612, longitude=2.352222)
            public = dict(
                type="node",
                id=123,
                lat=48.8578,
                lon=2.3533,
                tags={
                    "name": "Public bakery",
                    "shop": "bakery",
                    "opening_hours": "24/7",
                    "addr:street": "Public Street",
                },
            )
            provider = AsyncMock(return_value={"elements": [public]})
            with patch.object(osm, "request", provider):
                candidates = await osm.candidates(
                    origin, "bakery", 1500, datetime.now(timezone.utc)
                )
                self.assertEqual(candidates[0]["name"], "Public bakery")
                self.assertEqual(candidates[0]["opening"]["status"], "open")
                self.assertLess(candidates[0]["distance_m"], 300)
                await osm.candidates(
                    Position(latitude=48.8566, longitude=2.354),
                    "bakery",
                    1500,
                    datetime.now(timezone.utc),
                )
                self.assertEqual(provider.await_count, 1)
            serialized = json.dumps(candidates)
            self.assertNotIn(str(origin.latitude), serialized)
            self.assertNotIn(str(origin.longitude), serialized)
            with osm.cache.connection() as db:
                rows = db.execute("select key,value from cache").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertNotIn("distance_m", str(rows))
            self.assertNotIn(str(origin.longitude), str(rows))
            self.assertNotIn("48.856612", str(rows))

    async def test_refused_then_typed_and_web_privacy(self) -> None:
        with (
            tempfile.TemporaryDirectory(dir="BRAIN") as root,
            patch.dict(os.environ, {"ATLAS_OSM_STATE": root}),
        ):
            osm = OSM()
            search = Runtime(Cache(Path(root) / "search.sqlite"), Decimal(0))
            location = Location(consent="refused", place="private typed place")
            tools = NearbyTools(location, search, osm)
            candidate: dict[str, Any] = dict(
                name="Public pharmacy",
                category="pharmacy",
                address="Business Avenue",
                phone="",
                website="",
                source="https://www.openstreetmap.org/node/123",
                distance_m=200,
                opening_hours=None,
                opening=dict(
                    status="unknown",
                    closing_time=None,
                    next_opening=None,
                    closing_soon=False,
                ),
            )
            with (
                patch.object(
                    osm,
                    "geocode",
                    AsyncMock(
                        return_value=(
                            Position(latitude=45.7581, longitude=4.8321),
                            "Lyon",
                        )
                    ),
                ),
                patch.object(osm, "candidates", AsyncMock(return_value=[candidate])),
                patch.object(
                    search,
                    "execute",
                    AsyncMock(
                        return_value={"trust": "untrusted", "data": {"results": []}}
                    ),
                ) as web,
            ):
                result = await tools.execute(
                    Call("n", "nearby_places", '{"category":"pharmacy"}'), 10
                )
                wire = json.dumps(result)
                self.assertNotIn("private typed place", wire)
                self.assertNotIn("45.758", wire)
                self.assertNotIn("4.832", wire)
                self.assertIn(LEDGER_LABEL, wire)
                sent = web.call_args.args[0].arguments
                self.assertIn("Public pharmacy", sent)
                self.assertNotIn("private typed place", sent)
                self.assertNotIn("45.758", sent)
                self.assertLessEqual(tools.calls, 8)
                assert tools.result
                answer = render(tools.result, "en")
                self.assertIn("Hours not listed", answer)
                self.assertIn("200 m", answer)
                self.assertIn("0 place(s)", answer)
                self.assertNotIn("Paris", answer)

    async def test_no_invented_location_and_bounded_calls(self) -> None:
        with (
            tempfile.TemporaryDirectory(dir="BRAIN") as root,
            patch.dict(os.environ, {"ATLAS_OSM_STATE": root}),
        ):
            search = Runtime(Cache(Path(root) / "search.sqlite"), Decimal(0))
            tools = NearbyTools(Location(consent="denied"), search)
            result = await tools.execute(Call("g", "geocode", '{"place":"Paris"}'), 10)
            self.assertEqual(result["error"], "explicit_place_required")
            result = await tools.execute(
                Call("n", "nearby_places", '{"category":"bakery"}'), 10
            )
            self.assertEqual(result["error"], "location_required")
            tools.calls = 8
            result = await tools.execute(
                Call("n", "nearby_places", '{"category":"bakery"}'), 10
            )
            self.assertEqual(result["error"], "nearby_tool_limit")
            self.assertEqual(tools.calls, 8)
