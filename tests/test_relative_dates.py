"""Relative dates are resolved and checked by code (T8); fictional answers only."""

import random
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from services.orchestrator.chat_pipeline import ChatTools, process
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Reservation, Turn
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.relative_dates import (
    Rewriter,
    asked_weekdays,
    calendar,
    mentions_relative_date,
    note,
    repair,
)
from services.orchestrator.stream_client import sink_context

MONDAY = date(2026, 11, 16)
# Monday 16 November 2026, 11:10 UTC (12:10 in Paris).
NOW = datetime(2026, 11, 16, 11, 10, tzinfo=timezone.utc)


class CueTests(unittest.TestCase):
    def test_realistic_phrasings_name_a_relative_day(self) -> None:
        for question in (
            "il fait quel temps demain a Bayeux",
            "il fera beau demain a caen?",
            "il fera beau demian a caen?",
            "what's the weather this weekend in Lyon",
            "quel temps samedi prochain a Rennes",
            "dans 3 jours il pleut a Brest ?",
            "weather tommorow in Nantes",
            "Wie wird das Wetter übermorgen in Köln?",
            "que tiempo hara pasado manana en Sevilla",
            "che tempo fa domani a Torino",
            "y a-t-il un marché la semaine prochaine à Lisieux ?",
            "tempo amanhã em Lisboa",
            "明天北京天气",
            "what happened 2 days ago in Paris",
        ):
            with self.subTest(question=question):
                self.assertTrue(mentions_relative_date(question))

    def test_other_questions_keep_their_request(self) -> None:
        for question in (
            "Quelle est la capitale du Pérou ?",
            "Explain the domain name system",
            "Morgan Stanley quarterly results",
            "Résume ce texte en trois phrases.",
        ):
            with self.subTest(question=question):
                self.assertFalse(mentions_relative_date(question))
                self.assertEqual(note(question, "", "Europe/Paris", "fr", NOW), "")

    def test_short_follow_up_inherits_the_previous_question(self) -> None:
        self.assertTrue(
            mentions_relative_date("et à Caen ?", "il fait quel temps demain a Bayeux")
        )
        self.assertFalse(mentions_relative_date("et à Caen ?", "la capitale du Pérou"))


class CalendarTests(unittest.TestCase):
    def test_late_evening_utc_is_already_tomorrow_in_paris(self) -> None:
        late = datetime(2026, 11, 16, 23, 30, tzinfo=timezone.utc)
        paris = calendar("Europe/Paris", "fr", late)
        self.assertIn("- today: Tuesday 2026-11-17 (mardi 17 novembre 2026)", paris)
        self.assertIn("- tomorrow: Wednesday 2026-11-18", paris)
        self.assertIn("where it is now 00:30", paris)
        york = calendar("America/New_York", "en", late)
        self.assertIn("- today: Monday 2026-11-16\n", york)
        self.assertIn("UTC-05:00", york)
        # The request default "UTC" means no zone was sent: the home zone is used.
        self.assertIn("Europe/Paris", calendar("UTC", "fr", late))

    def test_month_and_year_boundaries(self) -> None:
        eve = datetime(2026, 12, 31, 12, 0, tzinfo=timezone.utc)
        text = calendar("Europe/Paris", "de", eve)
        self.assertIn("- tomorrow: Friday 2027-01-01 (Freitag, 1. Januar 2027)", text)
        self.assertIn("- this weekend: Saturday 2027-01-02 to Sunday 2027-01-03", text)
        self.assertIn("- next week: Monday 2027-01-04 to Sunday 2027-01-10", text)
        end = calendar(
            "Europe/Paris", "en", datetime(2026, 11, 30, 9, tzinfo=timezone.utc)
        )
        self.assertIn("- tomorrow: Tuesday 2026-12-01\n", end)

    def test_weekday_names_in_the_answer_language(self) -> None:
        for language, tomorrow in (
            ("fr", "mardi 17 novembre 2026"),
            ("en", "Tuesday 2026-11-17\n"),
            ("de", "Dienstag, 17. November 2026"),
            ("es", "martes, 17 de noviembre de 2026"),
            ("it", "martedì 17 novembre 2026"),
            ("nl", "dinsdag 17 november 2026"),
        ):
            with self.subTest(language=language):
                self.assertIn(tomorrow, calendar("Europe/Paris", language, NOW))

    def test_weekend_on_a_sunday_is_today_and_yesterday(self) -> None:
        sunday = datetime(2026, 11, 22, 10, tzinfo=timezone.utc)
        text = calendar("Europe/Paris", "en", sunday)
        self.assertIn("- this weekend: Saturday 2026-11-21 to Sunday 2026-11-22", text)
        self.assertIn("- next week: Monday 2026-11-23 to Sunday 2026-11-29", text)

    def test_unknown_language_falls_back_to_english_names(self) -> None:
        self.assertIn("- tomorrow: Tuesday 2026-11-17\n", calendar(None, "xx", NOW))


class RepairTests(unittest.TestCase):
    def test_staging_defect_is_repaired(self) -> None:
        answer = (
            "D'après les prévisions consultées aujourd'hui (16 novembre 2026), "
            "demain lundi 16 novembre 2026 à Bayeux : pluie."
        )
        self.assertEqual(
            repair(answer, "fr", MONDAY),
            "D'après les prévisions consultées aujourd'hui (16 novembre 2026), "
            "demain mardi 17 novembre 2026 à Bayeux : pluie.",
        )

    def test_five_languages_keep_their_form(self) -> None:
        for language, wrong, right in (
            (
                "en",
                "Tomorrow is Monday, 16 November 2026.",
                "Tomorrow is Tuesday, 17 November 2026.",
            ),
            (
                "en",
                "Tomorrow (Monday 16th November) and the day after tomorrow, Wednesday 19th November.",
                "Tomorrow (Tuesday 17th November) and the day after tomorrow, Wednesday 18th November.",
            ),
            ("en", "Tomorrow, Monday, November 16.", "Tomorrow, Tuesday, November 17."),
            (
                "de",
                "Morgen, Montag, den 16. November 2026: Regen.",
                "Morgen, Dienstag, den 17. November 2026: Regen.",
            ),
            (
                "es",
                "Mañana lunes 16 de noviembre de 2026 lloverá.",
                "Mañana martes 17 de noviembre de 2026 lloverá.",
            ),
            (
                "it",
                "Domani lunedì 16 novembre; dopodomani, mercoledì 19 novembre.",
                "Domani martedì 17 novembre; dopodomani, mercoledì 18 novembre.",
            ),
            ("fr", "Hier, samedi 15 novembre.", "Hier, dimanche 15 novembre."),
            ("fr", "DEMAIN LUNDI 16 NOV. 2026", "DEMAIN MARDI 17 NOV. 2026"),
            ("fr", "Demain lundi, pluie.", "Demain mardi, pluie."),
            ("en", "Tomorrow is Monday.", "Tomorrow is Tuesday."),
            (
                "fr",
                "Demain 17 novembre 2025 : pluie.",
                "Demain 17 novembre 2026 : pluie.",
            ),
            (
                "fr",
                "**Demain** (**lundi** 16 novembre) : pluie.",
                "**Demain** (**mardi** 17 novembre) : pluie.",
            ),
            (
                "fr",
                "**Samedi** 22 novembre 2026 : vent.",
                "**Dimanche** 22 novembre 2026 : vent.",
            ),
        ):
            with self.subTest(wrong=wrong):
                self.assertEqual(repair(wrong, language, MONDAY), right)

    def test_year_boundary(self) -> None:
        eve = date(2026, 12, 31)
        self.assertEqual(
            repair("Demain jeudi 31 décembre 2026, feu d'artifice.", "fr", eve),
            "Demain vendredi 1 janvier 2027, feu d'artifice.",
        )
        self.assertEqual(
            repair("Tomorrow, Thursday 31st December.", "en", eve),
            "Tomorrow, Friday 1st January.",
        )

    def test_correct_and_unrelated_text_is_untouched(self) -> None:
        for language, text in (
            ("fr", "Aujourd'hui lundi 16 novembre, demain mardi 17 novembre 2026."),
            ("en", "Tomorrow is Tuesday, 17 November 2026 (today is Monday)."),
            # A list after the relative word is not an apposition.
            ("fr", "Demain, lundi et mardi : du vent."),
            # Morning, not tomorrow.
            ("es", "Esta mañana, lunes 16 de noviembre, llovió."),
            ("de", "Heute Morgen, Montag 16. November, war es kalt."),
            # Another reference frame: a quotation or a date far away.
            ("fr", "« Aujourd'hui, 18 juin 1940 », hier 14 juillet 1789."),
            ("fr", "Le lundi 11 novembre 1918 marque l'armistice."),
            # A weekday with a date far from today and no year is not checked.
            ("fr", "Chaque vendredi 13 juin, une fête."),
            ("fr", "Le rendez-vous est lundi à 16 h, salle 3."),
        ):
            with self.subTest(text=text):
                self.assertEqual(repair(text, language, MONDAY), text)

    def test_weekday_written_with_a_wrong_date(self) -> None:
        # 22 November 2026 is a Sunday. Without a weekday in the question, the
        # weekday is what was worked out wrong.
        self.assertEqual(
            repair("Samedi 22 novembre 2026 sera ensoleillé.", "fr", MONDAY),
            "Dimanche 22 novembre 2026 sera ensoleillé.",
        )
        # The person asked for Saturday: the date is wrong, the weekday is theirs.
        asked = asked_weekdays("quel temps samedi prochain a Rennes", "fr")
        self.assertEqual(asked, frozenset({5}))
        self.assertEqual(
            repair(
                "Samedi 22 novembre 2026 sera ensoleillé.", "fr", MONDAY, asked=asked
            ),
            "Samedi 21 novembre 2026 sera ensoleillé.",
        )
        weekend = asked_weekdays("what's the weather this weekend in Lyon", "en")
        self.assertEqual(weekend, frozenset({5, 6}))
        self.assertEqual(
            repair("Sunday 23 November: wind.", "en", MONDAY, asked=weekend),
            "Sunday 22 November: wind.",
        )

    def test_stream_gives_the_stored_answer_in_any_chunking(self) -> None:
        answer = (
            "Voici la météo à Bayeux.\n\n- Aujourd'hui lundi 16 novembre : nuageux.\n"
            "- Demain lundi 16 novembre 2026 : pluie, 12 °C. Après-demain, mardi 18 "
            "novembre, éclaircies. Ce week-end (samedi 21 et dimanche 23 novembre) : "
            "vent. "
            * 3
            + "Le mercredi 19 novembre 2026 sera doux. " * 4
            + "Demain lundi"
        )
        asked = asked_weekdays("météo ce week-end à Bayeux", "fr")
        expected = repair(answer, "fr", MONDAY, asked=asked)
        self.assertIn("Demain mardi 17 novembre 2026", expected)
        self.assertIn("Après-demain, mercredi 18 novembre", expected)
        self.assertIn("dimanche 22 novembre", expected)
        self.assertTrue(expected.endswith("Demain mardi"))
        generator = random.Random(7)
        for _ in range(200):
            rewriter = Rewriter("fr", MONDAY, asked)
            out: list[str] = []
            index = 0
            while index < len(answer):
                size = generator.randint(1, 15)
                out.append(rewriter.feed(answer[index : index + size]))
                index += size
            out.append(rewriter.finish())
            self.assertEqual("".join(out), expected)

    def test_text_without_dates_streams_without_waiting(self) -> None:
        rewriter = Rewriter("en", MONDAY)
        self.assertEqual(rewriter.feed("The forecast is "), "The forecast is ")
        self.assertEqual(rewriter.feed("rain"), "")
        self.assertEqual(rewriter.feed(" later."), "rain later.")


class TurnTests(unittest.IsolatedAsyncioTestCase):
    """The staging turn, replayed: research tools and model are scripted."""

    async def run_turn(
        self, question: str, answer: str
    ) -> tuple[Interaction, str, str]:
        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=400,
            ),
        )
        seen: list[str] = []

        async def complete(messages: object, timeout: float) -> Turn:
            seen.append(str(messages))
            return Turn(answer, usage=Reservation(10, Decimal(0)))

        streamed: list[str] = []

        async def sink(event: dict[str, object]) -> None:
            delta = event.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                streamed.append(str(delta["content"]))

        search = {
            "data": {
                "results": [
                    {
                        "title": "Prévisions Bayeux",
                        "url": "https://meteo.example/bayeux",
                        "content": "Mardi 17 novembre : pluie, 12 °C.",
                        "consulted_at": NOW.isoformat(),
                    }
                ]
            }
        }
        request = ChatRequest(
            messages=[ChatMessage(role="user", content=question)],
            ui_timezone="Europe/Paris",
        )
        token = sink_context.set(sink)
        try:
            with (
                patch("services.orchestrator.datecalc.utc_now", return_value=NOW),
                patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
                patch.object(model, "complete", side_effect=complete),
                patch.object(ChatTools, "execute", AsyncMock(return_value=search)),
            ):
                item = Interaction()
                await process(request, item)
        finally:
            sink_context.reset(token)
        return item, "".join(streamed), "\n".join(seen)

    async def test_staging_weather_turn(self) -> None:
        item, streamed, sent = await self.run_turn(
            "il fait quel temps demain a Bayeux",
            "D'après les prévisions consultées aujourd'hui (16 novembre 2026), "
            "demain lundi 16 novembre 2026 à Bayeux : pluie, 12 °C.",
        )
        self.assertEqual(item.state, "done")
        self.assertIn("tomorrow: Tuesday 2026-11-17 (mardi 17 novembre 2026)", sent)
        self.assertIn("demain mardi 17 novembre 2026 à Bayeux", item.reponse)
        self.assertNotIn("lundi 16 novembre 2026 à", item.reponse)
        self.assertIn("demain mardi 17 novembre 2026 à Bayeux", streamed)

    async def test_turn_without_relative_day_sends_no_calendar(self) -> None:
        item, _, sent = await self.run_turn(
            "Quelle est la capitale du Pérou ?", "La capitale du Pérou est Lima."
        )
        self.assertEqual(item.reponse, "La capitale du Pérou est Lima.")
        self.assertNotIn("Calendar computed by code", sent)


if __name__ == "__main__":
    unittest.main()
