"""Specialized public sources the chat uses before, or instead of, a general web search.

Roadmap T8: what a precise official source answers is fetched by code from that source,
not reconstructed from a general search and the model's memory. Four tools:

- ``encyclopedia``: Wikipedia and Wikidata (people, places, organisations, works);
- ``news_headlines``: public feeds of major French and European outlets;
- ``eu_statistics``: Eurostat indicators;
- ``french_commune``: mayor and population of a French commune (official open data).

Like the exact tools, they are offered on the turns whose question is about their
subject only (``relevant``), so every other turn keeps the request it has always sent and
recorded provider exchanges stay valid. web_search stays available on the same turns.
Every URL a result names was fetched for this turn, and the chat records it as retrieved
(D7). Contracts live here, not in prompts/.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.privacy import PrivacyRefusal
from packages.validation import describe_validation
from services.orchestrator import encyclopedia, eu_statistics, fr_communes, news_feeds
from services.orchestrator.loop import Message
from services.orchestrator.reference_fetch import SourceError


class Encyclopedia(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=200)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")


class NewsHeadlines(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    topic: str | None = Field(default=None, max_length=200)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    days: int = Field(default=3, ge=1, le=14)
    limit: int = Field(default=8, ge=1, le=15)


IndicatorName = Literal[
    "population",
    "unemployment_rate",
    "youth_unemployment_rate",
    "inflation_rate",
    "gdp",
    "gdp_growth",
    "gdp_per_capita",
    "government_debt",
    "government_deficit",
    "minimum_wage",
    "life_expectancy",
    "fertility_rate",
]


class EuStatistics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    indicator: IndicatorName
    geo: str = Field(min_length=2, max_length=12)
    period: str | None = Field(default=None, max_length=10)


class FrenchCommune(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=120)
    department: str | None = Field(default=None, max_length=40)
    postal_code: str | None = Field(default=None, pattern=r"^\d{5}$")


SCHEMAS: dict[str, type[BaseModel]] = {
    "encyclopedia": Encyclopedia,
    "news_headlines": NewsHeadlines,
    "eu_statistics": EuStatistics,
    "french_commune": FrenchCommune,
}
FAMILIES = {
    "encyclopedia": "encyclopedia",
    "news": "news_headlines",
    "statistics": "eu_statistics",
    "commune": "french_commune",
}
_ORDER = ("commune", "statistics", "encyclopedia", "news")
DESCRIPTIONS = {
    "encyclopedia": (
        "Facts about a named person, place, organisation, event or work, fetched by "
        "code from Wikipedia and Wikidata: dates of birth and death, places, founding "
        "date and founders, office holders with start and end dates, latest "
        "population, headquarters, authors and creators. Call it before answering such "
        "a question, instead of answering from memory, and before web_search when the "
        "entity has a Wikipedia article. query: the entity's name only, not the whole "
        "question; language: the user's language code (fr, en, de, es, it...). The "
        "result gives the Wikipedia page URL to cite, its summary and dated facts. "
        "Check that the entry is the entity asked about (other_matches). When it is "
        "not, when the fact is missing, or when it may have changed recently (a "
        "current office holder, a latest figure), use web_search. Never present a fact "
        "the result does not contain as coming from it."
    ),
    "news_headlines": (
        "Latest headlines from the public RSS feeds of major French and European "
        "outlets (franceinfo, Le Monde, RFI, France 24, Le Figaro, BBC, DW, tagesschau, "
        "El Pais, ANSA and others), fetched by code: title, outlet, publication time, "
        "link and short description. Call it first for latest news, what is happening, "
        "or today's news on a subject. topic: the subject's key words (people, places, "
        "organisations, events), or omit it for the main headlines; language: the "
        "user's language code; days: how far back (default 3). Cite each item with "
        "its outlet, date and link. The feeds hold the main stories of the last days "
        "only: when nothing matches, say so or use web_search. Never invent or date a "
        "news item yourself."
    ),
    "eu_statistics": (
        "Official Eurostat figure, fetched by code. indicator: population (1 January), "
        "unemployment_rate (monthly, seasonally adjusted), youth_unemployment_rate "
        "(under 25), inflation_rate (HICP, annual rate, monthly), gdp (million euro), "
        "gdp_growth (real, annual %), gdp_per_capita (euro), government_debt and "
        "government_deficit (% of GDP), minimum_wage (euro per month), "
        "life_expectancy (at birth), fertility_rate. geo: ISO 3166 alpha-2 country "
        "code (FR, DE, ES, IT, PL...; EL is Greece), EU for the European Union, EA for "
        "the euro area. period: omit it for the latest published value, or YYYY or "
        "YYYY-MM. Returns the value, period, unit, status flag and the dataset page "
        "URL to cite. Call it before web_search for these indicators of European "
        "countries; never give such a figure from memory. National definitions (INSEE "
        "unemployment, hourly minimum wage) can differ: say which one you give."
    ),
    "french_commune": (
        "Current mayor (Repertoire national des elus, French Ministry of the "
        "Interior), INSEE municipal population, INSEE code, department and postal "
        "codes of a French commune, from official open data held by Atlas. Call it "
        "for the mayor or population of a French town or village, before web_search. "
        "name: the commune as the user wrote it; department (name or number) or "
        "postal_code when several communes share the name. Cite the Repertoire "
        "national des elus for the mayor, with the start date of the function. For "
        "a city outside France, use encyclopedia."
    ),
}


def declarations(families: Sequence[str] | frozenset[str]) -> list[Message]:
    return [
        {
            "type": "function",
            "function": {
                "name": FAMILIES[family],
                "description": DESCRIPTIONS[FAMILIES[family]],
                "parameters": SCHEMAS[FAMILIES[family]].model_json_schema(),
            },
        }
        for family in _ORDER
        if family in families
    ]


async def execute(name: str, arguments: str, timeout: float, language: str) -> Message:
    schema = SCHEMAS[name]
    try:
        request = schema.model_validate_json(arguments)
        if isinstance(request, Encyclopedia):
            data = await encyclopedia.lookup(
                request.query, request.language or language, timeout
            )
        elif isinstance(request, NewsHeadlines):
            data = await news_feeds.headlines(
                request.topic,
                request.language or language,
                timeout,
                days=request.days,
                limit=request.limit,
            )
        elif isinstance(request, EuStatistics):
            data = await eu_statistics.figure(
                request.indicator, request.geo, request.period, language, timeout
            )
        else:
            assert isinstance(request, FrenchCommune)
            data = await fr_communes.commune(
                request.name, request.department, request.postal_code, timeout
            )
    except ValidationError as exc:
        return {
            "error": "invalid_arguments",
            "message": describe_validation(exc),
            "schema": schema.model_json_schema(),
        }
    except SourceError as exc:
        return {"error": exc.code, "message": exc.detail}
    except PrivacyRefusal as exc:
        return {"error": exc.code, "message": exc.message}
    return {"data": data}


def cited_urls(data: object) -> list[str]:
    """Every source URL a result names: all were fetched for this turn (D7)."""
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if key in {"url", "wikidata_url", "dataset_url"} and isinstance(value, str):
                found.append(value)
            elif key == "feed_urls" and isinstance(value, list):
                found.extend(str(v) for v in value if isinstance(v, str))
            elif isinstance(value, (dict, list)):
                found.extend(cited_urls(value))
    elif isinstance(data, list):
        for value in data:
            found.extend(cited_urls(value))
    return [url for url in dict.fromkeys(found) if url.startswith("https://")]


# --- Relevance: which sources a question is about. -------------------------------
# Patterns run on lower-case text without accents, in the five UI languages. A request
# that asks for a web search explicitly keeps the web tools only.

_EXPLICIT_WEB = re.compile(
    r"\b(?:recherch\w*|cherch\w*|search\w*|google\w*|such\w*|busca\w*|cerca\w*)\b"
    r".{0,60}?\b(?:web|internet|en ligne|online|google)\b"
)
_ENCYCLOPEDIA = re.compile(
    "|".join(
        (
            r"\b(?:est|etait|suis|es|sont|etaient)\s+nee?s?\b",
            r"\bnee?s?\s+(?:le|en|a|au|aux|dans|ou|quand|quel|quelle)\b",
            r"\bdate\s+de\s+naissance\b|\bnaissance\s+(?:de|du|d')",
            r"\bborn\b|\bbirth\s*(?:date|day|place)\b|\bdate of birth\b|\bbirthplace\b",
            r"\bgeboren\b|\bgeburtstag\b|\bgeburtsdatum\b|\bgeburtsort\b",
            r"\bnaci(?:o|do|da|miento)\b|\b(?:e|fu|e')\s+nat[oa]\b|\bdata di nascita\b",
            r"\b(?:est|etait)\s+morte?\b|\bdecede\w*|\bdeces\b|\bdate\s+de\s+(?:sa\s+)?mort\b|\bmorte?\s+(?:le|en)\s",
            r"\bdied\b|\bdeath\s+(?:of|date|year|place)\b|\bgestorben\b|\btodestag\b",
            r"\bmurio\b|\bfallecio\b|\bfallecimiento\b|\b(?:e|fu)\s+mort[oa]\b",
            r"\bfond(?:e|ee|es|ees|ateur|ateurs|atrice|atrices)\b|\bfondation\s+(?:de|du|d')",
            r"\bfounded\b|\bfounders?\b|\bco-?founders?\b|\bestablished in\b",
            r"\bgegrundet\b|\bgrunder(?:in)?\b|\bgrundung\b",
            r"\bfundad[oa]s?\b|\bfundador(?:es|a)?\b|\bfondat[oaie]\b|\bfondator[ei]\b",
            r"\bcreee?s?\s+en\b|\b(?:date|annee)\s+de\s+creation\b",
            r"\ben quelle annee\b|\bin (?:what|which) year\b|\bwhat year\b|\bin welchem jahr\b",
            r"\ben que ano\b|\bin che anno\b",
            r"\bbiograph\w*|\bbiografi\w*|\blebenslauf\b|\bwiki(?:pedia)?\b",
            r"\bqui\s+a\s+(?:invente|fonde|ecrit|realise|compose|peint|decouvert|construit|concu)\b",
            r"\bwho\s+(?:invented|founded|wrote|directed|composed|painted|discovered|built|designed)\b",
            r"\bwer\s+hat\b.{0,60}?\b(?:erfunden|gegrundet|geschrieben|komponiert|gemalt|entdeckt|gebaut)\b",
            r"\bquien\s+(?:invento|fundo|escribio|dirigio|compuso|pinto|descubrio)\b",
            r"\bchi\s+ha\s+(?:inventato|fondato|scritto|diretto|composto|dipinto|scoperto)\b",
            r"\bdate\s+de\s+(?:sortie|parution|publication)\b|\brelease date of\b",
            r"\bsiege\s+(?:social\s+)?(?:de|du|d')|\bheadquarter\w*|\bsuperficie\b|\baltitude\b",
        )
    )
)
_MAYOR = re.compile(
    r"\bmaires?\b|\bmairie\s+de\b|\bmayor\b|\bburgermeister\w*|\balcalde\w*|\bsindac[oa]\b"
)
_POPULATION = re.compile(
    r"\bcombien\s+(?:d'|de\s+|y\s+a\s+t\s+il\s+d')?habitants\b|\bnombre\s+d'habitants\b"
    r"|\bpopulation\s+(?:de|du|des|d'|of|in|en|actuelle)\b|\bhow many (?:people|inhabitants|residents) live\b"
    r"|\beinwohner\w*|\bhabitantes\b|\babitanti\b"
)
_COMMUNE = re.compile(r"\bcode\s+insee\b|\bcode\s+postal\s+(?:de|du|d')")
_STATISTICS = re.compile(
    "|".join(
        (
            r"\bchomage\b|\bchomeurs\b|\bunemploy\w*|\barbeitslos\w*|\bdesempleo\b|\bparo\b|\bdisoccupazione\b",
            r"\binflation\b|\binflacion\b|\binflazione\b|\bteuerung\w*|\bhausse\s+des\s+prix\b|\bipch\b|\bhicp\b",
            r"\bindice\s+des\s+prix\b|\bconsumer\s+prices?\b|\bverbraucherpreis\w*",
            r"\bpib\b|\bgdp\b|\bbip\b|\bproduit\s+interieur\s+brut\b|\bgross\s+domestic\s+product\b",
            r"\bbruttoinlandsprodukt\b|\bproducto\s+interior\s+bruto\b|\bprodotto\s+interno\s+lordo\b",
            r"\bcroissance\s+(?:economique|du\s+pib)\b|\beconomic\s+growth\b|\bwirtschaftswachstum\b",
            r"\bdette\s+publique\b|\b(?:public|government|national)\s+debt\b|\bstaatsverschuldung\b|\bstaatsschulden\b",
            r"\bdeuda\s+publica\b|\bdebito\s+pubblico\b|\bdeficit\s+public\b|\b(?:budget|government)\s+deficit\b",
            r"\bsmic\b|\bsalaire\s+minimum\b|\bminimum\s+wage\b|\bmindestlohn\b|\bsalario\s+minimo\b",
            r"\besperance\s+de\s+vie\b|\blife\s+expectancy\b|\blebenserwartung\b|\besperanza\s+de\s+vida\b|\baspettativa\s+di\s+vita\b",
            r"\bfecondite\b|\bfertility\s+rate\b|\bgeburtenrate\b|\bnatalite\b|\bbirth\s+rate\b",
        )
    )
)
_NEWS = re.compile(
    "|".join(
        (
            r"\bactu(?:alite|alites|s)?\b|\binfos?\s+(?:du\s+jour|sur|de\s+la\s+journee)\b",
            r"\bdernieres?\s+(?:nouvelles|infos?|informations|actus?)\b|\bnouvelles\s+du\s+jour\b",
            r"\bnews\b|\bheadlines?\b|\bbreaking\b|\bgros\s+titres\b|\ba\s+la\s+une\b",
            r"\bnachrichten\b|\bschlagzeilen\b|\bneuigkeiten\b|\bnoticias?\b|\btitulares\b|\bnotizie\b",
            r"\bquoi\s+de\s+neuf\s+(?:en|au|aux|a|dans|sur|cote|avec)\b",
            r"\bque\s+se\s+passe(?:-t-il|\s+t\s+il)?\s+(?:en|au|aux|a|dans|avec|cote)\b",
            r"\bwhat(?:'s|\s+is)\s+(?:happening|going\s+on)\s+(?:in|with|at)\b",
            r"\bwhat\s+happened\s+(?:today|yesterday|this\s+week)\b|\bs'est\s+passe\s+(?:aujourd|hier|ce\s+matin|cette\s+semaine)",
        )
    )
)


def _plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower().replace("’", "'"))
    return "".join(c for c in folded if not unicodedata.combining(c))


def relevant(question: str, previous: str = "") -> frozenset[str]:
    """Sources the question is about; a short follow-up inherits the previous one's."""
    text = _plain(question[:4000])
    if _EXPLICIT_WEB.search(text):
        return frozenset()
    found: set[str] = set()
    if _ENCYCLOPEDIA.search(text):
        found.add("encyclopedia")
    if _MAYOR.search(text):
        found |= {"commune", "encyclopedia"}
    if _POPULATION.search(text):
        found |= {"commune", "encyclopedia", "statistics"}
    if _COMMUNE.search(text):
        found.add("commune")
    if _STATISTICS.search(text):
        found.add("statistics")
    if _NEWS.search(text):
        found.add("news")
    if not found and previous and len(text) <= 120:
        found = set(relevant(previous))
    return frozenset(found)
