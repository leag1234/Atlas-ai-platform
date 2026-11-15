"""Device first: a position-dependent question asks the device before the person.

Owner report and decision 2026-11-15 (staging, phone): a second question that
needed the person's position ("is the castle open this afternoon, and how long
from home?") got a text question "where are you?" although the browser could
give the position, and "ask my phone for my position" was answered "I cannot
access your phone's geolocation". The rule: the server decides when the
position is needed (any category, any wording); the reply to the Atlas
interface asks the device (machine-readable LOCATION_REQUEST, phase
location_required with device true); a text question comes only when the device
cannot answer, and says why. Phrasings are realistic, in French and English,
with typos; places are fictional or public landmarks.
"""

import os
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from packages import privacy
from services.orchestrator import grounding
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.location import (
    LEDGER_LABEL,
    Location,
    Position,
    device_position_request,
    position_dependent,
)
from services.orchestrator.location_route import (
    LOCATION_REQUEST,
    coarse_reply,
    device_request,
    location_turn,
    no_position_reply,
    origin_question,
)
from services.orchestrator.nearby import NearbyError, postal_address
from services.orchestrator.request_router import DECISION_KEY, Decision
from services.orchestrator.stream_client import sink_context

# One owner category per row, French and English, with typos and no punctuation.
CATEGORIES: dict[str, tuple[str, str]] = {
    "itinerary": (
        "itinéraire pour aller au chateau de Valombre en voiture stp",
        "directions to the old castle by car pls",
    ),
    "travel time": (
        "Le château de Valombre est-il ouvert cet aprèm ? Et combien de temps pour y aller depuis chez moi",
        "is the castle open this afternoon and how long to get ther from home",
    ),
    "from here": (
        "c koi le prochain bus d'ici pour la gare",
        "how do i get to the airport from here fastest",
    ),
    "local weather": (
        "il fait quel temps ici la",
        "will it rain here tonite",
    ),
    "opening hours near me": (
        "une pharmacie ouverte pres de moi maintenant",
        "any bakery open near me rn",
    ),
    "distance": (
        "a quelle distance je suis de la plage la plus proche",
        "how far is the nearest train station from here",
    ),
    "local events": (
        "des concerts ce soir dans le coin ?",
        "whats on tonight near me",
    ),
}

# Messages asking Atlas to use the device position.
DEVICE_REQUESTS = (
    "Ask my phone for my position !!!",
    "demande ma position au tel",
    "Demande ma position à mon tél !!!",
    "use my location",
    "use my locaiton pls",
    "prends ma position GPS",
    "utilise ma géoloc stp",
    "localise-moi",
    "t'as qu'à prendre ma positon sur mon iphone",
)
NOT_DEVICE_REQUESTS = (
    "je dois prendre position sur ce débat",
    "quelle est ma position fiscale",
    "how does GPS work",
    "explique comment marche la géolocalisation",
    "comment partager ma position sur whatsapp",
    "how do I share my location on WhatsApp",
)
NOT_POSITION_DEPENDENT = (
    "train Paris Lyon demain matin",
    "horaires du musée du Louvre",
    "quel temps fera-t-il à Nantes demain",
    "combien de temps pour cuire des pâtes",
    "how long to boil an egg",
    "explique la photosynthèse",
)
CASTLE = CATEGORIES["travel time"][0]


def request(
    text: str,
    history: list[dict[str, str]] | None = None,
    lang: str = "fr",
    location: Location | None = None,
    ui: bool = True,
    **decision: Any,
) -> ChatRequest:
    value = ChatRequest.model_validate(
        {
            "messages": [*(history or []), {"role": "user", "content": text}],
            "ui_locale": lang,
            "lang": lang,
            "atlas_ui": ui,
        }
    )
    value.location = location
    if decision:
        value.__dict__[DECISION_KEY] = Decision.model_validate(decision)
    return value


def device(accuracy: int = 25) -> Location:
    return Location(
        consent="once",
        position=Position(latitude=49.176, longitude=-0.347),
        accuracy=accuracy,
        timezone="Europe/Paris",
    )


class Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, {"ATLAS_MODEL_ROUTER": "1"})
        environment.start()
        self.addCleanup(environment.stop)
        nearby = patch(
            "services.orchestrator.nearby_chat.process_nearby",
            AsyncMock(return_value=1),
        )
        self.nearby = nearby.start()
        self.addCleanup(nearby.stop)
        reverse = patch(
            "services.orchestrator.nearby.OSM.reverse",
            AsyncMock(return_value="Rue des Tilleuls, 14000 Caen, France"),
        )
        self.reverse = reverse.start()
        self.addCleanup(reverse.stop)
        self.sink = Sink()
        token = sink_context.set(self.sink)
        self.addCleanup(sink_context.reset, token)

    def required(self) -> dict[str, Any]:
        events = [e for e in self.sink.events if e.get("phase") == "location_required"]
        self.assertEqual(len(events), 1, self.sink.events)
        return events[0]


class DeviceFirstTests(Base):
    async def test_every_category_asks_the_device_first(self) -> None:
        for category, phrasings in CATEGORIES.items():
            for lang, text in zip(("fr", "en"), phrasings, strict=True):
                with self.subTest(category=category, lang=lang):
                    self.sink.events.clear()
                    item = Interaction()
                    req = request(text, lang=lang, kind="research", needs_location=True)
                    self.assertTrue(await location_turn(req, item))
                    self.nearby.assert_not_awaited()
                    self.assertEqual(item.reponse, device_request(lang))
                    self.assertIn(f"]({LOCATION_REQUEST})", item.reponse)
                    self.assertNotIn(origin_question(lang), item.reponse)
                    event = self.required()
                    self.assertEqual(
                        event["location"],
                        {"device": True, "reason": "location_required"},
                    )

    async def test_without_a_routing_decision_every_category_is_detected(self) -> None:
        with patch.dict(os.environ, {"ATLAS_MODEL_ROUTER": ""}):
            for category, phrasings in CATEGORIES.items():
                for lang, text in zip(("fr", "en"), phrasings, strict=True):
                    with self.subTest(category=category, lang=lang):
                        self.assertTrue(position_dependent(text))
                        item = Interaction()
                        self.assertTrue(
                            await location_turn(request(text, lang=lang), item)
                        )
                        self.assertIn(LOCATION_REQUEST, item.reponse)
            for text in NOT_POSITION_DEPENDENT:
                with self.subTest(text=text):
                    self.assertFalse(position_dependent(text))
                    self.assertFalse(await location_turn(request(text), Interaction()))

    async def test_api_clients_get_the_text_question_and_the_signal(self) -> None:
        for lang in ("fr", "en"):
            with self.subTest(lang=lang):
                self.sink.events.clear()
                item = Interaction()
                req = request(
                    CATEGORIES["itinerary"][lang == "en"],
                    lang=lang,
                    ui=False,
                    kind="research",
                    needs_location=True,
                )
                self.assertTrue(await location_turn(req, item))
                self.assertEqual(item.reponse, origin_question(lang))
                self.assertNotIn(LOCATION_REQUEST, item.reponse)
                self.assertFalse(self.required()["location"]["device"])

    async def test_device_position_answers_a_route_with_a_place_not_coordinates(
        self,
    ) -> None:
        for lang, text in (("fr", CASTLE), ("en", CATEGORIES["travel time"][1])):
            with self.subTest(lang=lang):
                self.reverse.reset_mock()
                item = Interaction()
                req = request(
                    text,
                    lang=lang,
                    location=device(),
                    kind="research",
                    needs_location=True,
                )
                self.assertFalse(await location_turn(req, item))
                self.reverse.assert_awaited_once()
                asked = req.user_text
                self.assertTrue(asked.startswith(text))
                self.assertIn("Rue des Tilleuls, 14000 Caen, France", asked)
                self.assertIn("within about 25 m", asked)
                self.assertNotRegex(asked, r"49\.17|0\.347")
                self.assertIn("never say that you cannot access", asked)
                self.assertEqual(item.location_used, LEDGER_LABEL)
                self.assertIsNone(req.location)
                self.assertEqual(item.reponse, "")

    async def test_a_known_position_still_searches_nearby_shops(self) -> None:
        for text in CATEGORIES["opening hours near me"]:
            with self.subTest(text=text):
                self.nearby.reset_mock()
                req = request(
                    text, location=device(), kind="research", needs_location=True
                )
                self.assertTrue(await location_turn(req, Interaction()))
                self.nearby.assert_awaited_once()
                self.reverse.assert_not_awaited()

    async def test_place_typed_in_the_dialog_is_the_starting_point(self) -> None:
        typed = Location(consent="refused", place="gare de Caen")
        req = request(CASTLE, location=typed, kind="research", needs_location=True)
        self.assertFalse(await location_turn(req, Interaction()))
        self.assertIn("Starting point given by the user", req.user_text)
        self.assertIn("gare de Caen", req.user_text)
        self.reverse.assert_not_awaited()

    async def test_imprecise_fix_is_named_for_a_route_too(self) -> None:
        item = Interaction()
        req = request(
            CASTLE, location=device(18000), kind="research", needs_location=True
        )
        self.assertTrue(await location_turn(req, item))
        self.assertEqual(item.reponse, coarse_reply("fr", 18000))
        self.reverse.assert_not_awaited()

    async def test_map_service_failure_is_named(self) -> None:
        self.reverse.side_effect = NearbyError("osm_unavailable")
        item = Interaction()
        req = request(CASTLE, location=device(), kind="research", needs_location=True)
        self.assertTrue(await location_turn(req, item))
        self.assertEqual(item.reponse, no_position_reply("unresolved", "fr"))
        self.assertIn("Votre position a bien été reçue", item.reponse)


class DeviceRequestTests(Base):
    """'Ask my phone for my position' answers the pending question."""

    def history(self, lang: str) -> list[dict[str, str]]:
        question = CASTLE if lang == "fr" else CATEGORIES["travel time"][1]
        return [
            {"role": "user", "content": question},
            {"role": "assistant", "content": origin_question(lang)},
        ]

    def test_device_requests_are_recognised(self) -> None:
        for text in DEVICE_REQUESTS:
            with self.subTest(text=text):
                self.assertTrue(device_position_request(text))
        for text in NOT_DEVICE_REQUESTS:
            with self.subTest(text=text):
                self.assertFalse(device_position_request(text))

    async def test_device_request_starts_the_consent_flow(self) -> None:
        for text in DEVICE_REQUESTS:
            for lang in ("fr", "en"):
                with self.subTest(text=text, lang=lang):
                    item = Interaction()
                    # Whatever the router read in it, the device flow starts.
                    req = request(text, self.history(lang), lang=lang, kind="other")
                    self.assertTrue(await location_turn(req, item))
                    self.assertEqual(item.reponse, device_request(lang))
                    self.assertNotRegex(
                        item.reponse.lower(), r"cannot|ne peux pas|impossible"
                    )

    async def test_with_the_position_the_pending_question_is_answered(self) -> None:
        for lang in ("fr", "en"):
            with self.subTest(lang=lang):
                history = self.history(lang)
                req = request(
                    "Ask my phone for my position !!!",
                    history,
                    lang=lang,
                    location=device(),
                    kind="research",
                    needs_location=True,
                )
                self.assertFalse(await location_turn(req, Interaction()))
                self.assertTrue(req.user_text.startswith(history[0]["content"]))
                self.assertIn("Rue des Tilleuls", req.user_text)

    async def test_pending_shop_question_runs_the_nearby_search(self) -> None:
        history = [
            {"role": "user", "content": "boulangerie ouverte a moins de 10 min"},
            {"role": "assistant", "content": origin_question("fr")},
        ]
        req = request(
            "demande ma position au tel", history, location=device(), kind="other"
        )
        self.assertTrue(await location_turn(req, Interaction()))
        self.assertEqual(
            self.nearby.call_args.kwargs["category_text"],
            "boulangerie ouverte a moins de 10 min",
        )


class NoPositionTests(Base):
    """Refused or failed: say exactly why, then one question; never a denial."""

    CASES = (
        (Location(consent="refused"), "refused", "Vous n’avez pas partagé"),
        (
            Location(consent="denied", failure="permission"),
            "permission",
            "refuse l’accès à la position",
        ),
        (
            Location(consent="denied", failure="unsupported"),
            "unsupported",
            "Ce navigateur ne permet pas",
        ),
        (
            Location(consent="denied", failure="unavailable"),
            "unavailable",
            "n’a pas pu déterminer",
        ),
        (
            Location(consent="denied", failure="timeout"),
            "timeout",
            "pas donné sa position à temps",
        ),
        (Location(consent="denied"), "denied", "n’a pas fourni de position"),
    )

    async def test_each_reason_is_named_and_never_asks_the_device_again(
        self,
    ) -> None:
        for location, reason, words in self.CASES:
            for text in (CASTLE, "boulangerie ouverte pres de moi"):
                with self.subTest(reason=reason, text=text):
                    self.sink.events.clear()
                    item = Interaction()
                    req = request(
                        text,
                        location=location.model_copy(),
                        kind="research",
                        needs_location=True,
                    )
                    self.assertTrue(await location_turn(req, item))
                    self.nearby.assert_not_awaited()
                    self.assertIn(words, item.reponse)
                    self.assertTrue(item.reponse.endswith(origin_question("fr")))
                    self.assertNotIn(LOCATION_REQUEST, item.reponse)
                    self.assertFalse(self.required()["location"]["device"])
                    self.assertIn("location_" + reason, item.erreurs)

    def test_every_reason_exists_in_english(self) -> None:
        for _, reason, _ in self.CASES:
            reply = no_position_reply(reason, "en")
            self.assertTrue(reply.endswith(origin_question("en")))
            self.assertNotRegex(reply, r"cannot access|no way to read")

    def test_a_failure_reason_belongs_to_a_denied_position(self) -> None:
        with self.assertRaises(ValueError):
            Location(consent="once", failure="permission")
        with self.assertRaises(ValueError):
            Location(consent="refused", failure="timeout")


class PrivacyLevelTests(Base):
    async def test_enhanced_and_strict_never_ask_or_send_the_device_position(
        self,
    ) -> None:
        for level in (privacy.ENHANCED, privacy.STRICT):
            with self.subTest(level=level):
                token = privacy.elevate(level)
                try:
                    item = Interaction()
                    req = request(CASTLE, kind="research", needs_location=True)
                    self.assertTrue(await location_turn(req, item))
                    self.assertEqual(item.reponse, origin_question("fr"))
                    item = Interaction()
                    req = request(
                        CASTLE,
                        location=device(),
                        kind="research",
                        needs_location=True,
                    )
                    self.assertTrue(await location_turn(req, item))
                    self.assertEqual(item.reponse, no_position_reply("privacy", "fr"))
                    self.reverse.assert_not_awaited()
                    # A place the person names in text is still used.
                    req = request(
                        "je pars de la gare de Caen, combien de temps pour aller au chateau",
                        kind="research",
                        needs_location=True,
                        origin="gare, Caen",
                    )
                    self.assertFalse(await location_turn(req, Interaction()))
                finally:
                    privacy.reset(token)


class RouterPromptTests(unittest.TestCase):
    def test_router_knows_the_device_can_be_asked(self) -> None:
        from services.orchestrator import request_router

        system = request_router.messages("x", [])[0]["content"]
        self.assertIn("ask the device for it", system)
        self.assertIn('"demande ma position a mon tel"', system)


class AddressTests(unittest.TestCase):
    def test_house_number_without_its_street_is_omitted(self) -> None:
        self.assertEqual(
            postal_address(
                {
                    "addr:housenumber": "186",
                    "addr:postcode": "14000",
                    "addr:city": "Caen",
                }
            ),
            "14000, Caen",
        )
        self.assertEqual(
            postal_address(
                {
                    "addr:housenumber": "12",
                    "addr:street": "Rue des  Tilleuls",
                    "addr:postcode": "14000",
                    "addr:city": "Caen",
                }
            ),
            "12 Rue des Tilleuls, 14000, Caen",
        )
        # A square or hamlet address (addr:place) is completed, not cut.
        self.assertEqual(
            postal_address({"addr:housenumber": "3", "addr:place": "Place du Marché"}),
            "3 Place du Marché",
        )
        self.assertEqual(postal_address({}), "")

    def test_nominatim_pedestrian_street_completes_the_address(self) -> None:
        import tempfile
        from datetime import datetime, timezone

        from services.orchestrator.nearby import OSM

        row = dict(
            osm_type="node",
            osm_id=7,
            lat="49.1765",
            lon="-0.347",
            category="shop",
            type="bakery",
            name="Fournil des Tilleuls",
            extratags={},
            address={
                "house_number": "186",
                "pedestrian": "Rue Piétonne",
                "postcode": "14000",
                "city": "Caen",
            },
        )
        with tempfile.TemporaryDirectory(dir="BRAIN") as root:
            with patch.dict(os.environ, {"ATLAS_OSM_STATE": root}):
                osm = OSM()

                async def run() -> list[dict[str, Any]]:
                    with patch.object(osm, "request", AsyncMock(return_value=[row])):
                        found = await osm.nominatim_candidates(
                            Position(latitude=49.176, longitude=-0.347),
                            ["bakery"],
                            1500,
                            "fr",
                        )
                    osm.served_by = "nominatim"
                    return osm.parse_candidates(
                        found,
                        Position(latitude=49.176, longitude=-0.347),
                        ["bakery"],
                        1500,
                        datetime(2026, 11, 14, 10, tzinfo=timezone.utc),
                    )

                import asyncio

                places = asyncio.run(run())
        self.assertEqual(places[0]["address"], "186 Rue Piétonne, 14000, Caen")


class ReverseTests(unittest.IsolatedAsyncioTestCase):
    async def test_reverse_gives_street_and_locality_only(self) -> None:
        from services.orchestrator.nearby import OSM

        osm = OSM()
        answer = {
            "address": {
                "house_number": "4",
                "road": "Rue des Tilleuls",
                "suburb": "Quartier Nord",
                "city": "Caen",
                "postcode": "14000",
                "country": "France",
            }
        }
        with patch.object(osm, "request", AsyncMock(return_value=answer)) as call:
            place = await osm.reverse(Position(latitude=49.176, longitude=-0.347))
        self.assertEqual(place, "Rue des Tilleuls, Quartier Nord, 14000 Caen, France")
        self.assertNotIn("4", place.split(",")[0])
        assert call.await_args is not None
        service, parameters, endpoint = call.await_args.args
        self.assertEqual(service, "geocode")
        self.assertTrue(endpoint.endswith("/reverse"))
        self.assertEqual(parameters["lat"], "49.176")
        with patch.object(osm, "request", AsyncMock(return_value={"error": "x"})):
            with self.assertRaises(NearbyError):
                await osm.reverse(Position(latitude=49.176, longitude=-0.347))


class ContradictionTests(unittest.TestCase):
    """A contradiction is about the same entity and the same claim."""

    def test_another_place_or_a_headline_is_not_quoted_under_a_place(self) -> None:
        check = grounding.Grounding()
        check.add(
            "Caen : la ville ouvre 3 nouvelles places de parking cet hiver, annonce la mairie."
        )
        # Most words of the claim, but another bakery: before the rule this
        # sentence was quoted as contradicting the first one.
        check.add("Le Fournil Central, ouvert jusqu'à 21 h, recrute un apprenti.")
        answer = "- **Fournil des Tilleuls** — ouverte jusqu'à 19 h."
        self.assertEqual(
            check.check(answer, "boulangerie ouverte à moins de 10 min", "fr"), answer
        )

    def test_same_entity_with_another_value_is_still_noted(self) -> None:
        check = grounding.Grounding()
        check.add("Le Fournil des Tilleuls ferme à 18 h le samedi.")
        answer = "Le Fournil des Tilleuls ferme à 20 h le samedi."
        checked = check.check(
            answer, "à quelle heure ferme le Fournil des Tilleuls", "fr"
        )
        self.assertIn(grounding.wording("contradicted", "fr"), checked)


if __name__ == "__main__":
    unittest.main()
