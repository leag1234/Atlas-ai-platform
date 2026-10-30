"""Private, atomic budget ledger. It deliberately contains no request text."""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
# Explicit provider settings make caps off unless an operator enables one.
PROVIDER_SEARCH_LIMITS = {
    "tavily": (
        "ATLAS_TAVILY_USER_DAILY_SEARCHES",
        "ATLAS_TAVILY_GLOBAL_DAILY_SEARCHES",
        "ATLAS_TAVILY_GLOBAL_MONTHLY_SEARCHES",
    ),
    "serpapi": (
        "ATLAS_SERPAPI_USER_DAILY_SEARCHES",
        "ATLAS_SERPAPI_GLOBAL_DAILY_SEARCHES",
        "ATLAS_SERPAPI_GLOBAL_MONTHLY_SEARCHES",
    ),
    "searxng": (
        "ATLAS_SEARXNG_USER_DAILY_SEARCHES",
        "ATLAS_SEARXNG_GLOBAL_DAILY_SEARCHES",
        "ATLAS_SEARXNG_GLOBAL_MONTHLY_SEARCHES",
    ),
}


def _limit(name: str) -> Decimal | int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value: Decimal | int = Decimal(raw) if name.endswith("EUR") else int(raw)
    except (ValueError, ArithmeticError):
        return None
    return value if value > 0 else None


def user_from_headers(headers: object) -> str:
    """Read v0.11.3 legacy Open WebUI forwarded identity headers.

    v0.11.3 sends X-OpenWebUI-User-{Id,Email,Name,Role} unless its optional
    JWT forwarding is configured. An unverifiable JWT must not select a bucket.
    """
    get = getattr(headers, "get")
    for name in ("X-OpenWebUI-User-Id", "X-OpenWebUI-User-Email"):
        value = get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return "unknown"


def _number(value: Decimal | int) -> str:
    return f"{Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def language_message(
    category: str, used: Decimal | int, limit: Decimal | int, lang: str
) -> str:
    monthly, search = "monthly" in category, "search" in category
    labels = {
        "fr": (
            ("Plafond mensuel atteint", "Il se remet à zéro le 1er du mois.")
            if monthly
            else (
                "Plafond quotidien atteint",
                "Il se remet à zéro à minuit (heure de Paris).",
            )
        ),
        "en": (
            ("Monthly limit reached", "It resets on the first day of the month.")
            if monthly
            else ("Daily limit reached", "It resets at midnight (Paris time).")
        ),
        "de": (
            ("Monatslimit erreicht", "Es wird am ersten Tag des Monats zurückgesetzt.")
            if monthly
            else (
                "Tageslimit erreicht",
                "Es wird um Mitternacht (Pariser Zeit) zurückgesetzt.",
            )
        ),
        "es": (
            ("Límite mensual alcanzado", "Se restablece el primer día del mes.")
            if monthly
            else (
                "Límite diario alcanzado",
                "Se restablece a medianoche (hora de París).",
            )
        ),
        "it": (
            ("Limite mensile raggiunto", "Si reimposta il primo giorno del mese.")
            if monthly
            else (
                "Limite giornaliero raggiunto",
                "Si reimposta a mezzanotte (ora di Parigi).",
            )
        ),
    }
    first, reset = labels.get(lang, labels["en"])
    unit = "searches" if search else "EUR"
    amount, ceiling = (
        (str(used), str(limit)) if search else (_number(used), _number(limit))
    )
    middle = {
        "fr": "utilisés sur",
        "en": "used of",
        "de": "verwendet von",
        "es": "utilizados de",
        "it": "utilizzati su",
    }.get(lang, "used of")
    return f"{first}: {amount} {unit} {middle} {ceiling} {unit}. {reset}"


def maintenance_message(lang: str) -> str:
    return {
        "fr": "Maintenance en cours.",
        "en": "Maintenance in progress.",
        "de": "Wartung läuft.",
        "es": "Mantenimiento en curso.",
        "it": "Manutenzione in corso.",
    }.get(lang, "Maintenance in progress.")


@dataclass
class BudgetRefusal(Exception):
    category: str
    used: Decimal | int
    limit: Decimal | int
    lang: str

    @property
    def message(self) -> str:
        return language_message(self.category, self.used, self.limit, self.lang)


@dataclass(frozen=True)
class CostReservation:
    user: str
    amount: Decimal
    day: str
    month: str
    enabled: bool


class DailyLedger:
    def __init__(self, path: Path | None = None, now: datetime | None = None) -> None:
        self.path = (
            path
            or Path(os.environ.get("ATLAS_DATA_DIR", "BRAIN")) / "daily-budget.sqlite"
        )
        self.now = now
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS budget_ledger (kind TEXT NOT NULL, period TEXT NOT NULL, user TEXT NOT NULL, reserved_micro INTEGER NOT NULL DEFAULT 0, measured_micro INTEGER NOT NULL DEFAULT 0, searches INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(kind, period, user))"
            )
        self.path.chmod(0o600)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5, isolation_level="IMMEDIATE")
        try:
            with db:
                yield db
        finally:
            db.close()

    def periods(self) -> tuple[str, str]:
        paris = (self.now or datetime.now(PARIS)).astimezone(PARIS)
        return paris.date().isoformat(), paris.strftime("%Y-%m")

    def day(self) -> str:
        return self.periods()[0]

    @staticmethod
    def _total(
        db: sqlite3.Connection, kind: str, period: str, user: str | None, column: str
    ) -> int:
        if user is None:
            return int(
                db.execute(
                    f"SELECT COALESCE(SUM({column}),0) FROM budget_ledger WHERE kind=? AND period=?",
                    (kind, period),
                ).fetchone()[0]
            )
        db.execute(
            "INSERT OR IGNORE INTO budget_ledger(kind,period,user) VALUES (?,?,?)",
            (kind, period, user),
        )
        return int(
            db.execute(
                f"SELECT {column} FROM budget_ledger WHERE kind=? AND period=? AND user=?",
                (kind, period, user),
            ).fetchone()[0]
        )

    @staticmethod
    def _warn(
        user: str,
        category: str,
        before: Decimal | int,
        after: Decimal | int,
        limit: Decimal | int | None,
    ) -> None:
        if limit is None or not (
            Decimal(before) < Decimal(limit) * Decimal("0.8") <= Decimal(after)
            or Decimal(before) < Decimal(limit) <= Decimal(after)
        ):
            return
        logging.getLogger("atlas.daily_budget").warning(
            json.dumps(
                {
                    "event": "budget_threshold",
                    "user": user,
                    "category": category,
                    "used": str(after),
                    "limit": str(limit),
                }
            )
        )

    def reserve_cost(
        self, user: str, amount: Decimal, lang: str, *, user_limits: bool = True
    ) -> CostReservation:
        limits = (
            (
                "daily_cost",
                "day",
                _limit("ATLAS_USER_DAILY_EUR"),
                _limit("ATLAS_GLOBAL_DAILY_EUR"),
            ),
            (
                "monthly_cost",
                "month",
                _limit("ATLAS_USER_MONTHLY_EUR"),
                _limit("ATLAS_GLOBAL_MONTHLY_EUR"),
            ),
        )
        day, month, micro = *self.periods(), int(amount * 1_000_000)
        if all(a is None and b is None for _, _, a, b in limits):
            return CostReservation(user, amount, day, month, False)
        with self.connection() as db:
            checks: list[tuple[str, int, int, Decimal | int | None]] = []
            for category, kind, user_limit, global_limit in limits:
                period = day if kind == "day" else month
                own = self._total(
                    db, kind, period, user, "reserved_micro + measured_micro"
                )
                total = self._total(
                    db, kind, period, None, "reserved_micro + measured_micro"
                )
                checks += [
                    # An API key with its own budget is capped by its weekly key cap
                    # (api_keys), not by the per-user limits; instance limits still apply.
                    (category, own, own + micro, user_limit if user_limits else None),
                    ("global_" + category, total, total + micro, global_limit),
                ]
            for category, _, used, limit in checks:
                if limit is not None and Decimal(used) / 1_000_000 > Decimal(limit):
                    raise BudgetRefusal(
                        category, Decimal(used) / 1_000_000, limit, lang
                    )
            for category, before, after, limit in checks:
                self._warn(
                    user if not category.startswith("global_") else "global",
                    category,
                    Decimal(before) / 1_000_000,
                    Decimal(after) / 1_000_000,
                    limit,
                )
            for kind, period in (("day", day), ("month", month)):
                db.execute(
                    "UPDATE budget_ledger SET reserved_micro=reserved_micro+? WHERE kind=? AND period=? AND user=?",
                    (micro, kind, period, user),
                )
        return CostReservation(user, amount, day, month, True)

    def settle_cost(self, reservation: CostReservation, measured: Decimal) -> None:
        if not reservation.enabled:
            return
        reserve, actual = int(reservation.amount * 1_000_000), int(measured * 1_000_000)
        with self.connection() as db:
            for kind, period in (
                ("day", reservation.day),
                ("month", reservation.month),
            ):
                db.execute(
                    "INSERT OR IGNORE INTO budget_ledger(kind,period,user) VALUES (?,?,?)",
                    (kind, period, reservation.user),
                )
                db.execute(
                    "UPDATE budget_ledger SET reserved_micro=MAX(0,reserved_micro-?), measured_micro=measured_micro+? WHERE kind=? AND period=? AND user=?",
                    (reserve, actual, kind, period, reservation.user),
                )

    def _search_limits(
        self, provider: str
    ) -> tuple[Decimal | int | None, Decimal | int | None, Decimal | int | None]:
        names = PROVIDER_SEARCH_LIMITS.get(provider, ("", "", ""))
        generic = (
            "ATLAS_USER_DAILY_SEARCHES",
            "ATLAS_GLOBAL_DAILY_SEARCHES",
            "ATLAS_GLOBAL_MONTHLY_SEARCHES",
        )
        return tuple(
            _limit(one) or _limit(two) for one, two in zip(generic, names, strict=True)
        )  # type: ignore[return-value]

    def reserve_search(self, user: str, lang: str, provider: str | None = None) -> None:
        selected = (
            provider
            or os.environ.get("ATLAS_SEARCH_PROVIDER", "").split(",", 1)[0].lower()
            or "serpapi"
        )
        if selected not in PROVIDER_SEARCH_LIMITS:
            raise ValueError("invalid_search_provider")
        user_limit, daily_global, monthly_global = self._search_limits(selected)
        if user_limit is None and daily_global is None and monthly_global is None:
            return
        day, month = self.periods()
        with self.connection() as db:
            daily_kind, monthly_kind = selected + "_day", selected + "_month"
            own = self._total(db, daily_kind, day, user, "searches")
            daily = self._total(db, daily_kind, day, None, "searches")
            monthly = self._total(db, monthly_kind, month, None, "searches")
            checks = (
                ("daily_search", own, own + 1, user_limit, user),
                ("global_daily_search", daily, daily + 1, daily_global, "global"),
                (
                    "global_monthly_search",
                    monthly,
                    monthly + 1,
                    monthly_global,
                    "global",
                ),
            )
            for category, _, used, limit, _ in checks:
                if limit is not None and used > limit:
                    raise BudgetRefusal(category, used, limit, lang)
            for category, before, after, limit, subject in checks:
                self._warn(subject, category, before, after, limit)
            for kind, period in ((daily_kind, day), (monthly_kind, month)):
                db.execute(
                    "INSERT OR IGNORE INTO budget_ledger(kind,period,user) VALUES (?,?,?)",
                    (kind, period, user),
                )
                db.execute(
                    "UPDATE budget_ledger SET searches=searches+1 WHERE kind=? AND period=? AND user=?",
                    (kind, period, user),
                )

    def summary(self) -> list[dict[str, object]]:
        day, _ = self.periods()
        with self.connection() as db:
            rows = db.execute(
                "SELECT user, measured_micro, reserved_micro, searches FROM budget_ledger WHERE kind='day' AND period=? ORDER BY user",
                (day,),
            ).fetchall()
        return [
            {
                "user": user,
                "measured_eur": measured / 1_000_000,
                "reserved_eur": reserved / 1_000_000,
                "searches": searches,
            }
            for user, measured, reserved, searches in rows
        ]
