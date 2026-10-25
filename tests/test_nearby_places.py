"""REQ-NEARBY-01: semantic schedules, consent, ranking, privacy and regressions."""

from datetime import datetime, timezone
import unittest

from pydantic import ValidationError

from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.location import Location, Position, location_dependent
from services.orchestrator.nearby import opening_status, rank
from services.orchestrator.source_truth import (
    clean_citation_residue,
    without_unretrieved,
)


def rendered(text: str, sources: set[str]) -> str:
    return clean_citation_residue(without_unretrieved(text, sources))


class NearbyTests(unittest.TestCase):
    def status(self, expression: str | None, when: str) -> dict[str, object]:
        return opening_status(expression, (48.85, 2.35), datetime.fromisoformat(when))

    def test_hours(self) -> None:
        self.assertEqual(
            self.status("Mo-Fr 09:00-18:00", "2026-10-26T12:00:00+00:00")["status"],
            "open",
        )
        soon = self.status("Mo-Fr 09:00-18:00", "2026-10-26T16:45:00+00:00")
        self.assertTrue(soon["closing_soon"])
        self.assertEqual(soon["closing_time"], "2026-10-26T18:00:00+01:00")
        self.assertEqual(
            self.status("Mo-Su 09:00-18:00; PH off", "2026-12-25T12:00:00+00:00")[
                "status"
            ],
            "closed",
        )
        self.assertEqual(
            self.status("Mo 22:00-02:00", "2026-10-27T00:30:00+00:00")["status"], "open"
        )
        self.assertEqual(
            self.status("Mo-Su 09:00-18:00; Mo off", "2026-10-26T12:00:00+00:00")[
                "status"
            ],
            "closed",
        )
        for expression in (None, "nonsense", "unknown", "SH 10:00-18:00"):
            self.assertEqual(
                self.status(expression, "2026-10-26T12:00:00+00:00")["status"],
                "unknown",
            )

    def test_foreign_timezone(self) -> None:
        value = opening_status(
            "Mo-Fr 09:00-18:00",
            (40.71, -74.0),
            datetime(2026, 10, 26, 14, tzinfo=timezone.utc),
        )
        self.assertEqual(value["status"], "open")
        self.assertEqual(value["closing_time"], "2026-10-26T18:00:00-04:00")

    def test_consent_and_serialization(self) -> None:
        for mode in ("once", "always"):
            request = ChatRequest(
                messages=[ChatMessage(role="user", content="Nearby bakery open?")],
                location=Location(
                    consent=mode,
                    position=Position(latitude=48.856612, longitude=2.352222),
                ),
            )
            self.assertNotIn("location", request.model_dump())
            self.assertNotIn("48.856", repr(request))
            self.assertIsNotNone(request.location)
            assert request.location and request.location.position
            self.assertEqual(request.location.position.latitude, 48.857)
        for mode in ("refused", "denied"):
            with self.assertRaises(ValidationError):
                Location.model_validate(
                    dict(consent=mode, position=dict(latitude=48.1, longitude=2.1))
                )
            location = Location.model_validate(
                dict(consent=mode, place="près de la place Bellecour")
            )
            self.assertNotIn("place", location.model_dump())
        for lat in (float("nan"), float("inf"), 100.0):
            with self.assertRaises(ValidationError):
                Position(latitude=lat, longitude=0.0)

    def test_router(self) -> None:
        for question in (
            "Donne moi les boulangeries ouvertes et supérette ouverte les plus proches",
            "Où acheter du pain à cette heure autour de moi ?",
            "Je dois acheter du lait, où aller à pied maintenant ?",
            "Il me faut des médicaments tout de suite dans le quartier.",
            "Nearby bakery open?",
            "FARMACIA CERCA ABIERTA",
            "boulangerie ouverte pres moi",
        ):
            self.assertTrue(location_dependent(question), question)
        for question in (
            "Give me a bread recipe",
            "Explique comment fabriquer du pain",
            "Hello",
            "Translate nearest pharmacy into French",
        ):
            self.assertFalse(location_dependent(question), question)

    def test_ranking(self) -> None:
        def place(
            name: str, state: str, meters: int, next_open: str | None = None
        ) -> dict[str, object]:
            return dict(
                name=name,
                category="bakery",
                source=name,
                distance_m=meters,
                opening=dict(status=state, next_opening=next_open),
            )

        result = rank(
            [
                place("unknown", "unknown", 20),
                place("far", "open", 500),
                place("near", "open", 200),
                place("later", "closed", 50, "2026-10-27T09:00"),
                place("soon", "closed", 100, "2026-10-26T19:00"),
            ]
        )
        self.assertEqual(
            [p["name"] for p in result], ["near", "far", "soon", "later", "unknown"]
        )

    def test_observed_citation_residue(self) -> None:
        # Owner defect, 2026-10-25: exact observed fragment.
        self.assertEqual(
            rendered("Voici les boulangeries ouvertes, issues de sources .", set()),
            "Voici les boulangeries ouvertes.",
        )
        self.assertEqual(
            rendered(
                "Informations issues de sources [1](https://unretrieved.example/a).",
                set(),
            ),
            "Informations.",
        )
        self.assertEqual(
            rendered("A [1](https://unretrieved.example/a) , B.", set()),
            "A, B.",
        )
        self.assertIn("`x .`", rendered("Code `x .` et texte .", set()))
