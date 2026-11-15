"""Search cache identity, lifetime and the daily search health report."""

import calendar
import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType

from services.orchestrator import search_freshness as sf

ROOT = Path(__file__).resolve().parents[1]
NOW = calendar.timegm((2026, 11, 15, 12, 0, 0))


def _report() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "search_health_report", ROOT / "tools" / "search_health_report.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NormalizedQueryTests(unittest.TestCase):
    def same(self, language: str, *queries: str) -> None:
        keys = {sf.normalized_query(query, language) for query in queries}
        self.assertEqual(len(keys), 1, keys)

    def different(self, language: str, first: str, second: str) -> None:
        self.assertNotEqual(
            sf.normalized_query(first, language), sf.normalized_query(second, language)
        )

    def test_case_accents_punctuation_stop_words_and_order(self) -> None:
        self.same(
            "fr",
            "Prix de l'électricité en France",
            "prix electricite france",
            "France, prix de l’électricité ?",
            "PRIX ÉLECTRICITÉ FRANCE",
        )
        self.same("en", "history of the Lumiere brothers", "Lumière brothers history")
        self.same("de", "Geschichte der Stadt Ulm", "Ulm Stadt Geschichte")
        self.same("es", "receta de la paella", "paella receta")
        self.same("it", "ricetta della pasta al forno", "pasta forno ricetta")
        self.same("fr", "prix moyen du m² à Lyon 2026", "Prix moyen m2 Lyon 2026")
        self.same(
            "fr",
            "Recettes végétariennes populaires !",
            "recettes végétariennes populaires",
        )
        self.same("en", "how to cook rice", "cook rice: how to")

    def test_words_that_change_the_answer_are_kept(self) -> None:
        self.different("en", "when did the bridge open", "where did the bridge open")
        self.different("fr", "pain sans gluten", "pain gluten")
        self.different("en", "laptops without fan", "laptops fan")
        self.different("en", "train from Paris to Lyon", "train from Lyon to Paris")
        self.different("fr", "train de Paris à Lyon", "train de Lyon à Paris")
        self.different("de", "Zug von Ulm nach Bern", "Zug von Bern nach Ulm")
        self.different(
            "en", "translate english into french", "translate french into english"
        )
        self.different("en", "python 3.12 release", "python 3.13 release")
        self.different("en", "c++ tutorial", "c tutorial")

    def test_exact_phrases_and_operators_keep_their_order(self) -> None:
        self.different("en", '"red green" paint', '"green red" paint')
        self.different("en", "site:example.org news", "news site:example.org")
        self.different("en", "!wp bridge history", "history bridge wp")
        self.same("en", '"Red Green" paint', '"red green" PAINT')

    def test_only_stop_words_keep_every_word(self) -> None:
        self.assertEqual(sf.normalized_query("The The", "en"), "the")
        self.assertEqual(sf.normalized_query("?!", "en"), "?!")


class TimeBoundTests(unittest.TestCase):
    def test_news_flag_and_date_words_in_five_languages(self) -> None:
        self.assertTrue(sf.time_bound("history of the printing press", True, NOW))
        for query in (
            "weather in Lyon tomorrow",
            "latest firmware for the router",
            "météo Lyon demain",
            "prix de l'électricité aujourd'hui",
            "Wetter heute in Ulm",
            "noticias de hoy",
            "notizie di oggi",
            "who is the current CEO of the company",
            "best laptops 2026",
            "election results 2025",
            "recettes végétariennes populaires en ce moment",
        ):
            self.assertTrue(sf.time_bound(query, False, NOW), query)

    def test_settled_facts_are_not_time_bound(self) -> None:
        for query in (
            "history of the printing press",
            "founding year of the local currency association 2011",
            "recette de la pâte brisée",
            "Satz des Pythagoras Beweis",
            "how does a tube preamp work",
        ):
            self.assertFalse(sf.time_bound(query, False, NOW), query)

    def test_lifetimes(self) -> None:
        self.assertEqual(sf.cache_ttl(bound=False, degraded=False), 3 * 86400)
        self.assertEqual(sf.cache_ttl(bound=True, degraded=False), 3600)
        self.assertEqual(sf.cache_ttl(bound=False, degraded=True), 3600)
        self.assertEqual(sf.cache_ttl(bound=True, degraded=True), 60)


class SearchHealthReportTests(unittest.TestCase):
    def test_rates_per_engine_cache_hits_and_fallbacks(self) -> None:
        report = _report()
        outcome = {
            "provider": "searxng",
            "outcome": "partial_results",
            "engines": [
                {"engine": "startpage", "outcome": "upstream_blocked"},
                {"engine": "duckduckgo", "outcome": "ok"},
                {
                    "engine": "mojeek",
                    "outcome": "upstream_blocked",
                    "reason": "cooldown",
                },
                {"engine": "wikipedia", "outcome": "empty_results"},
            ],
            "waited_seconds": 1.5,
        }
        lines = [
            "searxng_outcome " + json.dumps(outcome),
            "searxng_outcome "
            + json.dumps({**outcome, "outcome": "ok", "waited_seconds": 0}),
            'searxng_cache {"result": "hit", "time_bound": false}',
            'searxng_cache {"result": "shared", "time_bound": true}',
            'searxng_engine_demoted {"engine": "startpage", "asked": 4}',
            json.dumps(
                {
                    "event": "search_chain",
                    "attempts": [
                        {"provider": "searxng", "outcome": "upstream_blocked"},
                        {"provider": "serpapi", "outcome": "ok"},
                    ],
                }
            ),
            "unrelated line",
        ]
        summary = report.summarize(lines)
        self.assertEqual(summary["searches"], 2)
        self.assertEqual(summary["lookups"], 4)
        self.assertEqual(summary["hit_rate"], 0.5)
        self.assertEqual(
            summary["engines"]["startpage"], {"asked": 2, "upstream_blocked": 2}
        )
        self.assertEqual(summary["engines"]["mojeek"], {"skipped": 2})
        self.assertEqual(summary["demoted"], {"startpage": 1})
        self.assertEqual(summary["fallbacks"], {"serpapi": 1})
        text = report.markdown(summary, "staging")
        self.assertIn("hit rate 50%", text)
        self.assertIn("startpage: asked 2, ok 0%, blocked 100%", text)
        self.assertIn("no web search", report.markdown(report.summarize([]), "x"))


if __name__ == "__main__":
    unittest.main()
