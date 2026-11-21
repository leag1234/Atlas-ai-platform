"""Trip answers of staging -142 (UI trip battery on main 46f72fa).

- Villeurbanne to Geneva airport on a Sunday: the late plan's note kept the
  first search's reasons, "every itinerary found leaves at night (from
  01:30)" and "the fastest takes 3 h 22", over options leaving at 05:38 and
  taking 2 h 53 to 3 h 37. A note is now built from the options shown, and
  every claim of it is checked against them below, for any late plan.
- New York to Boston: "the best actual train is the Amtrak Northeast
  Regional 66:" and that option's first walk, alone on its line, above a
  plan whose option 1 is a coach.
- Madrid to Barcelona: "desde tu posición" over a plan reading "su
  posición" and "compruébelas": the block follows the conversation's
  register (tu/vous, tú/usted, du/Sie, tu/Lei).
- Every answer ended with "Sources consulted in this conversation — Web
  search… / Reading page…": the heading of Atlas's own sources list, whose
  only pages were the journey planner's attribution pages.

Streets are fictional; the clock is patched wherever a plan is computed.
"""

import re
import unittest
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from services.orchestrator import source_truth
from services.orchestrator import transit_planner as tp
from test_person_local_day import NYC_DATED, TripTextTest
from test_planned_trip_block import MADRID_QUESTION, checked
from test_trip_answers_135 import GenevaTest
from test_transit_planner import Base
from test_trip_answers_141 import sunday

_CLOCK = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")
_DURATION = re.compile(r"(?:(\d+) h (\d{2}) min|(?<![\d h])(\d+) min)")


def _minutes(match: re.Match[str]) -> int:
    if match[1]:
        return int(match[1]) * 60 + int(match[2])
    return int(match[3])


def note_problems(data: dict[str, Any]) -> list[str]:
    """What a late plan's note says that its shown options do not hold: an
    unfinished sentence, a clock time that is neither the deadline nor a
    time of a shown option, a duration that is neither one of theirs nor how
    late one arrives, a first arrival that is not the earliest one shown."""
    block = str(data["rendered"])
    paragraphs = block.split("\n\n")
    first_option = next(i for i, p in enumerate(paragraphs) if p.startswith("**") and i)
    note = " ".join(paragraphs[1:first_option]).strip()
    shown = data["itineraries"]
    deadline = datetime.fromisoformat(str(data["arrive_by"]))
    problems: list[str] = []
    if not note:
        return ["no note"]
    if note[-1] not in ".!?":
        problems.append(f"unfinished: {note[-40:]!r}")
    times = {deadline.strftime("%H:%M")}
    durations: set[int] = set()
    for itinerary in shown:
        depart = datetime.fromisoformat(itinerary["depart"])
        arrive = datetime.fromisoformat(itinerary["arrive"])
        times |= {depart.strftime("%H:%M"), arrive.strftime("%H:%M")}
        durations |= {
            itinerary["minutes"],
            round((arrive - deadline).total_seconds() / 60),
        }
    for match in _CLOCK.finditer(note):
        if f"{int(match[1]):02d}:{match[2]}" not in times:
            problems.append(f"time not of the options: {match[0]}")
    for match in _DURATION.finditer(note):
        if _minutes(match) not in durations:
            problems.append(f"duration not of the options: {match[0]}")
    late = [i for i in shown if datetime.fromisoformat(i["arrive"]) > deadline]
    earliest = min(datetime.fromisoformat(i["arrive"]) for i in late)
    stated = [m[0] for m in _CLOCK.finditer(note) if m[0] != deadline.strftime("%H:%M")]
    if earliest.strftime("%H:%M") not in stated:
        problems.append(f"first arrival {earliest:%H:%M} not stated")
    return problems


class NoteTest(GenevaTest):
    def test_every_note_claim_holds_of_the_options_shown(self) -> None:
        _, _, sunday_data = sunday()
        _, friday_data = self.plan("aéroport de Genève, Le Grand-Saconnex")
        for name, data in (("sunday", sunday_data), ("friday", friday_data)):
            with self.subTest(trip=name):
                self.assertEqual(data["outcome"], "planned_late")
                self.assertEqual(note_problems(data), [])
                # The first search's reasons are not the options' (-142).
                self.assertNotIn("dès", data["rendered"].split("**Option 1")[0])
                self.assertNotIn("le plus rapide", data["rendered"])

    def test_the_checker_finds_the_staging_note(self) -> None:
        _, _, data = sunday()
        stale = dict(data)
        stale["rendered"] = data["rendered"].replace(
            "Aucun itinéraire trouvé n’arrive au plus tard à 09:00 sans voyager de nuit.",
            "Aucun itinéraire raisonnable trouvé n’arrive au plus tard à 09:00 : "
            "l’itinéraire le plus rapide trouvé met 3 h 22 min pour 108 km; tous "
            "les itinéraires trouvés partent la nuit (dès 01:30). Les options "
            "ci-dessous sont les premières raisonnables trouvées ; elles",
        )
        found = note_problems(stale)
        self.assertIn("time not of the options: 01:30", found)
        self.assertIn("duration not of the options: 3 h 22 min", found)

    def test_reasons_in_each_language(self) -> None:
        deadline = datetime(2026, 12, 7, 10, 0, tzinfo=ZoneInfo("Europe/Oslo"))
        now = deadline - timedelta(days=1)
        night = {
            "depart": deadline.replace(hour=2),
            "arrive": deadline.replace(hour=8),
            "latest": deadline.replace(hour=8),
            "minutes": 360,
        }
        day = {**night, "depart": deadline.replace(hour=6), "minutes": 120}
        shown = [
            {
                "depart": deadline.replace(hour=9),
                "arrive": deadline.replace(hour=10, minute=40),
                "latest": deadline.replace(hour=10, minute=40),
                "minutes": 100,
            }
        ]
        for language in ("en", "fr", "de", "es", "it"):
            w = tp.words(language)
            for found, key in (
                ([], "late_none"),
                ([night], "late_night"),
                ([day], "late_longer"),
            ):
                note = tp.late_note(found, shown, deadline, now, w)
                self.assertTrue(note.startswith(w[key].format(d="10:00")), note)
                self.assertIn("10:40", note)
                self.assertIn("40 min", note)
                self.assertTrue(note.endswith("."), note)


class PreludeTest(TripTextTest):
    def test_a_best_claim_on_a_later_option_and_a_stray_leg_go(self) -> None:
        turn, data = self.new_york()
        prelude = (
            "For tomorrow, Saturday 2026-11-20, the best actual train is the "
            "Amtrak Northeast Regional 66:\n\n"
            "Walk 3 min from your position on West 33rd Street to NY Moynihan "
            "Train Hall at Penn Station"
        )
        final, _ = checked(turn, prelude + "\n\n" + data["rendered"], "en", NYC_DATED)
        self.assertEqual(final, data["rendered"], final)

    def test_what_follows_the_plan_order_stays(self) -> None:
        turn, data = self.new_york()
        for opening in (
            "The best option is the FlixBus N2605, the first below.",
            "The Northeast Regional 66 leaves from Moynihan Train Hall.",
            "Here are three options for tomorrow:",
        ):
            with self.subTest(opening=opening):
                final, _ = checked(
                    turn, opening + "\n\n" + data["rendered"], "en", NYC_DATED
                )
                self.assertTrue(final.startswith(opening), final)

    def test_in_other_languages(self) -> None:
        turn, _ = self.new_york()
        restates = tp._restates(turn)
        assert restates is not None
        for sentence in (
            "Le meilleur train est le Northeast Regional 66.",
            "Der beste Zug ist der Northeast Regional 66.",
            "El mejor tren es el Northeast Regional 66.",
            "Il treno migliore è il Northeast Regional 66.",
            "Marche 3 min de votre position à Moynihan Train Hall",
        ):
            self.assertTrue(restates(sentence), sentence)
        self.assertFalse(restates("Il miglior pullman è il FlixBus N2605."))


class RegisterTest(TripTextTest):
    def test_the_block_follows_the_model_register(self) -> None:
        turn, data = self.madrid()
        opening = (
            "Desde tu posición en Calle Ficticia 9 (Madrid), estos son los trenes "
            "que te llevan a Barcelona-Sants antes de las 13:00:"
        )
        final, _ = checked(
            turn, opening + "\n\n" + data["rendered"], "es", MADRID_QUESTION
        )
        self.assertIn("compruébalas con el operador", final)
        self.assertNotIn("compruébelas", final)
        self.assertEqual(turn.plan, final.split("\n\n", 1)[1])

    def test_each_language_and_unsure(self) -> None:
        for language, text, expected in (
            ("fr", "Voici ton trajet, tu pars à 8 h.", "informal"),
            ("fr", "Voici votre trajet.", "formal"),
            ("fr", "Voici le trajet.", ""),
            ("fr", "Tu peux partir, ou vous pouvez attendre.", ""),
            ("es", "Puedes salir desde tu casa.", "informal"),
            ("es", "Usted puede salir a las 8.", "formal"),
            ("de", "Von deinem Standort aus fährst du los.", "informal"),
            ("de", "Von Ihrem Standort aus fahren Sie los.", "formal"),
            ("de", "Sie fahren um 8 Uhr.", ""),
            ("it", "Dalla tua posizione puoi partire.", "informal"),
            ("en", "From your position you can leave.", ""),
        ):
            with self.subTest(language=language, text=text):
                self.assertEqual(tp.register(text, language), expected)

    def test_every_formal_phrase_is_the_renderer_s(self) -> None:
        for language, pairs in tp.INFORMAL.items():
            templates = " ".join(
                str(value)
                for value in tp.WORDS[language].values()
                if isinstance(value, str)
            )
            for formal, informal in pairs:
                with self.subTest(language=language, formal=formal):
                    self.assertIn(formal, templates)
                    self.assertNotEqual(formal, informal)

    def test_a_formal_or_neutral_conversation_keeps_the_block(self) -> None:
        turn, data = self.caen()
        for opening in ("Voici vos options :", "Voici les options :"):
            self.assertEqual(
                tp.in_register(data["rendered"], "fr", opening, "caen paris demain"),
                data["rendered"],
            )
        # The person's words decide when the model's do not.
        self.assertIn(
            "ta position",
            tp.in_register(
                data["rendered"], "fr", "Voici les options :", "je pars de chez toi"
            ),
        )


class SourcesListTest(Base):
    def test_titles_and_links_never_step_labels(self) -> None:
        token = source_truth.page_titles.set({})
        try:
            source_truth.record_page(
                "https://bus.example.test/lignes/5", "text", "Ligne 5 [horaires](x)"
            )
            footer = source_truth.source_footer(
                ["https://bus.example.test/lignes/5", "https://rail.example.test/a"],
                "fr",
            )
        finally:
            source_truth.page_titles.reset(token)
        self.assertIn(
            "<summary>Sources consultées dans cette conversation</summary>", footer
        )
        self.assertNotIn("Recherche web", footer)
        self.assertIn(
            "- [Ligne 5 horaires x](https://bus.example.test/lignes/5)", footer
        )
        self.assertIn("- [rail.example.test](https://rail.example.test/a)", footer)

    def test_the_planner_pages_are_no_pages_read(self) -> None:
        read = source_truth.pages_read(
            [tp.SOURCES_URL, tp.OSM_URL, "https://bus.example.test/lignes/5"]
        )
        self.assertEqual(read, ["https://bus.example.test/lignes/5"])
        self.assertEqual(
            source_truth.source_footer(source_truth.pages_read([tp.SOURCES_URL]), "en"),
            "",
        )


if __name__ == "__main__":
    unittest.main()
