"""T8: dates, times, units and currencies are computed by code, never estimated."""

import importlib.util
import json
import tempfile
import unittest
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import aiohttp

from services.orchestrator import datecalc, exact_tools, fx_rates, units
from services.orchestrator.chat_pipeline import ChatTools
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
 xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
 <gesmes:subject>Reference rates</gesmes:subject>
 <Cube><Cube time='2026-11-13'>
  <Cube currency='USD' rate='1.1052'/>
  <Cube currency='JPY' rate='176.83'/>
  <Cube currency='GBP' rate='0.83559'/>
  <Cube currency='CHF' rate='0.9321'/>
 </Cube></Cube>
</gesmes:Envelope>"""


def clock(text: str) -> Callable[[], datetime]:
    moment = datetime.fromisoformat(text).astimezone(timezone.utc)
    return lambda: moment


def run(clock_text: str = "2026-11-14T10:00:00+00:00", **arguments: Any) -> Any:
    request = exact_tools.DateTime.model_validate(arguments).model_dump()
    return datecalc.run(request, clock(clock_text))


# The shared deploy runtime may lack the calendar until it is installed there:
# those checks then skip at deploy time (CI installs requirements-dev.txt).
CALENDAR = importlib.util.find_spec("holidays") is not None


class DateTests(unittest.TestCase):
    def test_difference_weekday_and_iso_week(self) -> None:
        span = run(operation="difference", date="2026-03-03", end_date="2026-11-13")
        self.assertEqual(span["days"], 255)
        self.assertEqual(span["calendar"], {"years": 0, "months": 8, "days": 10})
        self.assertEqual(span["weeks"], 36)
        self.assertEqual(span["remaining_days_after_weeks"], 3)
        backwards = run(
            operation="difference", date="2026-11-13", end_date="2026-03-03"
        )
        self.assertEqual(backwards["days"], -255)
        self.assertEqual(backwards["direction"], "end is before start")
        day = run(operation="day_info", date="2031-07-14")
        self.assertEqual(day["weekday"], "Monday")
        # 1 January 2027 still belongs to ISO week 53 of 2026.
        self.assertEqual(
            run(operation="day_info", date="2027-01-01")["iso_week"], "2026-W53"
        )

    def test_leap_years_and_month_ends(self) -> None:
        self.assertTrue(run(operation="day_info", date="2028-02-29")["leap_year"])
        with self.assertRaisesRegex(datecalc.DateError, "invalid_date"):
            run(operation="day_info", date="2027-02-29")
        clamped = run(operation="add", date="2024-02-29", amount=1, unit="years")
        self.assertEqual(clamped["result"], "2025-02-28")
        self.assertIn("note", clamped)
        self.assertEqual(
            run(operation="add", date="2028-01-31", amount=1, unit="months")["result"],
            "2028-02-29",
        )
        self.assertEqual(
            run(operation="add", date="2026-11-14", amount=-90, unit="days")["result"],
            "2026-08-16",
        )
        self.assertEqual(
            run(operation="difference", date="2024-02-29", end_date="2025-03-01")[
                "calendar"
            ],
            {"years": 1, "months": 0, "days": 1},
        )

    def test_age(self) -> None:
        age = run(operation="age", date="2008-02-29", end_date="2026-02-28")
        self.assertEqual(age["age_years"], 18)
        self.assertIn("28 February", age["note"])
        today = run(operation="age", date="1990-11-15")
        self.assertEqual(today["age_years"], 35)
        self.assertEqual(today["days_until_next_birthday"], 1)
        with self.assertRaisesRegex(datecalc.DateError, "birth_after_date"):
            run(operation="age", date="2030-01-01")

    @unittest.skipUnless(CALENDAR, "holidays is not installed")
    def test_business_days_with_french_and_regional_holidays(self) -> None:
        holiday = run(operation="day_info", date="2031-07-14")["public_holiday"]
        self.assertEqual(holiday, "Fête nationale")
        may = run(operation="business_days", date="2027-05-01", end_date="2027-05-31")
        self.assertEqual(may["business_days"], 19)
        self.assertEqual(
            [row["date"] for row in may["public_holidays_excluded"]],
            ["2027-05-06", "2027-05-17"],
        )
        march = {"operation": "business_days", "date": "2027-03-01"}
        self.assertEqual(run(**march, end_date="2027-03-31")["business_days"], 22)
        moselle = run(**march, end_date="2027-03-31", subdivision="57")
        self.assertEqual(moselle["business_days"], 21)
        saturdays = run(**march, end_date="2027-03-31", saturdays=True)
        self.assertEqual(saturdays["business_days"], 26)
        later = run(operation="add", date="2027-05-05", amount=3, unit="business_days")
        self.assertEqual(later["result"], "2027-05-11")
        earlier = run(
            operation="add", date="2027-05-18", amount=-1, unit="business_days"
        )
        self.assertEqual(earlier["result"], "2027-05-14")
        self.assertEqual(
            len(run(operation="holidays", year=2027)["public_holidays"]), 11
        )
        german = run(operation="day_info", date="2027-10-03", country="DE")
        self.assertIsNotNone(german["public_holiday"])
        with self.assertRaisesRegex(datecalc.DateError, "unknown_country"):
            run(operation="holidays", year=2027, country="QQ")
        with self.assertRaisesRegex(datecalc.DateError, "unknown_subdivision"):
            run(operation="holidays", year=2027, subdivision="99")
        with self.assertRaisesRegex(datecalc.DateError, "range_too_long"):
            run(operation="business_days", date="1900-01-01", end_date="2100-01-01")

    def test_time_zones_and_daylight_saving(self) -> None:
        now = run("2026-07-01T12:00:00+00:00", operation="now")
        self.assertEqual(now["time"], "14:00")
        self.assertEqual(now["zone"], "Europe/Paris")
        self.assertTrue(now["daylight_saving_time"])
        self.assertEqual(
            run(operation="now", zone="tokyo")["local"], "2026-11-14T19:00"
        )
        winter = run(
            operation="convert_time",
            date="2026-11-14T15:00",
            zone="Europe/Paris",
            to_zone="America/New_York",
        )
        self.assertEqual(winter["to"]["local"], "2026-11-14T09:00")
        # The United States moves its clocks two weeks before Europe.
        gap_weeks = run(
            operation="convert_time",
            date="2027-03-20T15:00",
            zone="Europe/Paris",
            to_zone="America/New_York",
        )
        self.assertEqual(gap_weeks["to"]["local"], "2027-03-20T10:00")
        late = run(
            operation="convert_time",
            date="2026-11-14T23:30",
            zone="Europe/Paris",
            to_zone="Asia/Tokyo",
        )
        self.assertEqual(late["day_change"], 1)
        with self.assertRaisesRegex(datecalc.DateError, "nonexistent_local_time"):
            run(
                operation="convert_time",
                date="2027-03-28T02:30",
                zone="Europe/Paris",
                to_zone="UTC",
            )
        twice = run(
            operation="convert_time",
            date="2027-10-31T02:30",
            zone="Europe/Paris",
            to_zone="UTC",
        )
        self.assertTrue(twice["ambiguous"])
        self.assertEqual(
            [reading["utc"] for reading in twice["readings"]],
            ["2027-10-31T00:30+00:00", "2027-10-31T01:30+00:00"],
        )
        hour = run(
            operation="add",
            date="2027-03-28T01:30",
            amount=1,
            unit="hours",
            zone="Europe/Paris",
        )
        self.assertEqual(hour["result"]["local"], "2027-03-28T03:30")
        with self.assertRaisesRegex(datecalc.DateError, "unknown_time_zone"):
            run(operation="now", zone="Mars/Olympus")

    def test_home_zone_is_used_without_a_zone(self) -> None:
        request = exact_tools.DateTime.model_validate({"operation": "now"}).model_dump()
        found = datecalc.run(
            request, clock("2026-11-14T10:00:00+00:00"), "America/Montreal"
        )
        self.assertEqual(found["time"], "05:00")


class UnitTests(unittest.TestCase):
    def convert(self, value: str, source: str, target: str) -> str:
        return str(units.convert(Decimal(value), source, target)["result"])

    def test_exact_conversions(self) -> None:
        self.assertEqual(self.convert("100", "km", "miles"), "62.1371192237")
        self.assertEqual(self.convert("1", "cup", "fl oz"), "8")
        self.assertEqual(self.convert("1", "mile", "m"), "1609.344")
        self.assertEqual(self.convert("2", "cuillères à soupe", "ml"), "30")
        self.assertEqual(self.convert("1", "GiB", "Go"), "1.073741824")
        self.assertEqual(self.convert("8", "Mb", "MB"), "1")
        self.assertEqual(self.convert("1", "B", "b"), "8")
        self.assertEqual(self.convert("90", "min", "h"), "1.5")
        self.assertEqual(self.convert("1", "hp", "W"), "745.699871582")
        self.assertEqual(self.convert("1", "psi", "Pa"), "6894.75729317")
        self.assertEqual(self.convert("1", "acre", "m²"), "4046.8564224")
        self.assertEqual(self.convert("1", "kWh", "MJ"), "3.6")

    def test_temperature_and_fuel(self) -> None:
        self.assertEqual(self.convert("-40", "°C", "°F"), "-40")
        self.assertEqual(self.convert("350", "degF", "celsius"), "176.666666667")
        self.assertEqual(self.convert("0", "K", "C"), "-273.15")
        self.assertEqual(self.convert("5", "L/100 km", "mpg"), "47.0429166667")
        self.assertEqual(self.convert("50", "mpg", "l/100km"), "4.70429166667")
        with self.assertRaisesRegex(units.UnitError, "below_absolute_zero"):
            units.convert(Decimal("-500"), "fahrenheit", "celsius")

    def test_refusals_are_precise(self) -> None:
        with self.assertRaisesRegex(
            units.UnitError, "incompatible_units: .*mass.*length"
        ):
            units.convert(Decimal(1), "kg", "km")
        with self.assertRaisesRegex(units.UnitError, "ambiguous_unit"):
            units.convert(Decimal(1), "ton", "kg")
        with self.assertRaisesRegex(units.UnitError, "unknown_unit"):
            units.convert(Decimal(1), "furlongs per fortnight", "km/h")
        pound = units.convert(Decimal(1), "livre", "g")
        self.assertEqual(pound["result"], "453.59237")
        self.assertIn("500 g", json.dumps(pound["notes"]))


class CurrencyTests(unittest.IsolatedAsyncioTestCase):
    def book(
        self, *bodies: bytes | Exception
    ) -> tuple[fx_rates.RateBook, list[float], dict[str, datetime]]:
        calls: list[float] = []
        queue = list(bodies)
        now = {"now": datetime.fromisoformat("2026-11-14T09:00:00+00:00")}

        async def fetch(timeout: float) -> bytes:
            calls.append(timeout)
            body = queue.pop(0)
            if isinstance(body, Exception):
                raise body
            return body

        return fx_rates.RateBook(fetch, lambda: now["now"]), calls, now

    async def test_conversion_states_rate_date_and_source(self) -> None:
        book, calls, _ = self.book(FEED)
        rates = await book.rates(5)
        found = fx_rates.convert(Decimal(100), "usd", "EUR", rates)
        self.assertEqual(found["result"], "90.48")
        self.assertEqual(found["rate_date"], "2026-11-13")
        self.assertEqual(found["source_url"], fx_rates.FEED_URL)
        self.assertFalse(found["via_euro"])
        cross = fx_rates.convert(Decimal(100), "GBP", "JPY", rates)
        self.assertEqual(cross["result"], "21162")
        self.assertTrue(cross["via_euro"])
        with self.assertRaisesRegex(fx_rates.RatesError, "unsupported_currency"):
            fx_rates.convert(Decimal(1), "EUR", "XAU", rates)
        await book.rates(5)
        self.assertEqual(len(calls), 1)

    async def test_day_cache_refreshes_after_publication(self) -> None:
        book, calls, now = self.book(FEED, FEED)
        await book.rates(5)
        now["now"] = datetime.fromisoformat("2026-11-14T15:30:00+00:00")
        await book.rates(5)
        self.assertEqual(len(calls), 2)
        now["now"] = datetime.fromisoformat("2026-11-14T20:00:00+00:00")
        await book.rates(5)
        self.assertEqual(len(calls), 2)

    async def test_unreachable_or_invalid_feed_is_an_error_never_a_rate(self) -> None:
        book, _, _ = self.book(aiohttp.ClientConnectionError("down"))
        with self.assertRaisesRegex(fx_rates.RatesError, "exchange_rates_unavailable"):
            await book.rates(5)
        for body in (
            b"<html>maintenance</html>",
            b"<!DOCTYPE x [<!ENTITY a 'b'>]><x/>",
            FEED.replace(b"1.1052", b"-1"),
        ):
            with self.subTest(body=body[:20]):
                with self.assertRaises(fx_rates.RatesError):
                    fx_rates.parse(body)
        with patch.object(fx_rates.BOOK, "fetcher", side_effect=TimeoutError):
            fx_rates.BOOK.current = None
            result = await exact_tools.execute(
                "currency_convert",
                '{"amount": 10, "from_currency": "EUR", "to_currency": "USD"}',
                5,
            )
        self.assertEqual(result["error"], "exchange_rates_unavailable")
        self.assertNotIn("data", result)

    async def test_chat_records_the_rate_feed_as_a_retrieved_source(self) -> None:
        book, _, _ = self.book(FEED)
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict("os.environ", {"ATLAS_WEB_CACHE": root + "/cache.sqlite"}),
            patch.object(fx_rates, "BOOK", book),
        ):
            item = Interaction()
            tools = ChatTools(item)
            result = await tools.execute(
                Call(
                    "1",
                    "currency_convert",
                    '{"amount": 250, "from_currency": "EUR", "to_currency": "CHF"}',
                ),
                5,
            )
        self.assertEqual(json.loads(json.dumps(result))["data"]["result"], "233.03")
        self.assertEqual(item.retrieved_urls, [fx_rates.FEED_URL])


class ToolContractTests(unittest.IsolatedAsyncioTestCase):
    def test_relevance_in_realistic_phrasings(self) -> None:
        cases = {
            "how many days are there between march 3 2026 and november 13 2026": {
                "date"
            },
            "le 14 juillet 2031 ça tombe quel jour de la semaine ?": {"date"},
            "combien de jours ouvrés entre le 1er mai et le 31 mai 2027 ?": {"date"},
            "jvoudrais savoir kel age j'aurai le 3 mars 2040 si chui né le 12/05/1987": {
                "date"
            },
            "Quelle heure est-il à Tokyo quand il est 15h à Paris ?": {"date"},
            "whats the time difference between london and sydney rn": {"date"},
            "dans 45 jours on sera quel jour ?": {"date"},
            "how many ounces in a cup": {"unit"},
            "convertis 350 degres fahrenheit en celsius stp": {"unit"},
            "5 L/100km ca fait combien en mpg": {"unit"},
            "100 km en miles": {"unit"},
            "1 tasse de farine = combien de grammes ?": {"unit"},
            "combien font 250 euros en francs suisses aujourd'hui ?": {"currency"},
            "convert 2,400 GBP to euros, use 1 GBP = 1.1834 EUR": {"currency"},
            "c'est quoi le taux de change dollar euro": {"currency"},
            "how much is 50 bucks in €": {"currency"},
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertTrue(expected <= exact_tools.relevant(question))
        for question in (
            "Bonjour, discutons en français.",
            "D'après la politique interne de télétravail, quels salariés sont concernés ?",
            "Calcula el IVA (21%) sobre una factura de 3.480,50 €.",
            "écris une fonction javascript qui trie un tableau d'objets par date",
            "Qui a gagné le dernier Tour de France ?",
            "make me a pomodoro timer",
            "Combien font 1 847 × 293, moins 12 % ?",
        ):
            with self.subTest(question=question):
                self.assertEqual(exact_tools.relevant(question), frozenset())
        self.assertEqual(
            exact_tools.relevant(
                "et en dollars ?", "combien font 250 euros en francs suisses"
            ),
            frozenset({"currency"}),
        )

    def test_declarations_live_in_code_and_never_change_other_turns(self) -> None:
        names = [
            tool["function"]["name"]  # type: ignore[index]
            for tool in exact_tools.declarations(
                frozenset({"date", "unit", "currency"})
            )
        ]
        self.assertEqual(names, ["date_time", "unit_convert", "currency_convert"])
        self.assertEqual(exact_tools.declarations(frozenset()), [])
        schema = exact_tools.DateTime.model_json_schema()
        self.assertEqual(schema["additionalProperties"], False)
        with open("prompts/tools.json") as handle:
            self.assertFalse(set(json.load(handle)) & set(exact_tools.SCHEMAS))

    async def test_invalid_arguments_return_the_schema(self) -> None:
        result = await exact_tools.execute("unit_convert", '{"value": "ten"}', 1)
        self.assertEqual(result["error"], "invalid_arguments")
        self.assertIn("schema", result)
        refused = await exact_tools.execute(
            "unit_convert", '{"value": 3, "from_unit": "kg", "to_unit": "km"}', 1
        )
        self.assertEqual(refused["error"], "incompatible_units")


if __name__ == "__main__":
    unittest.main()
