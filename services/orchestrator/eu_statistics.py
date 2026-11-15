"""Official European statistics from the Eurostat dissemination API (no account, no key).

A curated set of indicators answers the usual questions (population, unemployment,
inflation, GDP, public debt and deficit, minimum wage, life expectancy, fertility) for
the EU, the euro area, member states and the other countries Eurostat covers. Each
indicator is a fixed dataset with fixed filters, so the model chooses what to measure
and the code fetches the published figure, its period, unit, status flags and the
dataset page to cite. Eurostat content is reusable with attribution
(https://ec.europa.eu/eurostat/about-us/policies/copyright).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from services.orchestrator import reference_fetch
from services.orchestrator.reference_fetch import SourceError

CAPABILITY = "public_statistics"
API = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
CACHE_SECONDS = 6 * 3600


@dataclass(frozen=True)
class Indicator:
    dataset: str
    filters: dict[str, str]
    unit: str
    description: str
    periods: int = 6
    extra: dict[str, str] = field(default_factory=dict)


INDICATORS: dict[str, Indicator] = {
    "population": Indicator("tps00001", {}, "persons", "Population on 1 January"),
    "unemployment_rate": Indicator(
        "une_rt_m",
        {"s_adj": "SA", "age": "TOTAL", "sex": "T", "unit": "PC_ACT"},
        "% of the labour force",
        "Unemployment rate, monthly, seasonally adjusted (ILO definition)",
        periods=12,
    ),
    "youth_unemployment_rate": Indicator(
        "une_rt_m",
        {"s_adj": "SA", "age": "Y_LT25", "sex": "T", "unit": "PC_ACT"},
        "% of the labour force under 25",
        "Unemployment rate of people under 25, monthly, seasonally adjusted",
        periods=12,
    ),
    "inflation_rate": Indicator(
        "prc_hicp_minr",
        {"coicop18": "TOTAL", "unit": "RCH_A"},
        "% change on the same month of the previous year",
        "Harmonised index of consumer prices (HICP), annual rate of change, monthly",
        periods=12,
    ),
    "gdp": Indicator(
        "nama_10_gdp",
        {"unit": "CP_MEUR", "na_item": "B1GQ"},
        "million euro, current prices",
        "Gross domestic product at market prices, annual",
    ),
    "gdp_growth": Indicator(
        "tec00115",
        {"unit": "CLV_PCH_PRE", "na_item": "B1GQ"},
        "% change on previous year, chain-linked volumes",
        "Real GDP growth rate, annual",
    ),
    "gdp_per_capita": Indicator(
        "nama_10_pc",
        {"unit": "CP_EUR_HAB", "na_item": "B1GQ"},
        "euro per inhabitant, current prices",
        "GDP per inhabitant, annual",
    ),
    "government_debt": Indicator(
        "gov_10dd_edpt1",
        {"unit": "PC_GDP", "sector": "S13", "na_item": "GD"},
        "% of GDP",
        "General government gross debt (Maastricht definition), annual",
    ),
    "government_deficit": Indicator(
        "gov_10dd_edpt1",
        {"unit": "PC_GDP", "sector": "S13", "na_item": "B9"},
        "% of GDP (negative: deficit)",
        "General government net lending (+) or net borrowing (-), annual",
    ),
    "minimum_wage": Indicator(
        "earn_mw_cur",
        {"currency": "EUR"},
        "euro per month",
        "Statutory monthly minimum wage, gross, half-yearly",
    ),
    "life_expectancy": Indicator(
        "demo_mlexpec",
        {"age": "Y_LT1", "sex": "T", "unit": "YR"},
        "years",
        "Life expectancy at birth, annual",
    ),
    "fertility_rate": Indicator(
        "demo_find",
        {"indic_de": "TOTFERRT"},
        "live births per woman",
        "Total fertility rate, annual",
    ),
}

# Geographic codes the model may pass, mapped to Eurostat's. Eurostat writes Greece EL
# and the United Kingdom UK; the euro area and the EU are aggregates.
_ALIASES = {
    "GR": "EL",
    "GB": "UK",
    "EU": "EU27_2020",
    "EU27": "EU27_2020",
    "EZ": "EA",
    "EUROZONE": "EA",
    "EA": "EA",
}
# The euro area aggregate is published under its changing composition (EA) and under
# fixed ones; datasets differ in which they fill first.
_EURO_AREA = ("EA", "EA21", "EA20")
_GEO = re.compile(r"[A-Z]{2}(?:\d{2})?(?:_\d{4})?")


def geo_codes(value: str) -> tuple[str, ...]:
    code = re.sub(r"[^A-Za-z0-9_]", "", value).upper()
    code = _ALIASES.get(code, code)
    if not _GEO.fullmatch(code):
        raise SourceError(
            "invalid_geo",
            "geo is an ISO 3166 alpha-2 country code (FR, DE, IT...), EU or EA",
        )
    return _EURO_AREA if code == "EA" else (code,)


def _period(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().upper()
    if re.fullmatch(r"\d{4}(?:-\d{2}|-S[12]|-Q[1-4])?", text):
        return text
    raise SourceError("invalid_period", "period is YYYY, YYYY-MM, YYYY-Qn or YYYY-Sn")


def rows(data: dict[str, Any]) -> list[dict[str, object]]:
    """Observations of a one-series JSON-stat answer, oldest first, missing ones skipped."""
    dimension = data.get("dimension", {})
    times = dimension.get("time", {}).get("category", {}).get("index", {})
    if not isinstance(times, dict):
        return []
    ordered = sorted(times.items(), key=lambda pair: int(pair[1]))
    values = data.get("value", {})
    status = data.get("status", {})
    labels = data.get("extension", {}).get("status", {}).get("label", {})
    found = []
    for period, index in ordered:
        value = values.get(str(index)) if isinstance(values, dict) else None
        if value is None:
            continue
        row: dict[str, object] = {"period": period, "value": value}
        flag = status.get(str(index)) if isinstance(status, dict) else None
        if isinstance(flag, str) and flag:
            row["flag"] = flag
            if isinstance(labels, dict) and isinstance(labels.get(flag), str):
                row["flag_meaning"] = labels[flag]
        found.append(row)
    return found


async def figure(
    indicator: str, geo: str, period: str | None, language: str, timeout: float
) -> dict[str, object]:
    spec = INDICATORS.get(indicator)
    if spec is None:
        raise SourceError("unknown_indicator", "indicators: " + ", ".join(INDICATORS))
    wanted = _period(period)
    lang = language.upper() if language.upper() in {"EN", "FR", "DE"} else "EN"
    data: dict[str, Any] = {}
    found: list[dict[str, object]] = []
    used = ""
    for code in geo_codes(geo):
        params = {
            **spec.filters,
            **spec.extra,
            "geo": code,
            "lang": lang,
            "format": "JSON",
        }
        if wanted:
            params["time"] = wanted
        else:
            params["lastTimePeriod"] = str(spec.periods)
        body = await reference_fetch.get(
            API + spec.dataset,
            capability=CAPABILITY,
            timeout=timeout,
            ttl=CACHE_SECONDS,
            params=params,
            max_bytes=1_000_000,
        )
        try:
            data = json.loads(body)
        except ValueError:
            raise SourceError(
                "source_invalid", "Eurostat returned unreadable JSON"
            ) from None
        if not isinstance(data, dict):
            raise SourceError(
                "source_invalid", "Eurostat returned an unexpected answer"
            )
        found, used = rows(data), code
        if found:
            break
    geo_label = (
        data.get("dimension", {})
        .get("geo", {})
        .get("category", {})
        .get("label", {})
        .get(used)
        if isinstance(data, dict)
        else None
    )
    page = f"https://ec.europa.eu/eurostat/databrowser/view/{spec.dataset}/default/table?lang={lang.lower()}"
    output: dict[str, object] = {
        "source": "Eurostat",
        "indicator": indicator,
        "description": spec.description,
        "dataset": spec.dataset,
        "dataset_label": data.get("label"),
        "geo": used,
        "geo_label": geo_label or used,
        "unit": spec.unit,
        "updated": data.get("updated"),
        "url": page,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if not found:
        output["note"] = (
            "Eurostat publishes no value for this indicator, place and period. Say so; "
            "never estimate it. A national statistics office may publish it."
        )
        output["values"] = []
        return output
    output["latest"] = found[-1]
    output["values"] = found[-spec.periods :]
    output["note"] = (
        "Give the value with its period and unit, name Eurostat and link the dataset "
        "page. Flags: p provisional, e estimated, b break in series."
    )
    return output
