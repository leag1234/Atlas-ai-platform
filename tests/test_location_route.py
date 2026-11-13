"""Position-dependent turns: stated place, consented browser fix, or one question.

Owner report 2026-11-13 (staging): a public transport question "to get there
now" was answered from a city the person was not in, and a desktop browser
position matched the access provider's network. Phrasings below are realistic
and varied (two languages, typos, no question mark); the routing decision is
given, as the router produces it, and the tests check what each path does.
"""

import os
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from services.orchestrator import request_router
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.location import Location, Position
from services.orchestrator.location_route import (
    coarse_reply,
    location_turn,
    origin_question,
)
from services.orchestrator.nearby import rank
from services.orchestrator.request_router import DECISION_KEY, Decision
import test_request_router as router_tests

# Requests whose answer depends on where the person is, and no place stated.
POSITION_DEPENDENT = (
    "Je voudrais me rendre à Lyon maintenant, donne-moi les prochains horaires de transports en commun pour y aller",
    "c koi le prochain bus pour aller a la gare",
    "combien de temps a pied jusqu'au musée des beaux arts",
    "il fait quel temps ici la",
    "how do i get to the airport from here, fastest way",
    "next tram to the city centre pls",
    "y a un distributeur pas loin",
    "what's open around me right now",
)
# The same kind of requests with the starting point stated by the person.
STATED = (
    (
        "je suis place de la Gare à Caen, prochain train pour paris ?",
        "place de la Gare, Caen",
    ),
    ("From Rouen station, how long to drive to Le Havre right now?", "Rouen station"),
)


def request(
    text: str,
    history: list[dict[str, str]] | None = None,
    lang: str = "fr",
    location: Location | None = None,
) -> ChatRequest:
    value = ChatRequest.model_validate(
        {
            "messages": [*(history or []), {"role": "user", "content": text}],
            "ui_locale": lang,
            "lang": lang,
        }
    )
    value.location = location
    return value


def routed(value: ChatRequest, **decision: Any) -> ChatRequest:
    value.__dict__[DECISION_KEY] = Decision.model_validate(decision)
    return value


def browser(accuracy: int | None) -> Location:
    return Location(
        consent="once",
        position=Position(latitude=49.176, longitude=-0.347),
        accuracy=accuracy,
        timezone="Europe/Paris",
    )


class DecisionTests(unittest.TestCase):
    def test_location_fields_parse_and_default(self) -> None:
        self.assertEqual(
            request_router.parse('{"kind": "research"}'), Decision(kind="research")
        )
        decision = request_router.parse(
            '{"kind": "research", "needs_location": true, "origin": "  place de la Gare,\\n Caen "}'
        )
        self.assertTrue(decision.needs_location)
        self.assertEqual(decision.origin, "place de la Gare, Caen")
        for empty in ('""', '"null"', '"unknown"', '"ici"', "null", "3", '"x"'):
            with self.subTest(origin=empty):
                parsed = request_router.parse(
                    '{"kind": "research", "needs_location": true, "origin": %s}' % empty
                )
                self.assertIsNone(parsed.origin)
        long = request_router.parse(
            '{"kind": "research", "needs_location": true, "origin": "%s"}' % ("a" * 400)
        )
        self.assertEqual(len(long.origin or ""), 200)

    def test_prompt_asks_for_the_location_fields(self) -> None:
        system = request_router.messages("x", [])[0]["content"]
        self.assertIn('"needs_location": false, "origin": null}', system)
        self.assertIn("Never guess a place", system)
        # Each example states every key, so the model never drops them.
        examples = system.split("Examples (last message, then the reply):")[1]
        self.assertEqual(examples.count("{"), examples.count('"needs_location"'))
        self.assertGreaterEqual(request_router.payload("x", [])["max_tokens"], 160)


class LocationTurnTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_no_known_position_asks_one_short_question(self) -> None:
        for text in POSITION_DEPENDENT:
            for lang in ("fr", "en"):
                with self.subTest(text=text, lang=lang):
                    item = Interaction()
                    req = routed(
                        request(text, lang=lang), kind="research", needs_location=True
                    )
                    self.assertTrue(await location_turn(req, item))
                    self.nearby.assert_not_awaited()
                    self.assertEqual(item.reponse, origin_question(lang))
                    self.assertEqual(item.reponse.count("?"), 1)
                    self.assertEqual(item.task_type, "nearby")
                    self.assertEqual(item.state, "done")
                    self.assertIn("location_required", item.erreurs)

    async def test_question_never_names_a_city(self) -> None:
        for lang in ("fr", "en", "de", "es", "it"):
            self.assertNotRegex(origin_question(lang), r"Paris|Lyon|Clermont|Caen")

    async def test_stated_starting_point_goes_to_research_unchanged(self) -> None:
        for text, origin in STATED:
            with self.subTest(text=text):
                item = Interaction()
                req = routed(
                    request(text), kind="research", needs_location=True, origin=origin
                )
                self.assertFalse(await location_turn(req, item))
                self.nearby.assert_not_awaited()
                self.assertEqual(item.reponse, "")
                self.assertIsNone(req.location)

    async def test_starting_point_given_after_the_question(self) -> None:
        history = [
            {"role": "user", "content": "prochain bus pour l'aéroport stp"},
            {"role": "assistant", "content": origin_question("fr")},
        ]
        req = routed(
            request("je suis rue de Bayeux a Caen", history),
            kind="research",
            needs_location=True,
            origin="rue de Bayeux a Caen",
        )
        self.assertFalse(await location_turn(req, Interaction()))

    async def test_stated_place_wins_over_the_browser_fix(self) -> None:
        req = routed(
            request(
                "boulangeries ouvertes, je suis place de la Gare a Caen",
                location=browser(30),
            ),
            kind="research",
            needs_location=True,
            origin="place de la Gare a Caen",
        )
        self.assertTrue(await location_turn(req, Interaction()))
        self.nearby.assert_awaited_once()
        location = self.nearby.call_args.args[0].location
        self.assertEqual(location.place, "place de la Gare a Caen")
        self.assertIsNone(location.position)

    async def test_place_given_after_a_shop_question_searches_that_shop(self) -> None:
        history = [
            {"role": "user", "content": "Boulangeries proches"},
            {
                "role": "assistant",
                "content": "Indiquez un lieu, par exemple « près de la place Bellecour ».",
            },
        ]
        for text in (
            "je suis au 3 place de la Gare à Caen, regarde lesquelles sont ouvertes",
            "place de la gare caen",
        ):
            with self.subTest(text=text):
                self.nearby.reset_mock()
                req = routed(
                    request(text, history),
                    kind="research",
                    needs_location=True,
                    origin="place de la Gare, Caen",
                )
                self.assertTrue(await location_turn(req, Interaction()))
                category_text = self.nearby.call_args.kwargs["category_text"]
                self.assertIn("Boulangeries proches", category_text)
                self.assertIn(text, category_text)

    async def test_consented_browser_fix_runs_the_nearby_search(self) -> None:
        for text in (
            "Boulangeries proches",
            "pharmacie ouverte pres de moi",
            "any cafe open nearby?",
        ):
            with self.subTest(text=text):
                self.nearby.reset_mock()
                req = routed(
                    request(text, location=browser(25)),
                    kind="research",
                    needs_location=True,
                )
                self.assertTrue(await location_turn(req, Interaction()))
                self.nearby.assert_awaited_once()
                self.assertEqual(self.nearby.call_args.kwargs["category_text"], text)

    async def test_named_place_without_a_position_need_is_not_intercepted(self) -> None:
        for text in (
            "horaires d'ouverture du musée du Louvre",
            "quel temps fera-t-il à Nantes demain",
            "train Paris Lyon demain matin",
        ):
            with self.subTest(text=text):
                req = routed(request(text), kind="research")
                self.assertFalse(await location_turn(req, Interaction()))
                self.nearby.assert_not_awaited()

    async def test_without_a_decision_the_lexical_gate_is_unchanged(self) -> None:
        with patch.dict(os.environ, {"ATLAS_MODEL_ROUTER": ""}):
            req = request("boulangerie ouverte pres moi")
            self.assertTrue(await location_turn(req, Interaction()))
            self.nearby.assert_awaited_once()
            self.nearby.reset_mock()
            self.assertFalse(
                await location_turn(request("prochain bus pour la gare"), Interaction())
            )
            self.nearby.assert_not_awaited()


class CoarsePositionTests(unittest.IsolatedAsyncioTestCase):
    def test_accuracy_threshold(self) -> None:
        self.assertFalse(browser(None).coarse)
        self.assertFalse(browser(40).coarse)
        self.assertFalse(browser(1000).coarse)
        self.assertTrue(browser(1001).coarse)
        self.assertTrue(browser(25000).coarse)
        self.assertNotIn("accuracy", browser(25000).model_dump())
        typed = Location(consent="refused", place="place de la Gare, Caen")
        self.assertFalse(typed.coarse)

    async def test_coarse_fix_is_never_used_silently(self) -> None:
        from services.orchestrator.nearby_chat import process_nearby

        for lang in ("fr", "en"):
            with self.subTest(lang=lang):
                item = Interaction()
                req = request(
                    "Boulangeries proches", lang=lang, location=browser(18000)
                )
                with patch("services.orchestrator.nearby_chat.NearbyTools") as tools:
                    calls = await process_nearby(req, item)
                tools.assert_not_called()
                self.assertEqual(calls, 0)
                self.assertEqual(item.reponse, coarse_reply(lang, 18000))
                self.assertIn("18 km", item.reponse)
                self.assertIn("location_coarse", item.erreurs)
                self.assertEqual(item.location_used, "")
                self.assertIsNone(req.location)

    async def test_nearby_tool_refuses_a_coarse_fix(self) -> None:
        from services.orchestrator.loop import Call
        from services.orchestrator.nearby_chat import NearbyTools

        osm = AsyncMock()
        tools = NearbyTools(browser(9000), AsyncMock(), osm=osm)
        result = await tools.execute(
            Call("1", "nearby_places", '{"category": "boulangerie"}'), 5
        )
        self.assertEqual(result, {"error": "location_coarse"})
        osm.candidates.assert_not_called()


class CoverageTests(unittest.TestCase):
    def place(self, name: str, status: str, meters: int) -> dict[str, Any]:
        return dict(
            name=name,
            category="bakery",
            source=name,
            distance_m=meters,
            opening=dict(
                status=status,
                next_opening="2026-11-14T07:00" if status == "closed" else None,
            ),
        )

    def test_closer_places_without_hours_are_kept(self) -> None:
        candidates = [
            self.place("near-unknown", "unknown", 300),
            *(self.place(f"open-{i}", "open", 400 + 100 * i) for i in range(6)),
            self.place("far-unknown", "unknown", 1400),
        ]
        names = [p["name"] for p in rank(candidates)]
        self.assertIn("near-unknown", names)
        self.assertNotIn("far-unknown", names)
        self.assertEqual(names[:5], [f"open-{i}" for i in range(5)])

    def test_craft_bakery_counts_as_a_bakery(self) -> None:
        from services.orchestrator.nearby import OSM

        origin = Position(latitude=49.176, longitude=-0.347)
        elements = [
            dict(
                type="node",
                id=1,
                lat=49.1765,
                lon=-0.347,
                tags={"craft": "bakery", "name": "Fournil A"},
            ),
            dict(
                type="node",
                id=2,
                lat=49.1766,
                lon=-0.347,
                tags={"shop": "bakery", "name": "Fournil B"},
            ),
            dict(
                type="node",
                id=3,
                lat=49.1767,
                lon=-0.347,
                tags={"shop": "pastry", "name": "Gâteaux C"},
            ),
        ]
        osm = OSM()
        osm.served_by = "overpass"
        result = osm.parse_candidates(
            {"elements": elements},
            origin,
            ["bakery"],
            1500,
            datetime(2026, 11, 13, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(sorted(p["name"] for p in result), ["Fournil A", "Fournil B"])


class NominatimSplitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import tempfile

        self.root = tempfile.TemporaryDirectory(dir="BRAIN")
        self.addCleanup(self.root.cleanup)
        environment = patch.dict(os.environ, {"ATLAS_OSM_STATE": self.root.name})
        environment.start()
        self.addCleanup(environment.stop)

    async def test_a_full_answer_is_split_until_complete(self) -> None:
        from services.orchestrator import nearby
        from services.orchestrator.nearby import OSM

        def rows(start: int, count: int) -> list[dict[str, Any]]:
            return [
                dict(
                    osm_type="node",
                    osm_id=start + i,
                    lat="49.1765",
                    lon="-0.347",
                    category="shop",
                    type="bakery",
                    name=f"Fournil {start + i}",
                    extratags={},
                    address={"city": "Caen"},
                )
                for i in range(count)
            ]

        # The whole box is full; its four quadrants each return fewer rows,
        # one of them repeating a place already seen.
        answers = [
            rows(0, nearby.NOMINATIM_LIMIT),
            rows(100, 5),
            rows(200, 3),
            rows(0, 2),
            rows(300, 1),
        ]
        osm = OSM()
        with patch.object(osm, "request", AsyncMock(side_effect=answers)) as call:
            found = await osm.nominatim_candidates(
                Position(latitude=49.176, longitude=-0.347), ["bakery"], 1500, "fr"
            )
        self.assertEqual(call.await_count, 5)
        boxes = [c.args[1]["viewbox"] for c in call.await_args_list]
        self.assertEqual(len(set(boxes)), 5)
        self.assertTrue(all(c.args[1]["limit"] == "40" for c in call.await_args_list))
        identities = [e["id"] for e in found["elements"]]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual(len(identities), nearby.NOMINATIM_LIMIT + 5 + 3 + 1)
        self.assertNotIn("nominatim:coverage_limited", osm.failures)

    async def test_split_stops_at_the_request_budget(self) -> None:
        from services.orchestrator import nearby
        from services.orchestrator.nearby import OSM

        full = [
            dict(
                osm_type="node",
                osm_id=i,
                lat="49.1",
                lon="-0.3",
                category="shop",
                type="bakery",
                name=f"F{i}",
            )
            for i in range(nearby.NOMINATIM_LIMIT)
        ]
        osm = OSM()
        with patch.object(osm, "request", AsyncMock(return_value=full)) as call:
            await osm.nominatim_candidates(
                Position(latitude=49.176, longitude=-0.347), ["bakery"], 1500, "fr"
            )
        self.assertEqual(call.await_count, nearby.NOMINATIM_REQUESTS)
        self.assertIn("nominatim:coverage_limited", osm.failures)


class PipelineHookTests(unittest.IsolatedAsyncioTestCase):
    """The plain chat reaches the location rule before any research runs."""

    setUp = router_tests.PipelineGateTests.setUp

    def model(self) -> Mock:
        # The same model double as the router's pipeline tests.
        return router_tests.PipelineGateTests.model(self)  # type: ignore[arg-type]

    async def process(self, req: ChatRequest) -> tuple[AsyncMock, Interaction]:
        from services.orchestrator.chat_pipeline import _process_request
        from services.orchestrator.loop import Result

        req.__dict__["daily_user"] = "user@example.test"
        item = Interaction()
        run = AsyncMock(return_value=Result("Here it is.", "done"))
        with (
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(return_value=self.model()),
            ),
            patch("services.orchestrator.chat_pipeline.run", new=run),
        ):
            await _process_request(req, item)
        return run, item

    async def test_transport_question_without_a_place_asks_where(self) -> None:
        req = routed(
            request(POSITION_DEPENDENT[0]), kind="research", needs_location=True
        )
        run, item = await self.process(req)
        run.assert_not_awaited()
        self.assertEqual(item.reponse, origin_question("fr"))

    async def test_transport_question_with_a_place_is_researched(self) -> None:
        text, origin = STATED[0]
        req = routed(request(text), kind="research", needs_location=True, origin=origin)
        run, item = await self.process(req)
        run.assert_awaited_once()
        self.assertNotEqual(item.task_type, "nearby")


if __name__ == "__main__":
    unittest.main()
