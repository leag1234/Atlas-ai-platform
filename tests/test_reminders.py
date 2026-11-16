"""Reminders: explicit requests become a calendar file with an alarm, made by code.

Phrasings are realistic and varied: five languages, missing accents and
punctuation, one-letter typos. Dates are computed from a fixed "now" in the
person's IANA zone, across daylight-saving changes; every file is parsed back
with icalendar (BSD-2-Clause, test dependency only), an implementation
independent of the writer, including its VTIMEZONE.
"""

from __future__ import annotations

import asyncio
import importlib
import re
import unittest
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import icalendar

from packages import privacy
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.reminder_ics import (
    Recurrence,
    calendar_file,
    escape_text,
    vtimezone,
)
from services.orchestrator.reminder_parse import Plan, Question, resolve
from services.orchestrator.reminder_route import (
    FAILURE,
    file_name,
    publish,
    question,
    reminder_turn,
    understand,
)

# python-dateutil ships no type information in this environment.
rrulestr = importlib.import_module("dateutil.rrule").rrulestr

PARIS = "Europe/Paris"
# Monday 16 November 2026, 10:00 in Paris.
NOW = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def plan(text: str, now: datetime = NOW, zone: str = PARIS) -> Plan:
    spec = understand(text)
    assert spec is not None, text
    outcome = resolve(spec, now, zone)
    assert isinstance(outcome, Plan), (text, outcome)
    return outcome


def local(
    year: int, month: int, day: int, hour: int, minute: int = 0, zone: str = PARIS
) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(zone))


def event(content: bytes) -> Any:
    calendar = icalendar.Calendar.from_ical(content)
    events = list(calendar.walk("VEVENT"))
    assert len(events) == 1
    return events[0]


class RoutingTests(unittest.TestCase):
    REQUESTS = (
        "rappelle-moi à 16 h 15 d'aller chercher les filles à l'école",
        "rapelle moi demian a 9h d'appeler le plombier",
        "Rappele-moi lundi prochain à 14h30 de renouveler l'assurance",
        "peux tu me rappeler demain à 8h d'arroser les plantes stp",
        "Crée un rappel pour vendredi 18h : réserver le restaurant",
        "fais-moi penser à acheter du pain ce soir à 19h",
        "remind me tomorrow at 9 to call the plumber",
        "remnid me tomorow at 9am to send the invoice",
        "Set a reminder for friday 5pm: submit the report",
        "erinnere mich Freitag um 18 Uhr, den Müll rauszubringen",
        "Kannst du mich morgen um 9 daran erinnern, den Arzt anzurufen?",
        "erinner mich übermorgen um halb 8 an den Elternabend",
        "recuérdame mañana a las 9 llamar al fontanero",
        "recuerdame el viernes a las 6 de la tarde que tengo dentista",
        "ricordami domani alle 9 di chiamare l'idraulico",
        "ricordami venerdi alle 18:30 di comprare il latte",
    )
    # Not reminder requests: the noun alone, a question to answer, a memory.
    OTHERS = (
        "j'ai un rappel de vaccin la semaine prochaine, c'est obligatoire ?",
        "J'ai reçu un rappel de paiement demain à 9h, que faire ?",
        "remind me what RRULE means",
        "Can you remind me how to make crêpes?",
        "Rappelle-moi ce que tu m'as dit hier",
        "rappelle-moi le nom du film qu'on a vu",
        "Erinnere mich, wie das Wetter morgen wird",
        "recuérdame qué significa RRULE",
        "ricordami come si dice grazie in tedesco",
        "Ich kann mich nicht erinnern, ob das Treffen morgen ist",
        "Write an email to remind my team about the meeting tomorrow at 9",
        "Quel est le rappel du vaccin DTP ?",
        "what is a reminder in iOS",
    )

    def test_explicit_requests_are_routed(self) -> None:
        for text in self.REQUESTS:
            with self.subTest(text=text):
                spec = understand(text)
                self.assertIsNotNone(spec)
                assert spec is not None
                self.assertTrue(spec.is_request)

    def test_other_messages_are_not(self) -> None:
        for text in self.OTHERS:
            with self.subTest(text=text):
                self.assertIsNone(understand(text))

    def test_languages(self) -> None:
        expected = ("fr",) * 6 + ("en",) * 3 + ("de",) * 3 + ("es",) * 2 + ("it",) * 2
        for text, language in zip(self.REQUESTS, expected, strict=True):
            with self.subTest(text=text):
                spec = understand(text)
                assert spec is not None
                self.assertEqual(spec.language, language)

    def test_long_messages_are_not_read(self) -> None:
        self.assertIsNone(understand("remind me tomorrow at 9 " + "x" * 500))


class TitleTests(unittest.TestCase):
    def test_titles_drop_trigger_and_time(self) -> None:
        cases = {
            "rappelle-moi à 16 h 15 d'aller chercher les filles à l'école": "Aller chercher les filles à l'école",
            "remind me tomorrow at 9 to call the plumber": "Call the plumber",
            "erinnere mich Freitag um 18 Uhr, den Müll rauszubringen": "Müll rauszubringen",
            "erinnere mich jeden Tag um 20 Uhr an die Tabletten": "Tabletten",
            "recuérdame mañana a las 9 llamar al fontanero": "Llamar al fontanero",
            "ricordami domani alle 9 di chiamare l'idraulico": "Chiamare l'idraulico",
            "Crée un rappel pour vendredi 18h : réserver le restaurant": "Réserver le restaurant",
            "remind me to call the plumber tomorrow at 9 please": "Call the plumber",
            "rapelle moi demian a 9h d'appeler le plombier": "Appeler le plombier",
        }
        for text, title in cases.items():
            with self.subTest(text=text):
                self.assertEqual(plan(text).title, title)

    def test_a_second_request_is_not_folded_into_the_title(self) -> None:
        spec = understand(
            "remind me tomorrow at 9 to call the plumber. Also, what is a P-trap?"
        )
        assert spec is not None
        self.assertEqual(spec.title, "Call the plumber")
        self.assertTrue(spec.extra)

    def test_file_name_is_ascii(self) -> None:
        self.assertEqual(
            file_name("Aller chercher les filles à l'école", "fr"),
            "aller-chercher-les-filles-a-l-ecole.ics",
        )
        self.assertEqual(file_name("", "de"), "erinnerung.ics")
        self.assertRegex(file_name("x" * 200, "en"), r"^[a-z0-9-]{1,48}\.ics$")


class DateTests(unittest.TestCase):
    def test_absolute_and_relative(self) -> None:
        cases = {
            "rappelle-moi à 16 h 15 d'aller chercher les filles": local(
                2026, 11, 16, 16, 15
            ),
            "rappelle-moi à 9h de partir": local(2026, 11, 17, 9),  # 9:00 is past today
            "remind me tomorrow at 9 to call the plumber": local(2026, 11, 17, 9),
            "remind me at 3 to call Bob": local(2026, 11, 16, 15),
            "remind me in 20 minutes to take the pizza out": local(
                2026, 11, 16, 10, 20
            ),
            "rappelle-moi dans 1h30 de sortir le gâteau": local(2026, 11, 16, 11, 30),
            "rappelle-moi dans une demi-heure de rappeler Léa": local(
                2026, 11, 16, 10, 30
            ),
            "rappelle-moi dans 3 jours à 8h de payer": local(2026, 11, 19, 8),
            "erinnere mich Freitag um 18 Uhr an den Müll": local(2026, 11, 20, 18),
            "erinner mich übermorgen um halb 8 an den Elternabend": local(
                2026, 11, 18, 7, 30
            ),
            "recuerdame el viernes a las 6 de la tarde que tengo dentista": local(
                2026, 11, 20, 18
            ),
            "ricordami venerdi alle 18:30 di comprare il latte": local(
                2026, 11, 20, 18, 30
            ),
            "Rappele-moi lundi prochain à 14h30 de renouveler l'assurance": local(
                2026, 11, 23, 14, 30
            ),
            "rappelle-moi lundi à 14h de déposer le colis": local(2026, 11, 16, 14),
            "rappelle-moi lundi à 8h de déposer le colis": local(2026, 11, 23, 8),
            "remind me on December 24 at 6pm to call grandma": local(2026, 12, 24, 18),
            "rappelle moi le 25 décembre à 9h d'appeler mamie": local(2026, 12, 25, 9),
            "rappelle-moi le 3 janvier à 10h de faire la déclaration": local(
                2027, 1, 3, 10
            ),
            "rappelle-moi le 24/12 à 20 h de mettre les cadeaux": local(
                2026, 12, 24, 20
            ),
            "erinnere mich am 24.12. um 20 Uhr an die Geschenke": local(
                2026, 12, 24, 20
            ),
            "remind me tonight at 9 to lock the door": local(2026, 11, 16, 21),
            "fais-moi penser à acheter du pain ce soir à 19h": local(2026, 11, 16, 19),
            "rappelle-moi demain matin à 7 h 45 de prendre le train": local(
                2026, 11, 17, 7, 45
            ),
            "remind me the day after tomorrow at noon to water the plants": local(
                2026, 11, 18, 12
            ),
            "rappelle-moi à minuit de couper le four": local(2026, 11, 17, 0),
            "ricordami domani alle 9 e mezza di chiamare": local(2026, 11, 17, 9, 30),
            "recuérdame mañana a las 9 y cuarto llamar": local(2026, 11, 17, 9, 15),
            "remind me at half past 6 to feed the cat": local(2026, 11, 16, 18, 30),
            "recuérdame mañana por la mañana a las 7 ir al gimnasio": local(
                2026, 11, 17, 7
            ),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                start = plan(text).start
                self.assertEqual(start, expected)
                self.assertEqual(start.utcoffset(), expected.utcoffset())

    def test_zone_is_the_persons(self) -> None:
        start = plan("remind me tomorrow at 9 to call", zone="America/New_York").start
        self.assertEqual(start, local(2026, 11, 17, 9, zone="America/New_York"))
        # 10:00 in Paris is 04:00 in New York: 9 a.m. is still ahead today.
        start = plan("remind me at 9 to call", zone="America/New_York").start
        self.assertEqual(start, local(2026, 11, 16, 9, zone="America/New_York"))

    def test_relative_dates_across_daylight_saving(self) -> None:
        # Saturday 28 March 2026, 20:00 in Paris; clocks go forward that night.
        before = datetime(2026, 3, 28, 19, 0, tzinfo=timezone.utc)
        tomorrow = plan("rappelle-moi demain à 9 h de courir", now=before).start
        self.assertEqual(tomorrow, local(2026, 3, 29, 9))
        self.assertEqual(tomorrow.utcoffset(), timedelta(hours=2))
        # 24 elapsed hours land at 21:00 on the clock after the change.
        later = plan("rappelle-moi dans 24 heures de courir", now=before).start
        self.assertEqual(later - before, timedelta(hours=24))
        self.assertEqual((later.hour, later.utcoffset()), (21, timedelta(hours=2)))
        # Calendar days keep the wall-clock time.
        days = plan("rappelle-moi dans 2 jours à 20 h de courir", now=before).start
        self.assertEqual((days.date(), days.hour), (date(2026, 3, 30), 20))
        # 2:30 does not exist that night: the reminder moves to 3:30.
        skipped = plan("rappelle-moi demain à 2h30 de courir", now=before).start
        self.assertEqual((skipped.hour, skipped.minute), (3, 30))
        # New York leaves daylight saving on Sunday 1 November 2026.
        new_york = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)
        start = plan(
            "remind me tomorrow at 9am to run", now=new_york, zone="America/New_York"
        ).start
        self.assertEqual(start.utcoffset(), timedelta(hours=-5))
        self.assertEqual(start.hour, 9)
        later = plan(
            "remind me in 24 hours to run", now=new_york, zone="America/New_York"
        ).start
        self.assertEqual(later.hour, 11)

    def test_typos_in_every_language(self) -> None:
        cases = {
            "erinere mich morgn um 9 an den Zahnarzt": local(2026, 11, 17, 9),
            "recuedame mañana a las 10 pagar la luz": local(2026, 11, 17, 10),
            "ricordami doamni alle 9 di chiamare Bea": local(2026, 11, 17, 9),
            "rappelle-moi mecredi a 15h d'appeler l'école": local(2026, 11, 18, 15),
            "remind me on wednsday at 4pm to pick up the parcel": local(
                2026, 11, 18, 16
            ),
            "Erinnere mich am Donerstag um 7:30 an das Meeting": local(
                2026, 11, 19, 7, 30
            ),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(plan(text).start, expected)

    def test_recurrences(self) -> None:
        cases = {
            "Rappelle-moi tous les lundis à 8 h de sortir les poubelles": (
                "WEEKLY",
                (0,),
                local(2026, 11, 23, 8),
            ),
            "remind me every weekday at 7:30 am to stretch": (
                "WEEKLY",
                (0, 1, 2, 3, 4),
                local(2026, 11, 17, 7, 30),
            ),
            "erinnere mich jeden Tag um 20 Uhr an die Tabletten": (
                "DAILY",
                (),
                local(2026, 11, 16, 20),
            ),
            "recuérdame todos los martes y jueves a las 19 ir a natación": (
                "WEEKLY",
                (1, 3),
                local(2026, 11, 17, 19),
            ),
            "ricordami ogni lunedì alle 8 di pagare l'affitto": (
                "WEEKLY",
                (0,),
                local(2026, 11, 23, 8),
            ),
            "erinnere mich montags und donnerstags um 7 Uhr an den Sport": (
                "WEEKLY",
                (0, 3),
                local(2026, 11, 19, 7),
            ),
            "remind me every morning at 7 to take my vitamins": (
                "DAILY",
                (),
                local(2026, 11, 17, 7),
            ),
        }
        for text, (freq, days, first) in cases.items():
            with self.subTest(text=text):
                result = plan(text)
                assert result.recurrence is not None
                self.assertEqual(result.recurrence.freq, freq)
                self.assertEqual(result.recurrence.byday, days)
                self.assertEqual(result.start, first)
        monthly = plan("rappelle-moi le 5 de chaque mois à 9 h de payer le loyer")
        self.assertEqual(monthly.recurrence, Recurrence("MONTHLY", bymonthday=5))
        self.assertEqual(monthly.start, local(2026, 12, 5, 9))
        hourly = plan("rappelle-moi toutes les 2 heures de boire de l'eau")
        self.assertEqual(hourly.recurrence, Recurrence("HOURLY", 2))
        self.assertEqual(hourly.start, local(2026, 11, 16, 12))
        yearly = plan(
            "remind me every year on May 3 at 9am to wish Aria a happy birthday"
        )
        self.assertEqual(yearly.recurrence, Recurrence("YEARLY"))
        self.assertEqual(yearly.start, local(2027, 5, 3, 9))

    def test_early_alarm(self) -> None:
        cases = {
            "rappelle moi demain à 10h le rdv chez le dentiste, 15 minutes avant": timedelta(
                minutes=15
            ),
            "remind me friday at 3pm about the meeting, an hour before": timedelta(
                hours=1
            ),
            "erinnere mich morgen um 9 an den Termin, 30 Minuten vorher": timedelta(
                minutes=30
            ),
            "recuérdame mañana a las 9 la cita, 10 minutos antes": timedelta(
                minutes=10
            ),
            "ricordami domani alle 9 della riunione, un quarto d'ora prima": timedelta(
                minutes=15
            ),
            "rappelle-moi le 25 décembre à 9h d'appeler mamie, et la veille aussi": timedelta(
                days=1
            ),
        }
        for text, early in cases.items():
            with self.subTest(text=text):
                result = plan(text)
                self.assertEqual(result.early, early)
        self.assertEqual(plan(next(iter(cases))).title, "Rdv chez le dentiste")
        self.assertIsNone(plan("remind me tomorrow at 9 to call").early)


class QuestionTests(unittest.TestCase):
    def outcome(self, text: str, now: datetime = NOW) -> Plan | Question:
        spec = understand(text)
        assert spec is not None, text
        return resolve(spec, now, PARIS)

    def test_missing_or_unclear_time_is_asked(self) -> None:
        cases = {
            "rappelle-moi d'appeler le plombier": "when",
            "remind me to call the plumber": "when",
            "rappelle-moi ce soir d'appeler maman": "time",
            "remind me tomorrow to call the bank": "time",
            "erinnere mich morgen früh an den Arzt": "time",
            "recuérdame el lunes llamar a Inés": "time",
            "ricordami stasera di chiamare Nadia": "time",
            "rappelle-moi aujourd'hui à 8h de partir": "past",
            "remind me on 3/4 at 9 to pay": "date",
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                result = self.outcome(text)
                self.assertIsInstance(result, Question)
                assert isinstance(result, Question)
                self.assertEqual(result.kind, kind)

    def test_the_answer_completes_the_request(self) -> None:
        first = "rappelle-moi ce soir d'appeler maman"
        asked = question("time", "fr")
        spec = understand("à 20 h 30", asked, first)
        assert spec is not None
        result = resolve(spec, NOW, PARIS)
        assert isinstance(result, Plan)
        self.assertEqual(result.start, local(2026, 11, 16, 20, 30))
        self.assertEqual(result.title, "Appeler maman")
        spec = understand(
            "tomorrow at 9", question("when", "en"), "remind me to call the bank"
        )
        assert spec is not None
        result = resolve(spec, NOW, PARIS)
        assert isinstance(result, Plan)
        self.assertEqual(
            (result.start, result.title), (local(2026, 11, 17, 9), "Call the bank")
        )
        # Another subject after the question is not taken as its answer.
        self.assertIsNone(
            understand(
                "what is the capital of Peru?",
                question("when", "en"),
                "remind me to call the bank",
            )
        )
        self.assertIsNone(understand("à 20 h 30", "Bonjour !", first))


class CalendarFileTests(unittest.TestCase):
    def test_parsed_back(self) -> None:
        result = plan(
            "rappelle moi demain à 10h le rdv chez le dentiste, 15 minutes avant"
        )
        content = calendar_file(
            result.title, result.start, result.zone, early=result.early, now=NOW
        )
        self.assertTrue(content.endswith(b"\r\n"))
        self.assertTrue(all(len(line) <= 75 for line in content.split(b"\r\n")))
        calendar = icalendar.Calendar.from_ical(content)
        self.assertEqual(str(calendar["VERSION"]), "2.0")
        item = event(content)
        self.assertEqual(str(item["SUMMARY"]), "Rdv chez le dentiste")
        start = item.decoded("DTSTART")
        self.assertEqual(start, local(2026, 11, 17, 10))
        self.assertEqual(item["DTSTART"].params["TZID"], PARIS)
        alarms = [component for component in item.walk("VALARM")]
        self.assertEqual([str(a["ACTION"]) for a in alarms], ["DISPLAY", "DISPLAY"])
        self.assertEqual(
            [a.decoded("TRIGGER") for a in alarms],
            [timedelta(0), timedelta(minutes=-15)],
        )
        self.assertIn(b"BEGIN:VTIMEZONE", content)

    def test_recurring_rule_keeps_the_wall_clock_across_daylight_saving(self) -> None:
        before = datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc)
        result = plan(
            "Rappelle-moi tous les lundis à 8 h de sortir les poubelles", now=before
        )
        assert result.recurrence is not None
        content = calendar_file(
            result.title,
            result.start,
            result.zone,
            recurrence=result.recurrence,
            now=before,
        )
        item = event(content)
        raw = item["RRULE"].to_ical()
        rule = raw.decode() if isinstance(raw, bytes) else str(raw)
        self.assertEqual(rule, "FREQ=WEEKLY;BYDAY=MO")
        occurrences = rrulestr(rule, dtstart=item.decoded("DTSTART"))
        first = [occurrences[index] for index in range(3)]
        self.assertEqual(
            [o.date() for o in first],
            [date(2026, 3, 23), date(2026, 3, 30), date(2026, 4, 6)],
        )
        self.assertEqual({o.hour for o in first}, {8})
        self.assertEqual(
            [o.utcoffset() for o in first],
            [timedelta(hours=1), timedelta(hours=2), timedelta(hours=2)],
        )

    def test_vtimezone_matches_the_zone_database(self) -> None:
        for zone_name in (
            PARIS,
            "America/New_York",
            "Australia/Sydney",
            "Asia/Tokyo",
            "Europe/London",
        ):
            with self.subTest(zone=zone_name):
                start = datetime(2026, 11, 17, 9, tzinfo=ZoneInfo(zone_name))
                content = calendar_file("Test", start, zone_name, now=NOW)
                calendar = icalendar.Calendar.from_ical(content)
                zone_component: Any = next(iter(calendar.walk("VTIMEZONE")))
                described = zone_component.to_tz()
                reference = ZoneInfo(zone_name)
                moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
                while moment < datetime(2036, 1, 1, tzinfo=timezone.utc):
                    self.assertEqual(
                        moment.astimezone(described).utcoffset(),
                        moment.astimezone(reference).utcoffset(),
                        moment,
                    )
                    moment += timedelta(days=5, hours=7)

    def test_europe_uses_yearly_rules(self) -> None:
        lines = vtimezone(PARIS, local(2026, 11, 17, 9))
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", lines)
        self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", lines)

    def test_text_is_escaped_and_folded(self) -> None:
        title = "Acheter: lait, œufs; farine \\ sucre — " + "é" * 60
        content = calendar_file(title, local(2026, 11, 17, 9), PARIS, now=NOW)
        self.assertIn(
            escape_text("lait, œufs; farine").encode(), content.replace(b"\r\n ", b"")
        )
        self.assertTrue(all(len(line) <= 75 for line in content.split(b"\r\n")))
        self.assertEqual(str(event(content)["SUMMARY"]), title)

    def test_every_language_produces_a_valid_file(self) -> None:
        for text in RoutingTests.REQUESTS:
            with self.subTest(text=text):
                result = plan(text)
                content = calendar_file(
                    result.title or "Reminder",
                    result.start,
                    result.zone,
                    early=result.early,
                    recurrence=result.recurrence,
                    now=NOW,
                )
                item = event(content)
                self.assertEqual(item.decoded("DTSTART"), result.start)
                self.assertTrue(list(item.walk("VALARM")))


def request(
    text: str, history: list[dict[str, str]] | None = None, zone: str = PARIS
) -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "messages": [*(history or []), {"role": "user", "content": text}],
            "ui_timezone": zone,
        }
    )


class TurnTests(unittest.TestCase):
    def run_turn(
        self, value: ChatRequest, publish: AsyncMock
    ) -> tuple[bool, Interaction]:
        item = Interaction()
        with patch("services.orchestrator.reminder_route.publish", publish):
            handled = asyncio.run(reminder_turn(value, item, now=NOW))
        return handled, item

    def test_answer_is_one_sentence_and_the_file(self) -> None:
        url = "/api/v1/terminals/atlas-files/files/serve/home/user/requests/chatcmpl-0/x.ics"
        publish = AsyncMock(return_value=url)
        handled, item = self.run_turn(
            request("rappelle-moi à 16 h 15 d'aller chercher les filles à l'école"),
            publish,
        )
        self.assertTrue(handled)
        assert publish.await_args is not None
        content, name, request_id = publish.await_args.args[:3]
        self.assertEqual(name, "aller-chercher-les-filles-a-l-ecole.ics")
        self.assertEqual(request_id, item.request_id)
        self.assertEqual(event(content).decoded("DTSTART"), local(2026, 11, 16, 16, 15))
        self.assertEqual(
            item.reponse,
            "Rappel « Aller chercher les filles à l'école » prévu aujourd’hui, lundi 16 "
            "novembre, à 16 h 15 : ouvrez ce fichier pour l’ajouter à votre agenda.\n\n"
            f"[{name}]({url})",
        )
        self.assertEqual(
            (item.state, item.task_type, item.cout_eur), ("done", "reminder", 0.0)
        )
        self.assertEqual(item.files, [url])

    def test_each_language_answers_in_its_own(self) -> None:
        publish = AsyncMock(
            return_value="/api/v1/terminals/atlas-files/files/serve/x.ics"
        )
        expected = {
            "remind me tomorrow at 9 to call the plumber": "Reminder “Call the plumber” set for tomorrow, Tuesday 17 November, at 9:00 AM",
            "erinnere mich Freitag um 18 Uhr, den Müll rauszubringen": "Erinnerung „Müll rauszubringen“ für Freitag, 20. November, um 18:00 Uhr",
            "recuérdame mañana a las 9 llamar al fontanero": "Recordatorio «Llamar al fontanero» para mañana, martes 17 de noviembre, a las 9:00",
            "ricordami domani alle 9 di chiamare l'idraulico": "Promemoria «Chiamare l'idraulico» per domani, martedì 17 novembre, alle 9:00",
        }
        for text, start in expected.items():
            with self.subTest(text=text):
                handled, item = self.run_turn(request(text), publish)
                self.assertTrue(handled)
                self.assertTrue(item.reponse.startswith(start), item.reponse)

    def test_reminder_without_a_text(self) -> None:
        publish = AsyncMock(
            return_value="/api/v1/terminals/atlas-files/files/serve/x.ics"
        )
        _, item = self.run_turn(request("remind me in 10 minutes"), publish)
        self.assertTrue(
            item.reponse.startswith(
                "Reminder set for today, Monday 16 November, at 10:10 AM:"
            ),
            item.reponse,
        )
        assert publish.await_args is not None
        self.assertEqual(str(event(publish.await_args.args[0])["SUMMARY"]), "Reminder")
        self.assertEqual(publish.await_args.args[1], "reminder.ics")

    def test_assumed_zone_is_named(self) -> None:
        publish = AsyncMock(
            return_value="/api/v1/terminals/atlas-files/files/serve/x.ics"
        )
        _, item = self.run_turn(
            request("remind me tomorrow at 9 to call", zone="UTC"), publish
        )
        self.assertIn("(Europe/Paris)", item.reponse)

    def test_question_then_answer(self) -> None:
        publish = AsyncMock(
            return_value="/api/v1/terminals/atlas-files/files/serve/x.ics"
        )
        first = "rappelle-moi ce soir d'appeler maman"
        handled, item = self.run_turn(request(first), publish)
        self.assertTrue(handled)
        self.assertEqual(item.reponse, question("time", "fr"))
        publish.assert_not_awaited()
        history = [
            {"role": "user", "content": first},
            {"role": "assistant", "content": item.reponse},
        ]
        handled, item = self.run_turn(request("vers 20h30", history), publish)
        self.assertTrue(handled)
        self.assertIn("à 20 h 30", item.reponse)
        assert publish.await_args is not None
        self.assertEqual(
            event(publish.await_args.args[0]).decoded("DTSTART"),
            local(2026, 11, 16, 20, 30),
        )

    def test_file_is_served_by_the_download_route(self) -> None:
        from services.orchestrator.terminal_stack import DOWNLOAD

        class Terminal:
            def __init__(self, request_id: str) -> None:
                self.root = "/home/user/requests/" + request_id

            async def initialize(self) -> None:
                return None

            async def upload_generated(
                self, name: str, content: bytes, timeout: float, *, max_bytes: int
            ) -> str:
                return self.root + "/" + name

        item = Interaction()
        with patch("services.orchestrator.terminal_client.TerminalClient", Terminal):
            url = asyncio.run(
                publish(
                    b"BEGIN:VCALENDAR",
                    file_name("Rendez-vous à 9 h", "fr"),
                    item.request_id,
                    5,
                )
            )
        self.assertIsNotNone(DOWNLOAD.fullmatch(url), url)

    def test_failure_is_said(self) -> None:
        publish = AsyncMock(side_effect=RuntimeError("terminal_unavailable"))
        handled, item = self.run_turn(
            request("remind me tomorrow at 9 to call"), publish
        )
        self.assertTrue(handled)
        self.assertEqual(item.reponse, FAILURE["en"])
        self.assertNotIn("](", item.reponse)
        self.assertEqual(item.files, [])

    def test_other_turns_are_left_alone(self) -> None:
        publish = AsyncMock()
        for text in RoutingTests.OTHERS:
            with self.subTest(text=text):
                handled, item = self.run_turn(request(text), publish)
                self.assertFalse(handled)
                self.assertEqual(item.reponse, "")
        publish.assert_not_awaited()

    def test_strict_privacy_level(self) -> None:
        # No network: every outbound primitive is a trap; the file goes to the
        # request's own user-file area (publish, patched here).
        publish = AsyncMock(
            return_value="/api/v1/terminals/atlas-files/files/serve/x.ics"
        )
        trap = AsyncMock(side_effect=AssertionError("network used"))
        token = privacy.elevate(privacy.STRICT)
        try:
            self.assertTrue(privacy.is_strict())
            with (
                patch("aiohttp.ClientSession", trap),
                patch("urllib.request.urlopen", trap),
            ):
                handled, item = self.run_turn(
                    request("remind me tomorrow at 9 to call"), publish
                )
        finally:
            privacy.reset(token)
        self.assertTrue(handled)
        self.assertTrue(re.search(r"\]\(/api/v1/terminals/", item.reponse))
        trap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
