"""Entity facts from Wikipedia and Wikidata, with the page URLs as sources.

One lookup is three requests at most: a search on the Wikipedia edition of the user's
language (top pages with their introduction and Wikidata identifier), the Wikidata
entity of the best page (claims, label, description), and the labels of the items those
claims name. Facts are read from the structured claims (dates with their precision,
places, people, organisations, office holders with start and end dates, the latest
population with its date), never from the model's memory. Wikipedia text is CC BY-SA,
Wikidata data CC0; the answer cites the pages, which the chat records as retrieved (D7).

API etiquette (https://www.mediawiki.org/wiki/API:Etiquette): serial requests per host
(reference_fetch), a descriptive User-Agent with the project URL, responses cached for a
day, gzip, ``maxlag`` on Wikidata.

Backend, chosen per request (``source_plan``):

- a level that refuses third-party lookups (strict, enhanced) reads the Wikipedia mirror
  operated by Atlas (kiwix.py) when one is configured: the query never leaves Atlas.
  Without a mirror, the lookup is refused like any other third-party egress;
- the standard level reads the live API, whose Wikidata facts are fresh (current office
  holders, latest figures). When the API fails or does not answer in time, the mirror
  answers instead and the result says how old its copy is;
- ``ATLAS_ENCYCLOPEDIA_BACKEND=local`` forces the mirror at every level (an offline
  instance).

With the mirror, structured facts still come from the Wikidata API when the request's
privacy level allows a third-party lookup, and are left out otherwise (a local Wikidata
extract is the next step, docs/23).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import quote

from packages import privacy
from services.orchestrator import kiwix, reference_fetch
from services.orchestrator.reference_fetch import SourceError

CAPABILITY = "reference_lookup"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
SUMMARY_CHARS = 1400
CACHE_SECONDS = 86_400
_LANGUAGE = re.compile(r"[a-z]{2,3}")

# Wikidata properties read for every entity, by the key they get in the result.
# "many": several values kept; "dated": values with their start and end dates.
PROPERTIES: tuple[tuple[str, str, str], ...] = (
    ("P31", "instance_of", "many"),
    ("P1448", "official_name", "one"),
    ("P569", "date_of_birth", "one"),
    ("P19", "place_of_birth", "one"),
    ("P570", "date_of_death", "one"),
    ("P20", "place_of_death", "one"),
    ("P27", "citizenship", "many"),
    ("P106", "occupation", "many"),
    ("P39", "positions_held", "dated"),
    ("P102", "political_party", "dated"),
    ("P571", "inception", "one"),
    ("P576", "dissolved", "one"),
    ("P112", "founded_by", "many"),
    ("P159", "headquarters", "one"),
    ("P169", "chief_executive", "dated"),
    ("P488", "chairperson", "dated"),
    ("P1128", "employees", "latest"),
    ("P452", "industry", "many"),
    ("P17", "country", "one"),
    ("P131", "located_in", "one"),
    ("P36", "capital", "one"),
    ("P35", "head_of_state", "dated"),
    ("P6", "head_of_government", "dated"),
    ("P1082", "population", "latest"),
    ("P2046", "area", "one"),
    ("P2044", "elevation", "one"),
    ("P50", "author", "many"),
    ("P170", "creator", "many"),
    ("P57", "director", "many"),
    ("P86", "composer", "many"),
    ("P175", "performer", "many"),
    ("P61", "inventor_or_discoverer", "many"),
    ("P577", "publication_date", "one"),
    ("P580", "start_time", "one"),
    ("P582", "end_time", "one"),
    ("P585", "point_in_time", "one"),
    ("P856", "official_website", "one"),
)
_MANY = 5
_DATED = 6
_JULIAN = "http://www.wikidata.org/entity/Q1985786"
_ENTITY = "http://www.wikidata.org/entity/"


def wiki_language(value: str | None, default: str = "en") -> str:
    code = (value or "").strip().lower().split("-")[0]
    return code if _LANGUAGE.fullmatch(code) else default


def _json(body: bytes, host: str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        raise SourceError(
            "source_invalid", f"{host} returned unreadable JSON"
        ) from None


async def search(query: str, language: str, timeout: float) -> list[dict[str, Any]]:
    """Top pages of one Wikipedia edition, best first, with intro and Wikidata id."""
    host = f"{language}.wikipedia.org"
    body = await reference_fetch.get(
        f"https://{host}/w/api.php",
        capability=CAPABILITY,
        timeout=timeout,
        ttl=CACHE_SECONDS,
        params={
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "generator": "search",
            "gsrsearch": query,
            "gsrlimit": "4",
            "gsrnamespace": "0",
            "prop": "pageprops|extracts|info",
            "ppprop": "wikibase_item|disambiguation",
            "exintro": "1",
            "explaintext": "1",
            "exlimit": "4",
            "inprop": "url",
        },
    )
    data = _json(body, host)
    pages = data.get("query", {}).get("pages", []) if isinstance(data, dict) else []
    rows = [page for page in pages if isinstance(page, dict) and page.get("title")]
    return sorted(rows, key=lambda page: int(page.get("index", 99)))


def forced_local() -> bool:
    """The operator's override: the local mirror at every level (offline instances)."""
    return (
        os.environ.get("ATLAS_ENCYCLOPEDIA_BACKEND", "api").strip().lower() == "local"
    )


Plan = Literal["live", "live_then_local", "local", "refused"]


def source_plan() -> Plan:
    """Which backend answers this request, from its privacy level and the setup."""
    mirror = kiwix.configured()
    if forced_local():
        return "local"
    if not privacy.allows(CAPABILITY):
        return "local" if mirror else "refused"
    return "live_then_local" if mirror else "live"


async def entities(
    ids: list[str],
    props: str,
    languages: list[str],
    timeout: float,
    site_title: tuple[str, str] | None = None,
) -> dict[str, Any]:
    if not ids and site_title is None:
        return {}
    selector = (
        {"sites": site_title[0], "titles": site_title[1]}
        if site_title is not None
        else {"ids": "|".join(ids[:50])}
    )
    body = await reference_fetch.get(
        WIKIDATA_API,
        capability=CAPABILITY,
        timeout=timeout,
        ttl=CACHE_SECONDS,
        max_bytes=8_000_000,
        params={
            "action": "wbgetentities",
            "format": "json",
            **selector,
            "props": props,
            "languages": "|".join(dict.fromkeys([*languages, "mul", "en"])),
            "maxlag": "5",
        },
    )
    data = _json(body, "www.wikidata.org")
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        code = str(data["error"].get("code", ""))
        raise SourceError(
            "source_rate_limited" if code == "maxlag" else "source_unavailable",
            f"Wikidata answered {code or 'an error'}",
        )
    found = data.get("entities", {}) if isinstance(data, dict) else {}
    return found if isinstance(found, dict) else {}


def _label(entity: dict[str, Any], languages: list[str], kind: str = "labels") -> str:
    values = entity.get(kind, {})
    if not isinstance(values, dict):
        return ""
    for language in [*languages, "mul", "en"]:
        value = values.get(language)
        if isinstance(value, dict) and isinstance(value.get("value"), str):
            return str(value["value"])
    return ""


def time_value(value: dict[str, Any]) -> str | None:
    """An ISO date at the precision Wikidata states (year, month or day)."""
    raw = str(value.get("time", ""))
    match = re.fullmatch(r"([+-])(\d{1,16})-(\d\d)-(\d\d)T.*", raw)
    if not match:
        return None
    sign, year, month, day = match.groups()
    precision = int(value.get("precision", 11))
    year_text = ("-" if sign == "-" else "") + year.lstrip("0").rjust(4, "0")
    if precision >= 11 and day != "00":
        text = f"{year_text}-{month}-{day}"
    elif precision == 10 and month != "00":
        text = f"{year_text}-{month}"
    elif precision >= 9:
        text = year_text
    elif precision == 8:
        text = f"{year_text[:-1]}0s"
    else:
        text = f"{year_text} (precision {precision})"
    if value.get("calendarmodel") == _JULIAN and precision >= 11:
        text += " (Julian calendar)"
    return text


def _snak_value(snak: object, refs: set[str]) -> object | None:
    if not isinstance(snak, dict) or snak.get("snaktype") != "value":
        return None
    datavalue = snak.get("datavalue", {})
    kind, value = datavalue.get("type"), datavalue.get("value")
    if kind == "wikibase-entityid" and isinstance(value, dict):
        item = str(value.get("id", ""))
        if re.fullmatch(r"Q\d+", item):
            refs.add(item)
            return {"item": item}
        return None
    if kind == "time" and isinstance(value, dict):
        return time_value(value)
    if kind == "quantity" and isinstance(value, dict):
        amount = str(value.get("amount", "")).lstrip("+")
        unit = str(value.get("unit", "1"))
        if unit.startswith(_ENTITY):
            refs.add(unit[len(_ENTITY) :])
            return {"amount": amount, "unit_item": unit[len(_ENTITY) :]}
        return {"amount": amount}
    if kind == "monolingualtext" and isinstance(value, dict):
        return str(value.get("text", ""))
    if kind == "string" and isinstance(value, str):
        return value
    return None


def _qualifier(statement: dict[str, Any], prop: str, refs: set[str]) -> object | None:
    for snak in statement.get("qualifiers", {}).get(prop, []) or []:
        value = _snak_value(snak, refs)
        if value is not None:
            return value
    return None


def _statements(claims: dict[str, Any], prop: str) -> list[dict[str, Any]]:
    rows = [
        s
        for s in claims.get(prop, []) or []
        if isinstance(s, dict) and s.get("rank") != "deprecated"
    ]
    return rows


def raw_facts(entity: dict[str, Any]) -> tuple[dict[str, object], set[str]]:
    """Facts with item references still unresolved, and the references to label."""
    claims = entity.get("claims", {})
    if not isinstance(claims, dict):
        return {}, set()
    refs: set[str] = set()
    facts: dict[str, object] = {}
    for prop, key, mode in PROPERTIES:
        rows = _statements(claims, prop)
        if not rows:
            continue
        preferred = [s for s in rows if s.get("rank") == "preferred"]
        if mode == "one":
            value = _snak_value((preferred or rows)[0].get("mainsnak"), refs)
            if value is not None:
                facts[key] = value
        elif mode == "many":
            values = [_snak_value(s.get("mainsnak"), refs) for s in (preferred or rows)]
            kept = [v for v in values if v is not None][:_MANY]
            if kept:
                facts[key] = kept
        elif mode == "latest":
            dated = [(str(_qualifier(s, "P585", refs) or ""), s) for s in rows]
            dated.sort(key=lambda pair: pair[0], reverse=True)
            when, statement = dated[0]
            value = _snak_value(statement.get("mainsnak"), refs)
            if value is not None:
                facts[key] = {"value": value, "point_in_time": when or None}
        else:
            entries = []
            for statement in rows:
                value = _snak_value(statement.get("mainsnak"), refs)
                if value is None:
                    continue
                entry: dict[str, object] = {"value": value}
                start = _qualifier(statement, "P580", refs)
                end = _qualifier(statement, "P582", refs)
                if prop == "P39":
                    place = _qualifier(statement, "P1001", refs) or _qualifier(
                        statement, "P642", refs
                    )
                    if place is not None:
                        entry["of"] = place
                entry["start"] = start
                entry["end"] = end
                entries.append(entry)
            entries.sort(key=lambda e: str(e.get("start") or ""), reverse=True)
            if entries:
                facts[key] = entries[:_DATED]
    return facts, refs


def _resolve(value: object, labels: dict[str, str]) -> object:
    if isinstance(value, dict):
        if set(value) == {"item"}:
            return labels.get(str(value["item"]), str(value["item"]))
        if "amount" in value:
            unit = labels.get(str(value.get("unit_item", "")), "")
            return f"{value['amount']} {unit}".strip()
        return {k: _resolve(v, labels) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, labels) for v in value]
    return value


async def _facts(
    result: dict[str, object],
    languages: list[str],
    timeout: float,
    qid: str = "",
    site_title: tuple[str, str] | None = None,
) -> None:
    """Add the Wikidata description and facts of an entity to ``result``."""
    found = await entities(
        [qid] if qid else [],
        "claims|labels|descriptions|info",
        languages,
        timeout,
        site_title,
    )
    entity = (
        found.get(qid)
        if qid
        else next((v for k, v in found.items() if re.fullmatch(r"Q\d+", k)), None)
    )
    if not isinstance(entity, dict) or not entity.get("claims"):
        return
    qid = str(entity.get("id", qid))
    facts, refs = raw_facts(entity)
    labels: dict[str, str] = {}
    refs_list = sorted(refs)
    for start in range(0, min(len(refs_list), 100), 50):
        named = await entities(
            refs_list[start : start + 50], "labels", languages, timeout
        )
        labels.update(
            {
                key: label
                for key, value in named.items()
                if isinstance(value, dict) and (label := _label(value, languages))
            }
        )
    result["description"] = _label(entity, languages, "descriptions")
    result["facts"] = _resolve(facts, labels)
    if re.fullmatch(r"Q\d+", qid):
        result["wikidata_url"] = f"https://www.wikidata.org/wiki/{quote(qid)}"
    if isinstance(entity.get("modified"), str):
        result["wikidata_modified"] = entity["modified"]


_NOTE = (
    "Community-edited reference. Check that this entry is the entity asked "
    "about (see other_matches). Office holders and figures can lag behind "
    "recent events: dated facts carry their start, end or point_in_time."
)


# Live failures the local copy may answer instead: an outage, a refusal to serve now,
# an unreadable answer, a timeout. "No article" is an answer, not a failure.
FALLBACK_CODES = frozenset(
    {"source_unavailable", "source_rate_limited", "source_invalid"}
)
# Seconds kept for the local copy when the live API is tried first.
LOCAL_RESERVE = 3.0


async def _local(
    query: str,
    language: str,
    timeout: float,
    live_failure: str | None = None,
) -> dict[str, object]:
    """The article from the local copy. ``live_failure``: the code of the live error
    this answer replaces (standard level), which also skips the Wikidata facts."""
    available = kiwix.books()
    edition = language if language in available else "en" if "en" in available else ""
    if not edition:
        raise SourceError("local_language_missing", "no local Wikipedia is configured")
    try:
        book, hits = await kiwix.search(query, edition, timeout)
        if not hits and edition != "en" and "en" in available:
            edition = "en"
            book, hits = await kiwix.search(query, edition, timeout)
        if not hits:
            raise SourceError(
                "no_encyclopedia_entry",
                f"no article of the local Wikipedia matches {query[:80]!r}",
            )
        hits = kiwix.best(hits, query)
        path, page = await kiwix.article(hits[0].path, timeout)
    except kiwix.KiwixError as exc:
        raise SourceError(exc.code, exc.detail) from None
    # A redirect entry (a short name) leads to the article: cite and name that one.
    title = hits[0].title if path == hits[0].path else kiwix.path_title(path)
    dated = kiwix.book_date(book)
    copy = f"the local copy {book}" + (f", dated {dated}" if dated else "")
    result: dict[str, object] = {
        "source": f"Wikipedia ({edition}), local copy {book}",
        "title": title,
        "url": kiwix.public_url(edition, path),
        "local_book": book,
        "copy_date": dated or None,
        "summary": kiwix.introduction(page, SUMMARY_CHARS),
        "other_matches": [hit.title for hit in hits[1:4]],
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "license": "Wikipedia text CC BY-SA 4.0; Wikidata data CC0",
        "note": _NOTE + f" The text comes from {copy}.",
    }
    notes: list[str] = []
    if live_failure is not None:
        result["live_error"] = live_failure
        notes.append(
            f"The live Wikipedia did not answer ({live_failure}): this text comes from "
            f"{copy}. Events after that date (a new office holder, a latest figure) "
            "are missing: tell the user the answer comes from a copy of that date, and "
            "use web_search for anything recent."
        )
        notes.append(
            "Structured Wikidata facts were not fetched: answer from the summary only."
        )
    elif privacy.allows(CAPABILITY):
        try:
            await _facts(
                result,
                list(dict.fromkeys([edition, language])),
                timeout,
                site_title=(f"{edition}wiki", title),
            )
        except SourceError as exc:
            # The local summary stands on its own; say why the facts are missing.
            notes.append(
                f"Structured Wikidata facts could not be fetched ({exc.code}): "
                "answer from the summary only."
            )
    else:
        notes.append(
            "Structured Wikidata facts are not available at this privacy level: "
            "answer from the summary only."
        )
    if notes:
        result["note"] = " ".join([str(result["note"]), *notes])
    return result


async def lookup(query: str, language: str, timeout: float) -> dict[str, object]:
    language = wiki_language(language)
    query = " ".join(query.split())[:200]
    if not query:
        raise SourceError(
            "invalid_query", "name the person, place, organisation or work"
        )
    plan = source_plan()
    if plan == "refused":
        # No mirror: the live API would carry the subject out of Atlas.
        raise privacy.refusal(CAPABILITY)
    if plan == "local":
        if not kiwix.configured():
            raise SourceError(
                "local_mirror_unavailable", "the local Wikipedia is not configured"
            )
        return await _local(query, language, timeout)
    if plan == "live":
        return await _live(query, language, timeout)
    try:
        return await asyncio.wait_for(
            _live(query, language, timeout), max(1.0, timeout - LOCAL_RESERVE)
        )
    except TimeoutError:
        failure = SourceError("source_unavailable", "the live Wikipedia timed out")
    except SourceError as exc:
        if exc.code not in FALLBACK_CODES:
            raise
        failure = exc
    try:
        return await _local(query, language, LOCAL_RESERVE, live_failure=failure.code)
    except SourceError as exc:
        raise SourceError(
            failure.code,
            f"{failure.detail}; the local copy did not answer either ({exc.code})",
        ) from None


async def _live(query: str, language: str, timeout: float) -> dict[str, object]:
    edition = language
    pages = await search(query, language, timeout)
    if not pages and language != "en":
        edition = "en"
        pages = await search(query, "en", timeout)
    if not pages:
        raise SourceError(
            "no_encyclopedia_entry",
            f"no Wikipedia article matches {query[:80]!r}; try another spelling or web_search",
        )
    articles = [p for p in pages if "disambiguation" not in (p.get("pageprops") or {})]
    best = (articles or pages)[0]
    qid = str((best.get("pageprops") or {}).get("wikibase_item", ""))
    languages = list(dict.fromkeys([edition, language]))
    result: dict[str, object] = {
        "source": f"Wikipedia ({edition}) and Wikidata",
        "title": best.get("title"),
        "url": best.get("canonicalurl") or best.get("fullurl"),
        "summary": " ".join(str(best.get("extract", "")).split())[:SUMMARY_CHARS],
        "other_matches": [p.get("title") for p in pages if p is not best][:3],
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "license": "Wikipedia text CC BY-SA 4.0; Wikidata data CC0",
        "note": _NOTE,
    }
    if "disambiguation" in (best.get("pageprops") or {}):
        result["note"] = (
            "This is a disambiguation page: ask which one is meant. " + _NOTE
        )
    if re.fullmatch(r"Q\d+", qid):
        await _facts(result, languages, timeout, qid=qid)
    return result
