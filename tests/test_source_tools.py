"""T8: specialized public sources (encyclopedia, news feeds, Eurostat, French communes)."""

import json
import tempfile
import unittest
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from packages import privacy
from services.orchestrator import (
    encyclopedia,
    eu_statistics,
    fr_communes,
    news_feeds,
    reference_fetch,
    source_tools,
)
from services.orchestrator.chat_pipeline import ChatTools
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call
from services.orchestrator.reference_fetch import SourceError

# A fictional person and town: Wikipedia search answer and Wikidata entities.
SEARCH = {
    "query": {
        "pages": [
            {
                "title": "Lucienne Varnier (homonymie)",
                "index": 2,
                "pageprops": {"disambiguation": "", "wikibase_item": "Q900"},
                "extract": "Plusieurs personnes.",
                "canonicalurl": "https://fr.wikipedia.org/wiki/Lucienne_Varnier_(homonymie)",
            },
            {
                "title": "Lucienne Varnier",
                "index": 1,
                "pageprops": {"wikibase_item": "Q901"},
                "extract": "Lucienne Varnier, née le 3 mars 1911 à Bellerive, est une ingénieure.",
                "canonicalurl": "https://fr.wikipedia.org/wiki/Lucienne_Varnier",
            },
        ]
    }
}


def snak(kind: str, value: object) -> dict[str, object]:
    return {"snaktype": "value", "datavalue": {"type": kind, "value": value}}


def item(qid: str) -> dict[str, object]:
    return snak("wikibase-entityid", {"id": qid})


def when(text: str, precision: int = 11) -> dict[str, object]:
    return snak(
        "time",
        {
            "time": text,
            "precision": precision,
            "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
        },
    )


ENTITY = {
    "entities": {
        "Q901": {
            "modified": "2026-11-01T10:00:00Z",
            "labels": {"fr": {"value": "Lucienne Varnier"}},
            "descriptions": {"fr": {"value": "ingénieure fictive"}},
            "claims": {
                "P569": [{"rank": "normal", "mainsnak": when("+1911-03-03T00:00:00Z")}],
                "P19": [{"rank": "normal", "mainsnak": item("Q910")}],
                "P570": [
                    {"rank": "deprecated", "mainsnak": when("+1980-01-01T00:00:00Z")},
                    {"rank": "normal", "mainsnak": when("+1979-00-00T00:00:00Z", 9)},
                ],
                "P39": [
                    {
                        "rank": "normal",
                        "mainsnak": item("Q920"),
                        "qualifiers": {
                            "P580": [when("+1950-06-01T00:00:00Z")],
                            "P582": [when("+1958-00-00T00:00:00Z", 9)],
                        },
                    },
                    {
                        "rank": "normal",
                        "mainsnak": item("Q920"),
                        "qualifiers": {"P580": [when("+1962-04-00T00:00:00Z", 10)]},
                    },
                ],
                "P1082": [
                    {
                        "rank": "normal",
                        "mainsnak": snak("quantity", {"amount": "+1200", "unit": "1"}),
                        "qualifiers": {"P585": [when("+2015-01-01T00:00:00Z")]},
                    },
                    {
                        "rank": "normal",
                        "mainsnak": snak("quantity", {"amount": "+1350", "unit": "1"}),
                        "qualifiers": {"P585": [when("+2024-01-01T00:00:00Z")]},
                    },
                ],
            },
        }
    }
}
LABELS = {
    "entities": {
        "Q910": {"labels": {"fr": {"value": "Bellerive"}}},
        "Q920": {"labels": {"en": {"value": "mayor of Bellerive"}}},
    }
}

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Exemple</title>
<item><title>R\xc3\xa9forme des retraites : le vote report\xc3\xa9</title>
<link>https://news.example.org/a1?xtor=RSS</link>
<description>&lt;p&gt;Le texte sur les retraites revient en commission.&lt;/p&gt;</description>
<pubDate>Sun, 15 Nov 2026 07:30:00 +0100</pubDate></item>
<item><title>R\xc3\xa9forme des retraites : le vote report\xc3\xa9</title>
<link>https://news.example.org/a1</link><pubDate>Sun, 15 Nov 2026 07:00:00 +0100</pubDate></item>
<item><title>M\xc3\xa9t\xc3\xa9o : du vent sur la c\xc3\xb4te</title>
<link>https://news.example.org/a2</link><pubDate>Sun, 15 Nov 2026 06:00:00 +0100</pubDate></item>
<item><title>Ancien sujet sur les retraites</title>
<link>https://news.example.org/a3</link><pubDate>Mon, 02 Nov 2026 06:00:00 +0100</pubDate></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Ex</title>
<entry><title>Pension reform vote delayed</title>
<link rel="alternate" href="https://atom.example.org/p1"/>
<updated>2026-11-15T08:00:00Z</updated><summary>Lawmakers postponed the pension vote.</summary></entry>
</feed>"""
RDF = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
<item><title>Rentenreform vertagt</title><link>https://rdf.example.org/r1</link>
<dc:date>2026-11-15T09:00:00+01:00</dc:date></item></rdf:RDF>"""

EUROSTAT = {
    "label": "Unemployment by sex and age - monthly data",
    "updated": "2026-11-05T23:00:00+0100",
    "value": {"0": 7.9, "2": 8.1},
    "status": {"2": "p"},
    "extension": {"status": {"label": {"p": "provisional"}}},
    "dimension": {
        "geo": {"category": {"label": {"FR": "France"}}},
        "time": {"category": {"index": {"2026-07": 0, "2026-08": 1, "2026-09": 2}}},
    },
}


def mayors_csv() -> bytes:
    header = (
        "Code du département;Libellé du département;Code de la collectivité à statut "
        "particulier;Libellé de la collectivité à statut particulier;Code de la commune;"
        "Libellé de la commune;Nom de l'élu;Prénom de l'élu;Code sexe;Date de naissance;"
        "Code de la catégorie socio-professionnelle;Libellé de la catégorie "
        "socio-professionnelle;Date de début du mandat;Date de début de la fonction"
    )
    rows = [header]
    rows.append(
        "35;Ille-et-Vilaine;;;35999;Saint-Bellerive;DUVANEL;Odile;F;1970-01-01;1;X;2026-03-15;2026-03-21"
    )
    rows.append(
        "56;Morbihan;;;56999;Saint-Bellerive;KERVAREC;Yann;M;1960-01-01;1;X;2026-03-15;2026-03-22"
    )
    rows.extend(
        f"01;Ain;;;01{index:03d};Village {index};NOM;Prenom;M;1950-01-01;1;X;2026-03-15;2026-03-20"
        for index in range(1000)
    )
    return "\r\n".join(rows).encode()


COMMUNES = [
    {
        "nom": "Saint-Bellerive",
        "code": "35999",
        "population": 4100,
        "codesPostaux": ["35999"],
        "codeDepartement": "35",
    },
    {
        "nom": "Saint-Bellerive",
        "code": "56999",
        "population": 900,
        "codesPostaux": ["56999"],
        "codeDepartement": "56",
    },
    *(
        {
            "nom": f"Village {i}",
            "code": f"01{i:03d}",
            "population": 10,
            "codesPostaux": [],
            "codeDepartement": "01",
        }
        for i in range(1000)
    ),
]
DEPARTMENTS = [
    {"nom": "Ille-et-Vilaine", "code": "35"},
    {"nom": "Morbihan", "code": "56"},
]
RNE = {
    "resources": [
        {
            "url": "https://static.data.gouv.fr/resources/x/elus-conseiller-municipal-cm.csv"
        },
        {
            "url": "https://static.data.gouv.fr/resources/x/elus-maire-mai.csv",
            "last_modified": "2026-08-11T15:51:01",
        },
    ]
}


def fake_get(
    routes: dict[str, bytes], seen: list[str] | None = None
) -> Callable[..., Any]:
    async def get(
        url: str, *, capability: str, params: dict[str, str] | None = None, **_: Any
    ) -> bytes:
        privacy.require_shared(capability)
        full = url + ("?" + json.dumps(params, sort_keys=True) if params else "")
        if seen is not None:
            seen.append(full)
        for prefix, body in routes.items():
            if (
                full.startswith(prefix)
                or (params and params.get("action") == prefix)
                or (params and params.get("ids", "").startswith(prefix))
            ):
                return body
        raise SourceError("source_not_found", url)

    return get


class EncyclopediaTests(unittest.IsolatedAsyncioTestCase):
    def test_dates_keep_their_precision(self) -> None:
        def value(text: str, precision: int = 11) -> str | None:
            datavalue = when(text, precision)["datavalue"]
            assert isinstance(datavalue, dict)
            return encyclopedia.time_value(dict(datavalue["value"]))

        self.assertEqual(value("+1911-03-03T00:00:00Z"), "1911-03-03")
        self.assertEqual(value("+1962-04-00T00:00:00Z", 10), "1962-04")
        self.assertEqual(value("+1979-00-00T00:00:00Z", 9), "1979")
        self.assertEqual(value("-0044-03-15T00:00:00Z"), "-0044-03-15")

    async def test_lookup_reads_dated_claims_and_skips_disambiguation(self) -> None:
        routes = {
            "query": json.dumps(SEARCH).encode(),
            "Q901": json.dumps(ENTITY).encode(),
            "Q910": json.dumps(LABELS).encode(),
        }
        with patch.object(reference_fetch, "get", fake_get(routes)):
            result = await encyclopedia.lookup("lucienne varnier", "fr", 5)
        self.assertEqual(result["title"], "Lucienne Varnier")
        self.assertEqual(
            result["url"], "https://fr.wikipedia.org/wiki/Lucienne_Varnier"
        )
        self.assertEqual(result["wikidata_url"], "https://www.wikidata.org/wiki/Q901")
        facts = result["facts"]
        assert isinstance(facts, dict)
        self.assertEqual(facts["date_of_birth"], "1911-03-03")
        self.assertEqual(facts["place_of_birth"], "Bellerive")
        # The deprecated statement is ignored.
        self.assertEqual(facts["date_of_death"], "1979")
        # Latest position first, the open one has no end.
        self.assertEqual(
            facts["positions_held"][0],
            {"value": "mayor of Bellerive", "start": "1962-04", "end": None},
        )
        self.assertEqual(
            facts["population"], {"value": "1350", "point_in_time": "2024-01-01"}
        )
        self.assertEqual(result["other_matches"], ["Lucienne Varnier (homonymie)"])

    async def test_no_article_is_an_error_never_a_guess(self) -> None:
        empty = json.dumps({"batchcomplete": True}).encode()
        with patch.object(reference_fetch, "get", fake_get({"query": empty})):
            result = await source_tools.execute(
                "encyclopedia", '{"query": "Zzyzx Qwertu"}', 5, "fr"
            )
        self.assertEqual(result["error"], "no_encyclopedia_entry")

    def test_language_codes_are_safe_host_labels(self) -> None:
        self.assertEqual(encyclopedia.wiki_language("fr-FR"), "fr")
        self.assertEqual(encyclopedia.wiki_language("evil.example"), "en")
        self.assertFalse(reference_fetch.allowed_host("fr.wikipedia.org.example.net"))
        self.assertTrue(reference_fetch.allowed_host("de.wikipedia.org"))


class NewsTests(unittest.TestCase):
    outlet = news_feeds.Outlet("ex", "Exemple", "fr", "https://news.example.org/rss")

    def test_rss_atom_and_rdf_are_read(self) -> None:
        items = news_feeds.parse(RSS, self.outlet)
        self.assertEqual(len(items), 4)
        self.assertEqual(
            items[0]["summary"], "Le texte sur les retraites revient en commission."
        )
        self.assertEqual(items[0]["published"], "2026-11-15T06:30+00:00")
        atom = news_feeds.parse(ATOM, self.outlet)
        self.assertEqual(atom[0]["url"], "https://atom.example.org/p1")
        rdf = news_feeds.parse(RDF, self.outlet)
        self.assertEqual(rdf[0]["published"], "2026-11-15T08:00+00:00")

    def test_document_types_are_refused(self) -> None:
        with self.assertRaises(SourceError):
            news_feeds.parse(
                b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss/>',
                self.outlet,
            )

    def test_topic_window_and_duplicates(self) -> None:
        now = datetime(2026, 11, 15, 9, tzinfo=timezone.utc)
        items = news_feeds.parse(RSS, self.outlet)
        words = news_feeds.topic_words(
            "c'est quoi les dernières actus sur la réforme des retraites ?"
        )
        self.assertEqual(words, ["reforme", "retraites"])
        chosen = news_feeds.select(items, words, now, 3, 10)
        # Same article twice (tracking parameter), an old one and an unrelated one.
        self.assertEqual(
            [i["url"] for i in chosen], ["https://news.example.org/a1?xtor=RSS"]
        )
        everything = news_feeds.select(items, [], now, 3, 10)
        self.assertEqual(len(everything), 2)

    def test_outlets_follow_the_operator_list(self) -> None:
        with patch.dict("os.environ", {"ATLAS_NEWS_OUTLETS": "rfi,tagesschau"}):
            self.assertEqual([o.id for o in news_feeds.outlets("fr")], ["rfi"])
            self.assertEqual([o.id for o in news_feeds.outlets("de")], ["tagesschau"])
        self.assertTrue(all(o.url.startswith("https://") for o in news_feeds.OUTLETS))


class StatisticsTests(unittest.IsolatedAsyncioTestCase):
    def test_missing_values_are_skipped_and_flags_kept(self) -> None:
        rows = eu_statistics.rows(EUROSTAT)
        self.assertEqual(
            rows,
            [
                {"period": "2026-07", "value": 7.9},
                {
                    "period": "2026-09",
                    "value": 8.1,
                    "flag": "p",
                    "flag_meaning": "provisional",
                },
            ],
        )

    def test_geo_codes(self) -> None:
        self.assertEqual(eu_statistics.geo_codes("GR"), ("EL",))
        self.assertEqual(eu_statistics.geo_codes("eu"), ("EU27_2020",))
        self.assertEqual(eu_statistics.geo_codes("EA"), ("EA", "EA21", "EA20"))
        with self.assertRaises(SourceError):
            eu_statistics.geo_codes("France entière")

    async def test_figure_gives_latest_value_and_page(self) -> None:
        seen: list[str] = []
        routes = {eu_statistics.API + "une_rt_m": json.dumps(EUROSTAT).encode()}
        with patch.object(reference_fetch, "get", fake_get(routes, seen)):
            result = await eu_statistics.figure(
                "unemployment_rate", "FR", None, "fr", 5
            )
        self.assertEqual(
            result["latest"],
            {
                "period": "2026-09",
                "value": 8.1,
                "flag": "p",
                "flag_meaning": "provisional",
            },
        )
        self.assertEqual(
            result["url"],
            "https://ec.europa.eu/eurostat/databrowser/view/une_rt_m/default/table?lang=fr",
        )
        self.assertIn('"s_adj": "SA"', seen[0])


class CommuneTests(unittest.IsolatedAsyncioTestCase):
    def routes(self) -> dict[str, bytes]:
        return {
            fr_communes.RNE_DATASET: json.dumps(RNE).encode(),
            "https://static.data.gouv.fr/resources/x/elus-maire-mai.csv": mayors_csv(),
            fr_communes.COMMUNES_API: json.dumps(COMMUNES).encode(),
            fr_communes.DEPARTMENTS_API: json.dumps(DEPARTMENTS).encode(),
        }

    async def test_homonyms_spelling_and_department(self) -> None:
        with (
            patch.object(reference_fetch, "get", fake_get(self.routes())),
            patch.object(fr_communes, "DIRECTORY", fr_communes.Directory()),
        ):
            both = await fr_communes.commune("st bellerive", None, None, 5)
            one = await fr_communes.commune("Saint Bellerive", "Morbihan", None, 5)
        matches = both["matches"]
        assert isinstance(matches, list)
        self.assertEqual([m["insee_code"] for m in matches], ["35999", "56999"])
        self.assertIn("Several communes", str(both["note"]))
        mayor = one["matches"][0]["mayor"]  # type: ignore[index]
        self.assertEqual(mayor["name"], "Yann Kervarec")
        self.assertEqual(mayor["function_start"], "2026-03-22")
        # Birth dates and occupations of the file are never returned.
        self.assertNotIn("1960", json.dumps(one))

    def test_a_truncated_mayors_file_is_refused(self) -> None:
        with self.assertRaises(SourceError):
            fr_communes.parse_mayors(mayors_csv()[:2000])


class PrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_refuses_every_source_before_sending(self) -> None:
        token = privacy.elevate(privacy.STRICT)
        try:
            for name, arguments in (
                ("encyclopedia", '{"query": "Bellerive"}'),
                ("news_headlines", '{"topic": "retraites"}'),
                ("eu_statistics", '{"indicator": "gdp", "geo": "FR"}'),
                ("french_commune", '{"name": "Bellerive"}'),
            ):
                with self.subTest(tool=name):
                    with patch("aiohttp.ClientSession") as session:
                        result = await source_tools.execute(name, arguments, 5, "fr")
                    self.assertEqual(result["error"], privacy.CODE)
                    session.assert_not_called()
                    declaration = {"type": "function", "function": {"name": name}}
                    self.assertEqual(privacy.allowed_tools([declaration]), [])
        finally:
            privacy.reset(token)

    def test_enhanced_keeps_only_downloads_without_request_content(self) -> None:
        token = privacy.elevate(privacy.ENHANCED)
        try:
            self.assertTrue(privacy.allows(news_feeds.CAPABILITY))
            self.assertTrue(privacy.allows(fr_communes.CAPABILITY))
            self.assertFalse(privacy.allows(encyclopedia.CAPABILITY))
            self.assertFalse(privacy.allows(eu_statistics.CAPABILITY))
        finally:
            privacy.reset(token)


class ChatContractTests(unittest.IsolatedAsyncioTestCase):
    def test_relevance_in_realistic_phrasings(self) -> None:
        cases = {
            "quand est né jules marchetti ?": {"encyclopedia"},
            "c koi la date de naissance de Odile Brancourt": {"encyclopedia"},
            "when was Velmora Industries founded and who are its founders": {
                "encyclopedia"
            },
            "Wann wurde die Firma Kestrel Werke gegründet?": {"encyclopedia"},
            "en quelle annee a ete fonde le club de foot de Saint-Bellerive": {
                "encyclopedia"
            },
            "qui est le maire de bellerive en ce moment ?": {"commune", "encyclopedia"},
            "who is the mayor of Port Aldous": {"commune", "encyclopedia"},
            "combien d'habitants a Kervalec": {"commune", "statistics"},
            "quel est le taux de chomage en arvenie": {"statistics"},
            "inflation in the euro area last month?": {"statistics"},
            "Wie hoch ist die Staatsverschuldung von Arvenland?": {"statistics"},
            "le smic en valdorie c'est combien": {"statistics"},
            "quoi de neuf en valdorie aujourd'hui ?": {"news"},
            "c'est quoi les dernieres actus sur la reforme des retraites": {"news"},
            "any news about the strikes in Ostmark?": {"news"},
            "¿Qué noticias hay hoy de Montserral?": {"news"},
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertTrue(expected <= source_tools.relevant(question))
        # Recorded chat questions and other ordinary turns keep their requests.
        for question in (
            "Recherche sur le web : en quelle année Python a-t-il été publié pour la première fois ? Cite une source et sa date de consultation.",
            "Recherche sur le web puis lis une source : en quelle année le CERN a-t-il rendu le World Wide Web public ? Cite la source et la date de consultation.",
            "Réponds en une phrase : quelle est la capitale de la France ?",
            "qui est le PDG de Walmart",
            "quel est le prix de l'IPO de SpaceX",
            "Conçois une monnaie locale pour une ville de 50 000 habitants.",
            "Dans cette feuille de calcul, extrais les achats du mois de mars et crée un nouvel onglet avec un graphique",
            "Sur un Amstrad CPC, quels sont les timings de OUTI et OTIR en microsecondes ?",
            "Bonjour, discutons en français.",
            "écris une fonction javascript qui trie un tableau d'objets par date",
            "que se passe-t-il si je supprime ce fichier ?",
            "What's new in Python 3.14?",
        ):
            with self.subTest(question=question):
                self.assertEqual(source_tools.relevant(question), frozenset())
        self.assertEqual(
            source_tools.relevant("et à Kervalec ?", "qui est le maire de Bellerive"),
            frozenset({"commune", "encyclopedia"}),
        )

    def test_declarations_live_in_code(self) -> None:
        names = [
            tool["function"]["name"]  # type: ignore[index]
            for tool in source_tools.declarations(frozenset(source_tools.FAMILIES))
        ]
        self.assertEqual(
            names, ["french_commune", "eu_statistics", "encyclopedia", "news_headlines"]
        )
        self.assertEqual(source_tools.declarations(frozenset()), [])
        with open("prompts/tools.json") as handle:
            self.assertFalse(set(json.load(handle)) & set(source_tools.SCHEMAS))
        for name in source_tools.SCHEMAS:
            self.assertIn(name, privacy.TOOL_CAPABILITIES)

    async def test_invalid_arguments_return_the_schema(self) -> None:
        result = await source_tools.execute(
            "eu_statistics", '{"indicator": "happiness", "geo": "FR"}', 1, "fr"
        )
        self.assertEqual(result["error"], "invalid_arguments")
        self.assertIn("schema", result)

    async def test_chat_records_every_fetched_source(self) -> None:
        routes = {
            "query": json.dumps(SEARCH).encode(),
            "Q901": json.dumps(ENTITY).encode(),
            "Q910": json.dumps(LABELS).encode(),
        }
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict("os.environ", {"ATLAS_WEB_CACHE": root + "/cache.sqlite"}),
            patch.object(reference_fetch, "get", fake_get(routes)),
        ):
            item = Interaction()
            tools = ChatTools(item)
            tools.search_language = "fr"
            result = await tools.execute(
                Call("1", "encyclopedia", '{"query": "Lucienne Varnier"}'), 5
            )
        self.assertIn("data", result)
        self.assertEqual(
            item.retrieved_urls,
            [
                "https://fr.wikipedia.org/wiki/Lucienne_Varnier",
                "https://www.wikidata.org/wiki/Q901",
            ],
        )


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_unlisted_hosts_and_plain_http_are_refused(self) -> None:
        for url in ("https://example.org/x", "http://www.wikidata.org/w/api.php"):
            with self.subTest(url=url):
                with patch("aiohttp.ClientSession") as session:
                    with self.assertRaises(SourceError) as raised:
                        await reference_fetch.get(
                            url, capability="reference_lookup", timeout=1
                        )
                self.assertEqual(raised.exception.code, "source_not_allowed")
                session.assert_not_called()

    async def test_cached_bodies_are_not_fetched_again(self) -> None:
        url = "https://www.wikidata.org/w/api.php?action=x"
        reference_fetch.CACHE.put(url, b"{}", 60)
        try:
            with patch("aiohttp.ClientSession") as session:
                body = await reference_fetch.get(
                    "https://www.wikidata.org/w/api.php",
                    params={"action": "x"},
                    capability="reference_lookup",
                    timeout=1,
                )
            self.assertEqual(body, b"{}")
            session.assert_not_called()
        finally:
            reference_fetch.CACHE.drop(url)


KIWIX_SEARCH = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
<channel><title>Search: lucienne varnier</title>
<item><title>Pont Lucienne-Varnier</title>
<link>/content/wikipedia_fr_test_mini_2026-10/Pont_Lucienne-Varnier</link>
<description>...le pont <b>Lucienne</b>-<b>Varnier</b> enjambe...</description></item>
<item><title>Lucienne Varnier</title>
<link>/content/wikipedia_fr_test_mini_2026-10/Lucienne_Varnier</link>
<description><b>Lucienne</b> <b>Varnier</b>, ingenieure</description></item>
</channel></rss>"""
KIWIX_PAGE = """<html><body><table class="infobox"><tr><td>Naissance 1911</td></tr></table>
<p>Lucienne Varnier, née le 3 mars 1911 dans le 5<sup>e</sup> arrondissement de
Bellerive<sup class="reference">[1]</sup>, est une ingénieure.</p>
<p>Elle dirige le port.</p></body></html>""".encode()
KIWIX_ENV = {
    "ATLAS_ENCYCLOPEDIA_BACKEND": "local",
    "ATLAS_KIWIX_URL": "http://127.0.0.1:1",
    "ATLAS_KIWIX_BOOKS": "fr=wikipedia_fr_test_mini_2026-10,en=bad/../book",
}


class LocalMirrorTests(unittest.IsolatedAsyncioTestCase):
    def test_search_page_and_public_address(self) -> None:
        from services.orchestrator import kiwix

        hits = kiwix.parse_search(KIWIX_SEARCH)
        self.assertEqual(hits[0].snippet, "...le pont Lucienne-Varnier enjambe...")
        # The article titled as the query comes before a page naming it often.
        self.assertEqual(
            kiwix.best(hits, "lucienne varnier")[0].title, "Lucienne Varnier"
        )
        self.assertEqual(
            kiwix.introduction(KIWIX_PAGE, 200),
            "Lucienne Varnier, née le 3 mars 1911 dans le 5e arrondissement de "
            "Bellerive, est une ingénieure. Elle dirige le port.",
        )
        self.assertEqual(
            kiwix.public_url(
                "fr", "/content/wikipedia_fr_test_mini_2026-10/A/L%C3%A9a_Mor"
            ),
            "https://fr.wikipedia.org/wiki/L%C3%A9a_Mor",
        )
        with patch.dict("os.environ", KIWIX_ENV):
            # A malformed book name is ignored, never put in a URL.
            self.assertEqual(kiwix.books(), {"fr": "wikipedia_fr_test_mini_2026-10"})

    async def test_strict_reads_the_local_copy_only(self) -> None:
        from services.orchestrator import kiwix

        async def local(path: str, timeout: float) -> bytes:
            return KIWIX_SEARCH if path.startswith("/search?") else KIWIX_PAGE

        async def third_party(*args: object, **kwargs: object) -> bytes:
            raise AssertionError("no third-party request in strict mode")

        token = privacy.elevate(privacy.STRICT)
        try:
            with (
                patch.dict("os.environ", KIWIX_ENV),
                patch.object(kiwix, "_get", local),
                patch.object(reference_fetch, "get", third_party),
            ):
                declaration = {"type": "function", "function": {"name": "encyclopedia"}}
                self.assertEqual(privacy.allowed_tools([declaration]), [declaration])
                news = {"type": "function", "function": {"name": "news_headlines"}}
                self.assertEqual(privacy.allowed_tools([news]), [])
                result = await source_tools.execute(
                    "encyclopedia", '{"query": "Lucienne Varnier"}', 5, "fr"
                )
        finally:
            privacy.reset(token)
        data = result["data"]
        assert isinstance(data, dict)
        self.assertEqual(data["url"], "https://fr.wikipedia.org/wiki/Lucienne_Varnier")
        self.assertEqual(data["local_book"], "wikipedia_fr_test_mini_2026-10")
        self.assertNotIn("facts", data)
        self.assertIn("1911", data["summary"])

    async def test_local_backend_without_a_server_is_an_error(self) -> None:
        with patch.dict(
            "os.environ", {"ATLAS_ENCYCLOPEDIA_BACKEND": "local", "ATLAS_KIWIX_URL": ""}
        ):
            result = await source_tools.execute(
                "encyclopedia", '{"query": "Lucienne Varnier"}', 5, "fr"
            )
        self.assertEqual(result["error"], "local_mirror_unavailable")


if __name__ == "__main__":
    unittest.main()
