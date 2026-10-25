"""REQ-NEARBY-01: supervisor presentation defects, 2026-10-25."""

from copy import deepcopy
import re
import unittest
from typing import Any

from services.orchestrator.nearby_chat import render


def sample() -> dict[str, Any]:
    places = []
    for index, (state, meters) in enumerate(
        (("open", 350), ("open", 2500), ("closed", 700)), 1
    ):
        places.append(
            dict(
                name=f"Bakery {index}",
                category="bakery",
                distance_m=meters,
                address="10 Main Street",
                country="FR",
                phone="+33143314066",
                website="https://shop.example/",
                source=f"https://www.openstreetmap.org/node/{index}",
                opening=dict(
                    status=state,
                    timezone="Europe/Paris",
                    closing_soon=False,
                    closing_time="2026-10-25T18:30:00+01:00"
                    if state == "open"
                    else None,
                    next_opening="2026-10-26T07:00:00+01:00"
                    if state == "closed"
                    else None,
                ),
            )
        )
    return dict(
        places=places,
        open_count=2,
        warnings=[],
        source="nominatim",
        evaluated_at="2026-10-25T12:00:00+00:00",
    )


class NearbyPresentationTests(unittest.TestCase):
    def test_french_local_times_distances_groups_footer_and_phone(self) -> None:
        answer = render(sample(), "fr")
        for expected in (
            "ouvert, ferme à 18 h 30",
            "fermé, ouvre demain à 7 h 00",
            "≈ 350 m",
            "≈ 2,5 km",
            "Ouverts maintenant",
            "Ouverture plus tard",
            "Position approximative, utilisée pour cette demande uniquement",
            "Données © contributeurs OpenStreetMap (ODbL)",
            "Téléphone : +33 1 43 31 40 66",
        ):
            self.assertIn(expected, answer)
        self.assertEqual(answer.count("à vol d’oiseau"), 1)
        self.assertEqual(len(re.findall(r"^- \*\*", answer, re.M)), 3)
        self.assertLess(answer.index("Bakery 2"), answer.index("Ouverture plus tard"))
        self.assertNotRegex(
            answer, r"\d{4}-\d\d-\d\dT|Source: nominatim|location used:"
        )
        self.assertNotRegex(answer, r"\)\s*Téléphone")
        self.assertNotRegex(
            re.sub(r"\[[^\]]+\]\(https?://[^)]+\)", "", answer), r"https?://"
        )
        self.assertRegex(
            answer, r"\[OpenStreetMap\]\(https://www.openstreetmap.org/node/1\) \[1\]"
        )

    def test_english_and_foreign_timezones(self) -> None:
        result = sample()
        answer = render(result, "en")
        self.assertIn("open, closes at 6:30 pm", answer)
        self.assertIn("closed, opens tomorrow at 7:00 am", answer)
        self.assertIn("≈ 2.5 km", answer)
        self.assertIn("Approximate location, used for this request only", answer)
        result["evaluated_at"] = "2026-10-26T02:00:00+00:00"
        hours = result["places"][0]["opening"]
        hours.update(
            timezone="America/New_York", closing_time="2026-10-26T03:30:00+00:00"
        )
        self.assertIn("closes at 11:30 pm", render(result, "en"))
        self.assertNotIn("closes tomorrow", render(result, "en"))

    def test_far_open_choices_offer_a_changed_area_without_walking_claim(self) -> None:
        result = sample()
        result["places"][0]["distance_m"] = 1800
        for language, words in (
            ("fr", ("éloignés", "élargir", "réduire", "à pied")),
            ("en", ("far", "widen", "narrow", "walking")),
        ):
            answer = render(result, language)
            for word in words:
                self.assertIn(word, answer)
        result["places"][0]["distance_m"] = 1500
        self.assertNotIn("éloignés", render(result, "fr"))

    def test_other_supported_languages_and_later_dates(self) -> None:
        result = sample()
        for language, expected in (
            ("de", "öffnet morgen"),
            ("es", "abre mañana"),
            ("it", "apre domani"),
        ):
            answer = render(result, language)
            self.assertIn(expected, answer)
            self.assertIn("2,5 km", answer)
            self.assertNotIn("location used:", answer)
        result["places"][2]["opening"]["next_opening"] = "2026-10-28T07:00:00+01:00"
        self.assertIn("28 octobre 2026", render(result, "fr"))
        self.assertIn("October 28, 2026", render(result, "en"))

    def test_phone_country_is_business_country_not_answer_language(self) -> None:
        for raw, country, expected in (
            ("0143314066", "FR", "+33 1 43 31 40 66"),
            ("02083661177", "GB", "+44 20 8366 1177"),
            ("030123456", "DE", "+49 30 123456"),
            ("+16502532222", "", "+1 650-253-2222"),
        ):
            result = sample()
            result["places"][0].update(phone=raw, country=country)
            self.assertIn(expected, render(result, "en"))
        result["places"][0].update(phone="123", country="")
        self.assertIn("123", render(result, "en"))

    def test_safe_provider_text_unknown_hours_and_missing_time(self) -> None:
        result = sample()
        result["places"][0].update(
            name="Shop https://evil.example/ <script>", phone="123"
        )
        result["places"][0]["opening"].update(status="unknown", closing_time=None)
        answer = render(result, "fr")
        self.assertNotIn("evil.example", answer)
        self.assertIn("Horaires non renseignés", answer)
        self.assertIn("Ouverture plus tard", answer)
        other = deepcopy(result)
        other["places"][1]["opening"]["closing_time"] = "not-a-date"
        self.assertNotIn("not-a-date", render(other, "fr"))
        other["places"][1]["opening"]["closing_time"] = "2026-10-26T01:00:00+01:00"
        self.assertIn("ferme demain à 1 h 00", render(other, "fr"))


class CountryMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_nominatim_country_reaches_phone_formatter(self) -> None:
        import json
        import os
        from pathlib import Path
        import tempfile
        from datetime import datetime
        from unittest.mock import AsyncMock, patch

        from services.orchestrator.location import Position
        from services.orchestrator.nearby import OSM

        record = json.loads(Path("tests/fixtures/nearby/paris-bakery.json").read_text())
        rows = next(
            x["response"] for x in record["exchanges"] if x["service"] == "geocode"
        )
        with (
            tempfile.TemporaryDirectory(dir="BRAIN") as root,
            patch.dict(
                os.environ, {"ATLAS_OSM_STATE": root, "ATLAS_OVERPASS_URLS": ""}
            ),
        ):
            osm = OSM()
            with patch.object(osm, "request", AsyncMock(return_value=rows)):
                places = await osm.candidates(
                    Position(latitude=48.8566, longitude=2.3522),
                    "bakery",
                    3000,
                    datetime.fromisoformat(record["recorded_at"]),
                    "en",
                )
            self.assertTrue(places)
            self.assertTrue(all(p["country"] == "FR" for p in places))
