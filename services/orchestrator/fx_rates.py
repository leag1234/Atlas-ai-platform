"""Currency conversion with the European Central Bank euro reference rates.

The ECB publishes one rate per currency against the euro, around 16:00 Central
European Time on TARGET working days. The daily XML feed is public and free; it
is fetched once, kept for the Paris calendar day (refreshed after publication
time), and never replaced by an estimate: an unreachable or malformed feed is a
precise error. Cross rates go through the euro, as the ECB recommends.
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal, DecimalException, localcontext
from zoneinfo import ZoneInfo

import aiohttp

from services.orchestrator.web import USER_AGENT, http_timeout

FEED_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
SOURCE = "European Central Bank, euro foreign exchange reference rates"
MAX_FEED_BYTES = 64_000
FEED_TIMEOUT = 10.0
PARIS = ZoneInfo("Europe/Paris")
# The ECB publishes around 16:00 CET; a copy fetched before this time is
# refreshed once it has passed.
PUBLICATION = time(16, 15)
NAMESPACE = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"
# ISO 4217 minor units that are not 2, among the currencies the ECB quotes.
MINOR_UNITS = {"JPY": 0, "KRW": 0, "ISK": 0}
CODE = re.compile(r"[A-Z]{3}")


class RatesError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class Rates:
    day: date
    per_euro: dict[str, Decimal]
    fetched_at: datetime


Fetcher = Callable[[float], Awaitable[bytes]]


def parse(body: bytes) -> tuple[date, dict[str, Decimal]]:
    """The feed's rate date and rates per euro; anything unexpected is refused."""
    if len(body) > MAX_FEED_BYTES:
        raise RatesError("exchange_rates_invalid", "feed larger than expected")
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise RatesError("exchange_rates_invalid", "feed declares a document type")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise RatesError(
            "exchange_rates_invalid", "feed is not well-formed XML"
        ) from None
    days = [cube for cube in root.iter(f"{NAMESPACE}Cube") if cube.get("time")]
    if len(days) != 1:
        raise RatesError("exchange_rates_invalid", "feed does not hold one rate date")
    try:
        day = date.fromisoformat(str(days[0].get("time")))
        rates = {
            str(cube.get("currency")): Decimal(str(cube.get("rate")))
            for cube in days[0]
            if cube.get("currency")
        }
    except (ValueError, DecimalException):
        raise RatesError("exchange_rates_invalid", "unreadable rate in feed") from None
    if not rates or any(
        not CODE.fullmatch(code) or not value.is_finite() or value <= 0
        for code, value in rates.items()
    ):
        raise RatesError(
            "exchange_rates_invalid", "unexpected currency or rate in feed"
        )
    return day, {"EUR": Decimal(1), **rates}


async def fetch_feed(timeout: float) -> bytes:
    limit = min(timeout, FEED_TIMEOUT)
    async with aiohttp.ClientSession(
        trust_env=False,
        headers={"User-Agent": USER_AGENT},
        timeout=http_timeout(limit, FEED_TIMEOUT),
    ) as session:
        async with session.get(FEED_URL, allow_redirects=False) as response:
            if response.status != 200:
                raise RatesError(
                    "exchange_rates_unavailable",
                    f"the ECB feed answered HTTP {response.status}; no rate is invented",
                )
            body = await response.content.read(MAX_FEED_BYTES + 1)
    return body


class RateBook:
    """One process-wide copy of the day's feed."""

    def __init__(
        self,
        fetcher: Fetcher = fetch_feed,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.fetcher, self.clock = fetcher, clock
        self.current: Rates | None = None
        self.lock = asyncio.Lock()

    def fresh(self, now: datetime) -> bool:
        if self.current is None:
            return False
        fetched = self.current.fetched_at.astimezone(PARIS)
        local = now.astimezone(PARIS)
        if fetched.date() != local.date():
            return False
        return fetched.time() >= PUBLICATION or local.time() < PUBLICATION

    async def rates(self, timeout: float) -> Rates:
        async with self.lock:
            now = self.clock()
            if self.current is not None and self.fresh(now):
                return self.current
            try:
                body = await self.fetcher(timeout)
            except RatesError:
                raise
            except (aiohttp.ClientError, TimeoutError, OSError):
                raise RatesError(
                    "exchange_rates_unavailable",
                    "the ECB reference rate feed could not be reached; no rate is invented",
                ) from None
            day, per_euro = parse(body)
            self.current = Rates(day, per_euro, now)
            return self.current


BOOK = RateBook()


def _shown(value: Decimal, places: int) -> str:
    quantum = Decimal(1).scaleb(-places)
    return format(value.quantize(quantum, rounding=ROUND_HALF_UP), "f")


def convert(
    amount: Decimal, source: str, target: str, rates: Rates
) -> dict[str, object]:
    first, second = source.strip().upper(), target.strip().upper()
    for code in (first, second):
        if code not in rates.per_euro:
            raise RatesError(
                "unsupported_currency",
                f"{code[:8]} has no ECB reference rate; quoted: "
                + ", ".join(sorted(rates.per_euro)),
            )
    with localcontext() as context:
        context.prec = 40
        rate = rates.per_euro[second] / rates.per_euro[first]
        result = amount * rate
    places = MINOR_UNITS.get(second, 2)
    return {
        "amount": format(amount.normalize(), "f"),
        "from_currency": first,
        "to_currency": second,
        "result": _shown(result, places),
        "unrounded_result": _shown(result, 6),
        "rate": f"1 {first} = {_shown(rate, 6)} {second}",
        "via_euro": "EUR" not in {first, second},
        "rate_date": rates.day.isoformat(),
        "source": SOURCE,
        "source_url": FEED_URL,
        "fetched_at": rates.fetched_at.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "note": "ECB reference rates are published for information on working "
        "days around 16:00 CET; banks and card issuers apply their own rates and fees",
    }
