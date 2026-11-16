"""Trip answers: shared position reused, travel values sourced, door-to-door plans.

Owner test 2026-11-16 (staging, phone, dictated): a lunch at a Paris station at
12:30 and the morning trains to be on time. Atlas asked the device position,
then said the person's street was "about 10 minutes' walk" from the station (no
routing engine exists), mixed direct trains to another terminus with invented
connection times, searched first with a poor query, and asked the position
again for the correction sent two minutes later.

Every place, street and person below is fictional; phrasings vary in language,
country and spelling as real requests do.
"""

import json
import os
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

from services.orchestrator import journey, travel_claims
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.location import (
    LEDGER_LABEL,
    LEDGER_LABEL_REUSED,
    Location,
    Position,
)
from services.orchestrator.location_memory import (
    ConversationPositions,
    conversation_key,
    reuse_seconds,
)
from services.orchestrator.location_route import (
    asked_text,
    device_request,
    location_turn,
)
from services.orchestrator.request_router import DECISION_KEY, Decision

STREET = "12 rue des Tilleuls, Quartier Fictif, 14000 Caen, France"


def request(
    text: str,
    *,
    chat: str = "chat-1",
    user: str = "probe@example.test",
    history: list[dict[str, str]] | None = None,
    location: Location | None = None,
    lang: str = "fr",
) -> ChatRequest:
    value = ChatRequest.model_validate(
        {
            "messages": [*(history or []), {"role": "user", "content": text}],
            "ui_locale": lang,
            "lang": lang,
            "atlas_ui": True,
        }
    )
    value.location = location
    value.__dict__["daily_user"] = user
    if chat:
        value.__dict__["native_chat_id"] = chat
    value.__dict__[DECISION_KEY] = Decision(kind="research", needs_location=True)
    return value


def device(consent: str = "once") -> Location:
    return Location(
        consent=consent,  # type: ignore[arg-type]
        position=Position(latitude=49.18, longitude=-0.37),
        accuracy=20,
        timezone="Europe/Paris",
    )


class LocationReuseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        environment = patch.dict(
            os.environ,
            {"ATLAS_MODEL_ROUTER": "1", "ATLAS_LOCATION_REUSE_MINUTES": "45"},
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.clock = [1000.0]
        store = ConversationPositions(clock=lambda: self.clock[0])
        for target in (
            "services.orchestrator.location_route.positions",
            "services.orchestrator.location_memory.positions",
        ):
            patcher = patch(target, store)
            patcher.start()
            self.addCleanup(patcher.stop)
        reverse = patch(
            "services.orchestrator.nearby.OSM.reverse",
            AsyncMock(return_value=STREET),
        )
        self.reverse = reverse.start()
        self.addCleanup(reverse.stop)
        privacy = patch("packages.privacy.allows", return_value=True)
        privacy.start()
        self.addCleanup(privacy.stop)

    async def first_turn(self, chat: str = "chat-1") -> Interaction:
        item = Interaction()
        req = request(
            "demain midi je dois etre gare saint lazare a 12h30, quels trains le matin",
            chat=chat,
            location=device(),
        )
        self.assertFalse(await location_turn(req, item))
        self.assertEqual(item.location_used, LEDGER_LABEL)
        return item

    async def test_follow_up_in_the_same_conversation_does_not_ask_again(self) -> None:
        await self.first_turn()
        self.clock[0] += 120  # the correction came two minutes later
        for text in (
            "non c'est Saint-Lazare pas Saint-Nazaire",
            "and what about the bus to get to the station?",
            "et si je pars de chez moi a pied ?",
        ):
            with self.subTest(text=text):
                item = Interaction()
                req = request(text)
                self.assertFalse(await location_turn(req, item))
                self.assertNotIn("location_required", item.erreurs)
                self.assertEqual(item.location_used, LEDGER_LABEL_REUSED)
                self.assertIn(STREET, req.user_text)
                # The person's own words stay what discovery searches.
                self.assertEqual(asked_text(req), text)

    async def test_another_conversation_or_person_is_asked(self) -> None:
        await self.first_turn()
        for req in (
            request("prochain train pour Caen ?", chat="chat-2"),
            request("prochain train pour Caen ?", user="other@example.test"),
            request("prochain train pour Caen ?", chat=""),
        ):
            item = Interaction()
            self.assertTrue(await location_turn(req, item))
            self.assertIn("location_required", item.erreurs)
            self.assertEqual(item.reponse, device_request("fr"))

    async def test_expired_entry_is_forgotten(self) -> None:
        await self.first_turn()
        self.clock[0] += 45 * 60 + 1
        item = Interaction()
        self.assertTrue(await location_turn(request("et le suivant ?"), item))
        self.assertIn("location_required", item.erreurs)

    async def test_refusal_forgets_the_shared_position(self) -> None:
        await self.first_turn()
        item = Interaction()
        refused = Location(consent="refused", timezone="Europe/Paris")
        self.assertTrue(
            await location_turn(request("prochain bus ?", location=refused), item)
        )
        item = Interaction()
        self.assertTrue(await location_turn(request("et le train ?"), item))
        self.assertIn("location_required", item.erreurs)

    async def test_typed_place_is_reused_too(self) -> None:
        typed = Location(consent="once", place="place de la Mairie, Caen")
        self.assertFalse(
            await location_turn(
                request("prochain bus ?", location=typed), Interaction()
            )
        )
        req = request("et le dernier ?")
        self.assertFalse(await location_turn(req, Interaction()))
        self.assertIn("place de la Mairie, Caen", req.user_text)

    async def test_coarse_fix_is_not_kept(self) -> None:
        coarse = device()
        coarse.accuracy = 15000
        item = Interaction()
        self.assertTrue(
            await location_turn(request("prochain bus ?", location=coarse), item)
        )
        self.assertIn("location_coarse", item.erreurs)
        item = Interaction()
        self.assertTrue(await location_turn(request("et le train ?"), item))
        self.assertIn("location_required", item.erreurs)

    def test_reuse_window_is_bounded(self) -> None:
        for value, seconds in (
            ("45", 2700.0),
            ("500", 3600.0),
            ("0", 0.0),
            ("x", 3600.0),
        ):
            with patch.dict(os.environ, {"ATLAS_LOCATION_REUSE_MINUTES": value}):
                self.assertEqual(reuse_seconds(), seconds)

    def test_key_is_a_digest_never_the_raw_ids(self) -> None:
        key = conversation_key(request("x"))
        self.assertIsNotNone(key)
        self.assertNotIn("probe", str(key))
        self.assertNotIn("chat-1", str(key))
        self.assertIsNone(conversation_key(request("x", user="unknown")))


def turn_with(*sources: str, asked: str = "") -> travel_claims.Turn:
    turn = travel_claims.Turn(asked=[asked] if asked else [])
    turn.activate()
    for text in sources:
        turn.collect({"title": "", "snippet": text})
    return turn


class TravelClaimsTests(unittest.TestCase):
    def test_units_and_forms_in_several_languages(self) -> None:
        cases = {
            "about 10 minutes' walk": ("duration", 10),
            "à environ 10 min à pied": ("duration", 10),
            "etwa 25 Minuten zu Fuß": ("duration", 25),
            "unos 2 horas en coche": ("duration", 120),
            "circa 1 ora e 20 minuti": ("duration", 80),
            "1 h 20 min de trajet": ("duration", 80),
            "3,5 km": ("distance", 3500),
            "800 m": ("distance", 800),
            "2 miles": ("distance", 3218.688),
            "départ à 10h05": ("clock", 605),
            "Abfahrt 14:00": ("clock", 840),
            "leave at 9 am": ("clock", 540),
            "à 10 heures": ("clock", 600),
        }
        for text, (kind, value) in cases.items():
            with self.subTest(text=text):
                found = travel_claims.quantities(text)
                self.assertEqual(len(found), 1, found)
                self.assertEqual(found[0].kind, kind)
                self.assertAlmostEqual(found[0].value, value, places=2)

    def test_non_travel_numbers_are_not_read(self) -> None:
        for text in (
            "12 M€ de budget",
            "ligne 13, voie 5",
            "120 km/h",
            "see https://example.test/10-min-guide",
            "`sleep 10m`",
            "50 m²",
        ):
            with self.subTest(text=text):
                self.assertEqual(travel_claims.quantities(text), [])

    def test_owner_defect_invented_walk_is_removed(self) -> None:
        answer = (
            f"Votre position, {STREET}, est à environ 10 minutes à pied de la gare. "
            "Le TER 3105 part à 9h12 et arrive à 11h52."
        )
        turn = turn_with(
            "TER 3105 Caen 09:12 → Paris Saint-Lazare 11:52",
            asked="je dois être à la gare à 12h30",
        )
        checked = turn.check(answer, "fr")
        self.assertNotIn("10 minutes", checked)
        self.assertIn("est à environ (durée non vérifiée) à pied de la gare", checked)
        self.assertTrue(checked.endswith(travel_claims.wording("note", "fr")))
        self.assertIn("9h12 et arrive à 11h52.", checked)  # sourced times kept
        self.assertNotIn("non sourcé", checked)

    def test_invented_connection_times_are_marked(self) -> None:
        answer = (
            "| Train | Départ | Arrivée |\n|---|---|---|\n"
            "| TER 3105 | 09:12 | 11:52 |\n| Correspondance | 10h05 | 12h00 |\n\n"
            "Bon voyage."
        )
        turn = turn_with("TER 3105 Caen 09:12 Paris Saint-Lazare 11:52")
        checked = turn.check(answer, "fr")
        self.assertIn("10h05 (non sourcé)", checked)
        self.assertIn("12h00 (non sourcé)", checked)
        self.assertIn("| TER 3105 | 09:12 | 11:52 |", checked)
        # One note, at the end, never inside the table.
        self.assertEqual(checked.count("Non vérifié"), 1)
        self.assertTrue(checked.endswith(travel_claims.wording("note", "fr")))
        self.assertIn(
            "| Correspondance | 10h05 (non sourcé) | 12h00 (non sourcé) |", checked
        )

    def test_ranges_are_one_value(self) -> None:
        turn = turn_with()
        checked = turn.check("La gare est à 1,5-2 km, soit 20–25 min à pied.", "fr")
        self.assertIn(
            "à (distance non vérifiée), soit (durée non vérifiée) à pied", checked
        )
        self.assertNotIn("1,5", checked)
        self.assertNotIn("20", checked.split("\n\n")[0])

    def test_note_goes_before_the_folded_source_list(self) -> None:
        answer = (
            "La gare est à 2 km.\n\n<details>\n<summary>Sources</summary>\n</details>"
        )
        checked = turn_with().check(answer, "fr")
        self.assertIn("(distance non vérifiée)", checked)
        before, after = checked.split("<details>")
        self.assertIn("Non vérifié", before)
        self.assertNotIn("Non vérifié", after)

    def test_sourced_and_derived_values_pass(self) -> None:
        turn = turn_with(
            "ICE 503 München Hbf 08:55 – Berlin Hbf 13:24, Fahrzeit 4h29",
            "Le trajet dure 2h05 en moyenne. La gare est à 1,2 km du centre.",
        )
        for answer in (
            "Der ICE 503 fährt um 08:55 ab und ist um 13:24 in Berlin (4 h 29 min).",
            "Comptez 2 h 05 min de train ; la gare est à 1,2 km du centre.",
            "Prévoyez 15 min de marge à l'arrivée.",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(turn.check(answer, "fr"), answer)

    def test_users_own_values_and_computed_values_pass(self) -> None:
        turn = turn_with(asked="j'ai 20 minutes de marche jusqu'à la gare, rdv 12h30")
        turn.compute(
            "| 1. votre position → Gare (à pied) | au plus tard 11:40 | au plus tard 12:00 | 20 min |"
        )
        answer = (
            "Avec vos 20 minutes de marche, partez au plus tard à 11:40 pour 12h30."
        )
        self.assertEqual(turn.check(answer, "fr"), answer)

    def test_travel_values_are_left_to_the_travel_check(self) -> None:
        from services.orchestrator import unsourced_details

        answer = (
            "La gare est à environ 10 minutes à pied. Le train part à 10h05 "
            "et arrive à 12h00, soit 1 h 55 min de trajet sur 230 km."
        )
        turn = turn_with()
        own = unsourced_details.Evidence.of([turn.owned_values(answer)])
        nothing = unsourced_details.Evidence.of([])
        left = unsourced_details.unsupported(answer, nothing, own)
        self.assertEqual(left, [], [d.text for d in left])

    def test_inactive_turn_changes_nothing(self) -> None:
        turn = travel_claims.Turn()
        self.assertEqual(turn.check("Cuire 10 minutes.", "fr"), "Cuire 10 minutes.")

    def test_note_language_follows_the_answer(self) -> None:
        turn = turn_with()
        for language, word in (
            ("en", "Not verified"),
            ("de", "Nicht überprüft"),
            ("es", "Sin verificar"),
            ("it", "Non verificato"),
        ):
            with self.subTest(language=language):
                self.assertIn(word, turn.check("It is 3 km away.", language))


RETRIEVED = [
    "https://transport.example.test/caen-paris",
    "https://metro.example.test/line-3",
]


def plan(
    arguments: dict[str, Any], language: str = "fr", km: float | None = None
) -> dict[str, Any]:
    return journey.build(
        journey.Journey.model_validate(arguments), RETRIEVED, language, first_stop_km=km
    )


class JourneyPlanTests(unittest.TestCase):
    def test_backwards_from_the_deadline_door_to_door(self) -> None:
        data = plan(
            {
                "destination": "Restaurant Le Fictif, Paris",
                "arrive_by": "2026-11-17T12:30",
                "from_device_position": True,
                "legs": [
                    {
                        "origin": "home",
                        "destination": "Gare de Caen",
                        "mode": "local_transit",
                        "duration_minutes": 25,
                        "source_url": "https://metro.example.test/line-3",
                    },
                    {
                        "origin": "Gare de Caen",
                        "destination": "Paris Saint-Lazare",
                        "mode": "train",
                        "service": "TER 3105",
                        "depart": "09:12",
                        "arrive": "11:52",
                        "source_url": "https://transport.example.test/caen-paris/",
                    },
                    {
                        "origin": "Paris Saint-Lazare",
                        "destination": "Restaurant Le Fictif",
                        "mode": "walk",
                        "duration_minutes": 12,
                        "source_url": "https://metro.example.test/line-3",
                    },
                ],
            },
            km=7.4,
        )
        self.assertEqual(data["verdict"], "on_time")
        first, train, last = data["legs"]
        # The walk ends at the deadline; the first mile ends at the train.
        self.assertEqual(
            (last["depart"], last["arrive"]), ("2026-11-17T12:18", "2026-11-17T12:30")
        )
        self.assertEqual(first["arrive"], "2026-11-17T09:12")
        self.assertEqual(first["depart"], "2026-11-17T08:47")
        self.assertEqual(train["duration"], 160)
        rendered = data["rendered"]
        self.assertIn("votre position → Gare de Caen", rendered)
        self.assertIn("Départ au plus tard à 08:47", rendered)
        self.assertIn("distance à vol d’oiseau 7,4 km (pas un trajet)", rendered)
        self.assertIn("Correspondance à Gare de Caen", rendered)
        # The values written are the ones the claims check accepts.
        turn = travel_claims.Turn()
        turn.activate()
        turn.compute(rendered)
        self.assertEqual(turn.check(rendered, "fr"), rendered)

    def test_unsourced_leg_is_not_found_with_a_next_step(self) -> None:
        data = plan(
            {
                "destination": "Berlin Hbf",
                "arrive_by": "14:00",
                "legs": [
                    {
                        "origin": "Schwabing",
                        "destination": "München Hbf",
                        "mode": "local_transit",
                        "duration_minutes": 20,
                    },
                    {
                        "origin": "München Hbf",
                        "destination": "Berlin Hbf",
                        "mode": "train",
                        "service": "ICE 503",
                        "depart": "08:55",
                        "arrive": "13:24",
                        "source_url": "https://transport.example.test/caen-paris",
                    },
                ],
            },
            language="de",
        )
        self.assertEqual(data["verdict"], "incomplete")
        self.assertEqual(data["legs"][0]["status"], "not_found")
        self.assertIsNone(data["legs"][0]["duration"])
        self.assertIn(
            "Abschnitt nicht gefunden: Schwabing → München Hbf", data["rendered"]
        )
        self.assertIn("spätestens um 08:55 ab München Hbf", data["rendered"])
        self.assertNotIn("20 min", data["rendered"])

    def test_source_not_read_this_turn_does_not_count(self) -> None:
        data = plan(
            {
                "destination": "Genève Aéroport",
                "arrive_by": "09:00",
                "legs": [
                    {
                        "origin": "Lyon Part-Dieu",
                        "destination": "Genève Aéroport",
                        "mode": "train",
                        "depart": "06:34",
                        "arrive": "08:40",
                        "source_url": "https://remembered.example.test/timetable",
                    },
                ],
            }
        )
        self.assertEqual(data["legs"][0]["status"], "not_found")
        self.assertEqual(data["legs"][0]["reason"], "source_not_read_this_turn")

    def test_late_arrival_and_missed_connection(self) -> None:
        data = plan(
            {
                "destination": "Paris Saint-Lazare",
                "arrive_by": "12:30",
                "legs": [
                    {
                        "origin": "Bayeux",
                        "destination": "Caen",
                        "mode": "train",
                        "depart": "10:00",
                        "arrive": "10:25",
                        "source_url": RETRIEVED[0],
                    },
                    {
                        "origin": "Caen",
                        "destination": "Paris Saint-Lazare",
                        "mode": "train",
                        "depart": "10:20",
                        "arrive": "12:40",
                        "source_url": RETRIEVED[0],
                    },
                ],
            },
            language="en",
        )
        self.assertEqual(data["verdict"], "late")
        self.assertIn("Missed connection between Caen and Caen", data["rendered"])
        self.assertIn(
            "arrives at 12:40, after the requested time (12:30)", data["rendered"]
        )

    def test_bad_time_is_an_error_the_model_can_fix(self) -> None:
        with self.assertRaises(journey.PlanError):
            plan(
                {
                    "destination": "x",
                    "arrive_by": "vers midi",
                    "legs": [{"origin": "a", "destination": "b"}],
                }
            )

    def test_straight_line_distance(self) -> None:
        origin = Position(latitude=48.876, longitude=2.325)  # a Paris station area
        self.assertEqual(journey.straight_line_km(origin, 49.176, -0.347), 197.6)


class JourneyToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_uses_the_turn_sources_position_and_language(self) -> None:
        turn = travel_claims.Turn()
        turn.located(place=STREET, position=Position(latitude=49.18, longitude=-0.37))
        turn.retrieved = lambda: list(RETRIEVED)
        turn.language = "fr"
        token = travel_claims.current.set(turn)
        try:
            with patch(
                "services.orchestrator.journey.geocode_near",
                AsyncMock(return_value=(49.176, -0.347)),
            ):
                output = await journey.execute(
                    json.dumps(
                        {
                            "destination": "Paris Saint-Lazare",
                            "arrive_by": "12:30",
                            "from_device_position": True,
                            "legs": [
                                {
                                    "origin": "home",
                                    "destination": "Gare de Caen",
                                    "mode": "walk",
                                },
                                {
                                    "origin": "Gare de Caen",
                                    "destination": "Paris Saint-Lazare",
                                    "mode": "train",
                                    "depart": "09:12",
                                    "arrive": "11:52",
                                    "source_url": RETRIEVED[0],
                                },
                            ],
                        }
                    ),
                    5,
                )
        finally:
            travel_claims.current.reset(token)
        data = output["data"]
        assert isinstance(data, dict)
        self.assertEqual(data["straight_line_km_to_first_stop"], 1.7)
        self.assertIn(
            "Étape non trouvée : votre position → Gare de Caen", data["rendered"]
        )
        self.assertIn(data["rendered"], turn.computed)
        # A walking time is never derived from the straight line.
        self.assertIsNone(data["legs"][0]["duration"])

    async def test_trip_research_is_bounded_and_never_forced(self) -> None:
        from types import SimpleNamespace

        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.loop import Call

        turn = travel_claims.Turn()
        turn.activate()
        turn.journey_offered = True
        token = travel_claims.current.set(turn)
        tools = ChatTools(Interaction(), "trains Caen Paris")
        model = SimpleNamespace(
            required_file_tool=None,
            answer_only=False,
            has_attachments=False,
            configuration=SimpleNamespace(input_eur_per_mtok=0),
        )
        tools.model = model  # type: ignore[assignment]
        found = {
            "data": {
                "results": [
                    {
                        "link": RETRIEVED[0],
                        "title": "Caen Paris",
                        "snippet": "09:12 11:52",
                    }
                ]
            }
        }
        search = Call("1", "web_search", '{"query": "trains Caen Paris", "lang": "fr"}')
        try:
            with patch(
                "services.orchestrator.tools.Runtime.execute",
                AsyncMock(return_value=found),
            ):
                for count in range(1, journey.RESEARCH_RESULTS + 1):
                    output = await tools.execute(search, 10)
                    # The plan is offered, never required: a model that writes
                    # instead of calling it does not fail the turn.
                    self.assertIsNone(model.required_file_tool)
                    closed = count >= journey.RESEARCH_RESULTS
                    self.assertEqual(model.answer_only, closed)
                    self.assertEqual(
                        output["next_step"],
                        journey.WRITE_NOW if closed else journey.NEXT_STEP,
                    )
            # The time share closes research too, whatever the count.
            model.answer_only = False
            turn.results = 0
            turn.started -= journey.research_seconds() + 1
            with patch(
                "services.orchestrator.tools.Runtime.execute",
                AsyncMock(return_value=found),
            ):
                await tools.execute(search, 10)
            self.assertTrue(model.answer_only)
        finally:
            travel_claims.current.reset(token)

    def test_research_share_is_bounded(self) -> None:
        for value, seconds in (("40", 40.0), ("5", 10.0), ("500", 80.0), ("x", 40.0)):
            with patch.dict(os.environ, {"ATLAS_TRIP_RESEARCH_SECONDS": value}):
                self.assertEqual(journey.research_seconds(), seconds)

    async def test_invalid_arguments_return_the_schema(self) -> None:
        output = await journey.execute('{"destination": "x", "legs": []}', 5)
        self.assertEqual(output["error"], "invalid_arguments")
        self.assertIn("schema", output)

    def test_offered_on_trips_in_several_languages_only(self) -> None:
        trips = (
            "demain midi je dois etre gare saint nazaire a 12h30 donne moi les trains possibles du matin",
            "I need to be at Berlin Hbf by 14:00 tomorrow, which trains from Munich?",
            "Ich muss morgen um 9 Uhr am Flughafen Genf sein, welcher Zug ab Lyon?",
            "tengo que estar en la estación de Atocha a las 10h, qué tren desde Valencia",
            "devo essere alla stazione di Milano Centrale alle 18:00, che treno prendo da Torino",
            "comment aller à l'aéroport depuis chez moi",
        )
        for text in trips:
            with self.subTest(text=text):
                self.assertTrue(journey.relevant(text))
        for text in (
            "recette de gâteau au chocolat en 10 minutes",
            "explique-moi la théorie de la relativité",
            "quelle heure est-il à Tokyo",
            "write a poem about trains",
        ):
            with self.subTest(text=text):
                self.assertFalse(journey.relevant(text))
        self.assertTrue(
            journey.relevant("non, Saint-Lazare", trips[0]),
            "a short correction inherits the trip",
        )
        self.assertTrue(journey.relevant("et pour la gare ?", located=True))


class LowValueResultTests(unittest.TestCase):
    def test_social_and_translation_pages_are_dropped(self) -> None:
        from services.orchestrator.tools import drop_low_value

        rows = [
            {"link": "https://www.instagram.com/p/fictif/"},
            {"link": "https://m.facebook.com/gare.fictive"},
            {"link": "https://translate.google.fr/?sl=fr&tl=en"},
            {"link": "https://fictif-translate.translate.goog/page"},
            {"link": "https://www.pinterest.fr/pin/1"},
            {"link": "https://transport.example.test/caen-paris"},
        ]
        query = "trains Caen Paris Saint-Lazare demain matin"
        kept = drop_low_value({"results": rows}, query)["results"]
        self.assertEqual(kept, [{"link": "https://transport.example.test/caen-paris"}])

    def test_kept_when_asked_for_or_when_nothing_else_remains(self) -> None:
        from services.orchestrator.tools import drop_low_value

        rows = [
            {"link": "https://www.instagram.com/fictif_cafe/"},
            {"link": "https://cafe.example.test/"},
        ]
        self.assertEqual(
            drop_low_value({"results": rows}, "compte Instagram du Café Fictif")[
                "results"
            ],
            rows,
        )
        only = [{"link": "https://www.facebook.com/fictif"}]
        self.assertEqual(
            drop_low_value({"results": only}, "Café Fictif")["results"], only
        )


class DiscoveryQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_note_never_reaches_the_discovery_search(self) -> None:
        from services.orchestrator.location_route import replace_question

        question = "prix du billet de train Caen Paris aujourd'hui"
        req = request(question)
        replace_question(req, question, "[Position shared by the user's device: x]")
        self.assertIn("Position shared", req.user_text)
        self.assertEqual(asked_text(req), question)
        from services.orchestrator.search_policy import required_research

        calls = required_research(asked_text(req), "fr")
        self.assertEqual(json.loads(calls[0].arguments)["query"], question)

    def test_service_note_does_not_change_the_language(self) -> None:
        from packages.language import conversation_language
        from services.orchestrator.location_route import position_note
        from services.orchestrator.searxng import search_language

        question = (
            "demain midi je dois etre gare saint lazare a 12h30 donne moi les trains "
            "possibles du matin pour etre a l'heure"
        )
        for note in (
            position_note(STREET, 20, False),
            position_note("place de la Mairie, Caen", None, True),
        ):
            with self.subTest(note=note[:30]):
                req = request(question)
                from services.orchestrator.location_route import replace_question

                replace_question(req, question, note)
                self.assertEqual(
                    conversation_language(req.conversation_messages, "en"), "fr"
                )
                self.assertEqual(search_language(req.conversation_messages, "en"), "fr")


if __name__ == "__main__":
    unittest.main()


class ReverseGeocodingTests(unittest.IsolatedAsyncioTestCase):
    """Any country: a transient map failure is retried, then a second service answers."""

    async def test_transient_failure_is_retried_then_photon_answers(self) -> None:
        from services.orchestrator.nearby import OSM, NearbyError

        munich = Position(latitude=48.15, longitude=11.54)
        photon = {
            "features": [
                {
                    "properties": {
                        "street": "Musterstraße",
                        "district": "Neuhausen",
                        "city": "München",
                        "postcode": "80636",
                        "country": "Deutschland",
                    }
                }
            ]
        }
        calls: list[str] = []

        async def request(
            service: str, parameters: dict[str, str], endpoint: str | None = None
        ) -> object:
            calls.append(str(endpoint))
            if "photon" in str(endpoint):
                return photon
            raise NearbyError("osm_unavailable")

        osm = OSM()
        with (
            patch.object(osm, "request", request),
            patch("services.orchestrator.nearby.asyncio.sleep", AsyncMock()),
            patch.dict(
                os.environ,
                {"ATLAS_PHOTON_REVERSE_URL": "https://photon.example.test/reverse"},
            ),
        ):
            place = await osm.reverse(munich)
        self.assertEqual(place, "Musterstraße, Neuhausen, 80636 München, Deutschland")
        self.assertEqual(len(calls), 3)  # Nominatim twice, then Photon

    async def test_a_lasting_refusal_is_not_retried_and_fallback_can_be_off(
        self,
    ) -> None:
        from services.orchestrator.nearby import OSM, NearbyError

        calls: list[str] = []

        async def request(
            service: str, parameters: dict[str, str], endpoint: str | None = None
        ) -> object:
            calls.append(str(endpoint))
            raise NearbyError("osm_http_403")

        osm = OSM()
        with (
            patch.object(osm, "request", request),
            patch.dict(os.environ, {"ATLAS_PHOTON_REVERSE_URL": ""}),
            self.assertRaises(NearbyError),
        ):
            await osm.reverse(Position(latitude=52.52, longitude=13.405))
        self.assertEqual(len(calls), 1)


class FailureTextTests(unittest.TestCase):
    def test_unexpected_failure_reads_in_the_user_language_without_internals(
        self,
    ) -> None:
        from packages.user_errors import unexpected_failure

        for language in ("fr", "en", "de", "es", "it", "nl"):
            text = unexpected_failure(language)
            self.assertNotRegex(text, r"RuntimeError|EUR|limit|\\d")
        self.assertIn("erreur inattendue", unexpected_failure("fr"))
