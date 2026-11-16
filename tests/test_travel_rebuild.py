"""Trip answers rewritten from sourced facts, checked again, or built by code.

Staging -100 (2026-11-16, phone): leaving clauses out after the fact left a
Munich-Berlin answer with one line although the pages gave the ICE times, a
Lyon-Geneva answer with "**1." skeletons and the airport station the source
never named, and a placeholder time in a Caen-Paris answer. The answers below
are written the way the model wrote them; every address is fictional.
"""

import unittest
from unittest.mock import AsyncMock, patch

from services.orchestrator import travel_claims, travel_rebuild

BERLIN_URL = "https://rail.example.test/munich-berlin"
GENEVA_URL = "https://rail.example.test/lyon-geneve"
PARIS_URL = "https://rail.example.test/caen-paris"
COMPLETE = "services.orchestrator.unsourced_details._complete"

BERLIN_ANSWER = (
    "**Fastest option: ICE direct**\n\n"
    "1. **Leave home** around 09:00 and walk to München Hbf (about 10 minutes).\n"
    "2. **ICE München Hbf → Berlin Hbf**: trains take 4h00–4h30; the 09:30 "
    "departure arrives around 13:15–13:30.\n"
    "3. **Arrival**: Berlin Hbf, 3h53 for the fastest connection.\n\n"
    "Notes: Allow a few minutes to walk from Berlin Hbf's platform to the exit."
)
GENEVA_ANSWER = (
    "**1. Rejoindre Lyon Part-Dieu (environ 15 min)**\n"
    "Prenez le tram T1 depuis votre domicile.\n\n"
    "**2. Train Lyon Part-Dieu → Genève**\n"
    "D'après les horaires SNCF Connect, le TER part à 6h38 et arrive à Genève "
    "Aéroport vers 8h43.\n\n"
    "**3. Arrivée à l'aéroport**\n"
    "Comptez 10 min de marche jusqu'au terminal."
)
PARIS_ANSWER = (
    "Le TER 3105 part de Caen à 9h12, arrivée vers 12 h 2x à Paris Saint-Lazare. "
    "Bon déjeuner !"
)


def trip(url: str, snippet: str, asked: str) -> travel_claims.Turn:
    turn = travel_claims.Turn(asked=[asked])
    turn.activate()
    turn.retrieved = lambda: [url]
    turn.collect(
        {"data": {"results": [{"link": url, "title": "Timetable", "snippet": snippet}]}}
    )
    return turn


def berlin() -> travel_claims.Turn:
    return trip(
        BERLIN_URL,
        "ICE trains from München Hbf to Berlin Hbf take from 4 h 02 min. "
        "ICE 1006 München Hbf 08:55 - Berlin Hbf 13:24.",
        "I need to be in Berlin this afternoon, leaving from home in Munich",
    )


def geneva() -> travel_claims.Turn:
    return trip(
        GENEVA_URL,
        "TER Lyon Part-Dieu 06:38 → Genève 08:43, direct.",
        "je dois être à l'aéroport de Genève demain matin, je pars de Villeurbanne",
    )


def paris() -> travel_claims.Turn:
    return trip(
        PARIS_URL,
        "TER 3105 Caen 09:12 → Paris Saint-Lazare 11:52.",
        "déjeuner à Saint-Lazare demain midi trente, je pars de Caen",
    )


def model(text: str) -> AsyncMock:
    return AsyncMock(return_value={"text": text, "cost_eur": "0.0004"})


def well_formed(test: unittest.TestCase, text: str) -> None:
    for line in text.split("\n"):
        test.assertEqual(line.count("**") % 2, 0, line)
        if line.strip():
            test.assertRegex(line, r"[^\W\d_]", f"hollow line {line!r}")


class RewriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_munich_berlin_keeps_the_sourced_ice(self) -> None:
        rewritten = (
            "The fastest ICE from München Hbf to Berlin Hbf takes 4 h 02 min "
            f"([rail.example.test]({BERLIN_URL})). ICE 1006 leaves München Hbf at "
            f"08:55 and reaches Berlin Hbf at 13:24 ([rail.example.test]({BERLIN_URL}))."
            "\n\nThe walk from your home to München Hbf could not be verified: "
            "check it in the operator's journey planner."
        )
        turn = berlin()
        call = model(rewritten)
        with patch(COMPLETE, call):
            final, report = await travel_rebuild.review(
                turn, BERLIN_ANSWER, "en", question=turn.asked[0], profile="p"
            )
        self.assertEqual(final, rewritten)
        self.assertEqual((report.rebuilt, report.built), ("kept", "model"))
        self.assertGreater(report.cost_eur, 0)
        awaited = call.await_args
        assert awaited is not None
        prompt = awaited.args[0]["messages"][1]["content"]
        self.assertIn("4 h 02 min", prompt)
        self.assertIn(BERLIN_URL, prompt)
        self.assertEqual(awaited.args[0]["reasoning_effort"], "none")

    async def test_an_invented_rewrite_falls_back_to_sourced_excerpts(self) -> None:
        turn = berlin()
        with patch(COMPLETE, model("Take ICE 1010 at 10:00, arriving 14:00.")):
            final, report = await travel_rebuild.review(
                turn, BERLIN_ANSWER, "en", question=turn.asked[0], profile="p"
            )
        self.assertEqual(report.rebuilt, "rejected:unsourced")
        self.assertEqual(report.built, "code+excerpts")
        self.assertNotIn("ICE 1010", final)
        # The person gets the sourced facts, with their page.
        self.assertIn("4 h 02 min", final)
        self.assertIn("ICE 1006 München Hbf 08:55 - Berlin Hbf 13:24", final)
        self.assertIn(f"[rail.example.test]({BERLIN_URL})", final)
        self.assertIn("journey planner", final)
        well_formed(self, final.rsplit("\n\n", 1)[0])

    async def test_lyon_geneva_never_keeps_the_airport_or_a_skeleton(self) -> None:
        turn = geneva()
        with patch(COMPLETE, AsyncMock(side_effect=TimeoutError)):
            final, report = await travel_rebuild.review(
                turn, GENEVA_ANSWER, "fr", question=turn.asked[0], profile="p"
            )
        self.assertEqual(report.rebuilt, "late")
        body = final.rsplit("\n\n", 1)[0]
        self.assertNotIn("Genève Aéroport", body)
        well_formed(self, body)
        self.assertIn("06:38 → Genève 08:43", final)

    async def test_lyon_geneva_rewrite_naming_the_airport_is_rejected(self) -> None:
        turn = geneva()
        wrong = (
            "Le TER part de Lyon Part-Dieu à 6h38 et arrive à Genève Aéroport à 8h43 "
            f"([rail.example.test]({GENEVA_URL}))."
        )
        right = (
            "Le TER part de Lyon Part-Dieu à 6h38 et arrive à Genève à 8h43 "
            f"([rail.example.test]({GENEVA_URL})). Le trajet jusqu'à Part-Dieu et "
            "de la gare à l'aéroport n'a pas pu être vérifié : consultez le "
            "calculateur d'itinéraire de l'exploitant."
        )
        for text, outcome in ((wrong, "rejected:unsourced"), (right, "kept")):
            with self.subTest(outcome=outcome), patch(COMPLETE, model(text)):
                final, report = await travel_rebuild.review(
                    turn, GENEVA_ANSWER, "fr", question=turn.asked[0], profile="p"
                )
                self.assertEqual(report.rebuilt, outcome)
                self.assertEqual(final == right, outcome == "kept")

    async def test_caen_paris_placeholder_time_is_never_kept(self) -> None:
        turn = paris()
        found = turn.findings(PARIS_ANSWER)
        self.assertIn("12 h 2x", found.labels())
        rewritten = (
            "Le TER 3105 part de Caen à 9h12 et arrive à Paris Saint-Lazare à 11h52 "
            f"([rail.example.test]({PARIS_URL})). Bon déjeuner !"
        )
        with patch(COMPLETE, model(rewritten)):
            final, report = await travel_rebuild.review(
                turn, PARIS_ANSWER, "fr", question=turn.asked[0], profile="p"
            )
        self.assertEqual(final, rewritten)
        self.assertNotRegex(final, r"\d\s?h\s?\d?x")

    async def test_links_to_pages_not_read_are_rejected(self) -> None:
        turn = paris()
        text = (
            "Le TER 3105 part de Caen à 9h12 "
            "([ailleurs](https://elsewhere.example.test/tt))."
        )
        with patch(COMPLETE, model(text)):
            _, report = await travel_rebuild.review(
                turn, PARIS_ANSWER, "fr", question=turn.asked[0], profile="p"
            )
        self.assertEqual(report.rebuilt, "rejected:links")

    async def test_a_sourced_answer_makes_no_call(self) -> None:
        turn = paris()
        answer = (
            "Le TER 3105 part de Caen à 9h12 et arrive à Paris Saint-Lazare à 11h52."
        )
        call = model("x")
        with patch(COMPLETE, call):
            final, report = await travel_rebuild.review(
                turn, answer, "fr", question=turn.asked[0], profile="p"
            )
        self.assertEqual(final, answer)
        call.assert_not_awaited()
        self.assertEqual(report.marked, 0)

    async def test_off_switch_keeps_the_code_built_answer(self) -> None:
        turn = paris()
        call = model("x")
        with (
            patch.dict("os.environ", {"ATLAS_TRAVEL_REBUILD": "off"}),
            patch(COMPLETE, call),
        ):
            final, report = await travel_rebuild.review(
                turn, PARIS_ANSWER, "fr", question=turn.asked[0], profile="p"
            )
        call.assert_not_awaited()
        self.assertEqual(report.rebuilt, "off")
        self.assertIn("Non vérifié", final)


class SameFactTests(unittest.TestCase):
    def test_one_fact_in_two_spellings(self) -> None:
        cases = (
            ("Fahrzeit 4 h 30 min", "The trip takes 4h30."),
            ("journey time 4:30", "The trip takes 4 h 30."),
            ("ICE ab 4 h 02 min", "The ICE takes about 4 h."),
            ("trains take 4 h 02 min to 4 h 29 min", "Trains take 4h00–4h30."),
        )
        for source, answer in cases:
            with self.subTest(source=source):
                turn = trip(BERLIN_URL, source, "Munich to Berlin")
                self.assertEqual(turn.check(answer, "en"), answer)

    def test_close_but_different_values_are_not_the_same(self) -> None:
        turn = trip(BERLIN_URL, "TER Caen 09:12 Paris 11:52", "Caen Paris")
        for answer in ("Arrival at 12h00.", "The trip takes 3 h.", "Arrival 12 h 2x."):
            with self.subTest(answer=answer):
                self.assertNotEqual(turn.check(answer, "en"), answer)

    def test_a_city_is_not_its_airport_station(self) -> None:
        turn = geneva()
        self.assertTrue(turn.findings("Arrivée à Genève Aéroport vers 8h43."))
        self.assertFalse(turn.findings("Arrivée à Genève à 8h43."))


class WellFormedTests(unittest.TestCase):
    def test_no_dangling_marks_or_empty_items(self) -> None:
        text = (
            "**1.\n**2. Train Lyon Part-Dieu → Genève**\n- \n3)\n"
            "D'après les horaires, le train part à 6h38.\n**Note** : `code"
        )
        self.assertEqual(
            travel_claims.wellformed(text),
            "**2. Train Lyon Part-Dieu → Genève**\n"
            "D'après les horaires, le train part à 6h38.\n**Note** : code",
        )

    def test_the_code_path_leaves_no_skeleton(self) -> None:
        checked = geneva().check(GENEVA_ANSWER, "fr")
        body = checked.rsplit("\n\n", 1)[0]
        well_formed(self, body)
        # Headings whose content went, go with it.
        for heading in ("Rejoindre", "Train Lyon", "Arrivée"):
            self.assertNotIn(heading, checked)


if __name__ == "__main__":
    unittest.main()
