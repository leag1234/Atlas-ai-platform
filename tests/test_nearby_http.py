"""Recorded public OSM journeys through the chat HTTP boundary, JSON and SSE."""

from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from services.orchestrator.chat_api import app
from services.orchestrator.location import Position
from services.orchestrator.nearby import OSM
from services.orchestrator.nearby_chat import NearbyTools

FIXTURES = Path(__file__).parent / "fixtures/nearby"


class NearbyHTTPTests(unittest.TestCase):
    def test_recorded_journeys_json_stream_and_refusal(self) -> None:
        receipts = []
        for case in (
            "paris-bakery",
            "paris-convenience",
            "lyon-pharmacy",
            "rural",
            "abroad",
            "english",
        ):
            record = json.loads((FIXTURES / (case + ".json")).read_text())
            rows = [x for x in record["exchanges"] if x["service"] == "geocode"]
            geocoded = next(
                (
                    x["response"][0]
                    for x in rows
                    if x.get("category") != record["question"]
                    and x.get("response")
                    and "address" in x["response"][0]
                    and x["response"][0].get("type")
                    not in {"bakery", "convenience", "pharmacy"}
                ),
                None,
            )
            if case in {"lyon-pharmacy", "rural", "abroad"}:
                geocoded = rows[0]["response"][0]
            lat, lon = (
                (float(geocoded["lat"]), float(geocoded["lon"]))
                if geocoded
                else (48.8566, 2.3522)
            )
            question = record["question"] + (
                " ouverte près de moi" if record["language"] == "fr" else " open nearby"
            )
            question += " within 3 km"
            if record["language"] == "de":
                question = "Apotheke in der Nähe geöffnet im Umkreis von 3 km"
            for stream in (False, True):
                with (
                    self.subTest(case=case, stream=stream),
                    tempfile.TemporaryDirectory(dir="BRAIN") as root,
                ):
                    env = {
                        "ATLAS_OSM_STATE": root,
                        "ATLAS_WEB_CACHE": root + "/web.sqlite",
                        "ATLAS_INTERACTION_DIR": root + "/receipts",
                        "ATLAS_OVERPASS_URLS": "",
                        "ATLAS_PORT_BASE": "28000",
                    }
                    with patch.dict(os.environ, env):
                        osm = OSM()

                    async def recorded_request(
                        service: str,
                        parameters: dict[str, str],
                        endpoint: str | None = None,
                    ) -> Any:
                        matches = [
                            x["response"]
                            for x in rows
                            if x.get("category") == parameters["q"]
                        ]
                        if not matches:
                            self.fail("Unrecorded provider query: " + parameters["q"])
                        return matches[0]

                    # Freeze only the clock and external transports. Routing,
                    # validation, HTTP, schedules, ranking and citations are real.
                    def bound(location: Any, search: Any, **kwargs: Any) -> NearbyTools:
                        return NearbyTools(
                            location,
                            search,
                            osm,
                            datetime.fromisoformat(record["recorded_at"]),
                            **kwargs,
                        )

                    with (
                        patch.dict(os.environ, env),
                        patch.object(osm, "request", side_effect=recorded_request),
                        patch(
                            "services.orchestrator.nearby_chat.NearbyTools",
                            side_effect=bound,
                        ),
                        patch(
                            "services.orchestrator.model.GatewayModel.connect",
                            AsyncMock(return_value=None),
                        ),
                        patch(
                            "services.orchestrator.chat_pipeline.ChatTools.execute",
                            AsyncMock(
                                return_value={"error": "web_verification_unavailable"}
                            ),
                        ),
                        TestClient(app) as client,
                    ):
                        body = dict(
                            messages=[dict(role="user", content=question)],
                            ui_locale=record["language"],
                            stream=stream,
                            location=dict(
                                consent="once",
                                position=dict(latitude=lat, longitude=lon),
                            ),
                        )
                        response = client.post("/v1/chat/completions", json=body)
                        self.assertEqual(response.status_code, 200)
                        if stream:
                            events = [
                                json.loads(line[6:])
                                for line in response.text.splitlines()
                                if line.startswith("data: ") and line != "data: [DONE]"
                            ]
                            answer = "".join(
                                choice.get("delta", {}).get("content", "")
                                for event in events
                                for choice in event.get("choices", [])
                            )
                        else:
                            answer = response.json()["choices"][0]["message"]["content"]
                        self.assertIn("OpenStreetMap", answer)
                        self.assertIn("https://www.openstreetmap.org/", answer)
                        self.assertIn("nominatim", answer)
                        self.assertTrue(
                            any(p["name"] in answer for p in record["candidates"]),
                            answer,
                        )
                        self.assertNotIn("issues de sources .", answer)
                        self.assertNotIn(
                            str(Position(latitude=lat, longitude=lon).longitude), answer
                        )
                        receipts.append(
                            dict(
                                case=case,
                                stream=stream,
                                status=response.status_code,
                                answer=answer,
                                evidence="recorded OSM; search unavailable; model not dispatched",
                            )
                        )
                        if case == "lyon-pharmacy" and not stream:
                            body["location"] = dict(consent="refused")
                            refused = client.post("/v1/chat/completions", json=body)
                            self.assertIn(
                                "Indiquez un lieu",
                                refused.json()["choices"][0]["message"]["content"],
                            )
                            body["location"] = dict(
                                consent="refused", place=rows[0]["category"]
                            )
                            typed = client.post("/v1/chat/completions", json=body)
                            typed_answer = typed.json()["choices"][0]["message"][
                                "content"
                            ]
                            self.assertIn("OpenStreetMap", typed_answer)
                            self.assertTrue(
                                any(
                                    p["name"] in typed_answer
                                    for p in record["candidates"]
                                )
                            )
                            receipts.append(
                                dict(
                                    case="refused-then-typed",
                                    status=typed.status_code,
                                    answer=typed_answer,
                                )
                            )
        Path("BRAIN/nearby-2-http-journeys.json").write_text(
            json.dumps(receipts, ensure_ascii=False, indent=2)
        )

    def test_location_ticket_is_single_use_ephemeral_and_origin_checked(self) -> None:
        from services.orchestrator.location import location_tickets

        with TestClient(app) as client:
            location = dict(
                consent="once", position=dict(latitude=48.856612, longitude=2.352222)
            )
            issued = client.post("/api/atlas/nearby-location", json=location)
            self.assertEqual(issued.status_code, 200)
            token = issued.json()["location_ref"]
            self.assertNotIn("latitude", issued.text)
            self.assertNotIn("48.856", issued.text)
            self.assertEqual(issued.headers["cache-control"], "no-store")
            resolved = location_tickets.take(token)
            self.assertIsNotNone(resolved)
            self.assertIsNone(location_tickets.take(token))
            denied = client.post(
                "/api/atlas/nearby-location",
                json=location,
                headers={"Origin": "https://foreign.example"},
            )
            self.assertEqual(denied.status_code, 403)
            malformed = client.post(
                "/api/atlas/nearby-location",
                json=dict(consent="denied", position=location["position"]),
            )
            self.assertEqual(malformed.status_code, 400)
            self.assertNotIn("48.856", malformed.text)
            another = client.post("/api/atlas/nearby-location", json=location).json()[
                "location_ref"
            ]
            removed = client.delete("/api/atlas/nearby-location/" + another)
            self.assertEqual(removed.status_code, 204)
            self.assertIsNone(location_tickets.take(another))
