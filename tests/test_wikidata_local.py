"""Local Wikidata facts: the extract built from a dump, its reader, the encyclopedia."""

import importlib.util
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from test_source_tools import (
    ENTITY,
    KIWIX_ENV,
    LABELS,
    NO_KIWIX_ENV,
    SEARCH,
    AtLevel,
    fake_get,
    item,
    kiwix_ok,
    live_down,
    third_party_unused,
)

from packages import privacy
from services.orchestrator import reference_fetch, source_tools
from services.orchestrator import wikidata_local

ROOT = Path(__file__).resolve().parents[1]


def _extractor() -> Any:
    spec = importlib.util.spec_from_file_location(
        "wikidata_extract", ROOT / "tools" / "wikidata_extract.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _labels(texts: dict[str, str]) -> dict[str, object]:
    return {k: {"language": k, "value": v} for k, v in texts.items()}


def _sitelinks(**titles: str) -> dict[str, object]:
    return {site: {"site": site, "title": title} for site, title in titles.items()}


def _line(entity: dict[str, Any]) -> bytes:
    """A dump line: the keys in the dump's order (labels first, sitelinks last)."""
    ordered = {
        "type": "item",
        "id": entity["id"],
        "labels": entity.get("labels", {}),
        "descriptions": entity.get("descriptions", {}),
        "aliases": {},
        "claims": entity.get("claims", {}),
        "sitelinks": entity.get("sitelinks", {}),
        "lastrevid": 1,
    }
    if "modified" in entity:
        ordered["modified"] = entity["modified"]
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":")).encode()


# The fictional person of test_source_tools (same claims, so the live and local paths
# can be compared), her town, an office and an occupation without an article, a
# category and an item with an article on another edition only.
PERSON = {
    **ENTITY["entities"]["Q901"],
    "id": "Q901",
    "sitelinks": _sitelinks(frwiki="Lucienne Varnier", enwiki="Lucienne Varnier"),
}
PERSON["labels"] = _labels({"fr": "Lucienne Varnier"})
PERSON["descriptions"] = _labels(
    {"fr": "ingénieure fictive", "en": "fictional engineer"}
)
DUMP_ENTITIES: list[dict[str, Any]] = [
    # Named by the person's facts, before her in the dump, no article.
    {"id": "Q920", "labels": LABELS["entities"]["Q920"]["labels"]},
    PERSON,
    {
        "id": "Q910",
        "labels": _labels({"fr": "Bellerive", "de": "Bellerive"}),
        "sitelinks": _sitelinks(frwiki="Bellerive", nlwiki="Bellerive"),
        "claims": {"P112": [{"rank": "normal", "mainsnak": item("Q970")}]},
        "modified": "2026-11-05T08:00:00Z",
    },
    # An older copy of the town's article link on another item (the dump is written
    # while Wikidata is edited), and the person a second time.
    {
        "id": "Q960",
        "labels": _labels({"fr": "Bellerive (ancien)"}),
        "sitelinks": _sitelinks(frwiki="Bellerive"),
        "modified": "2026-10-01T08:00:00Z",
    },
    PERSON,
    {
        "id": "Q940",
        "labels": _labels({"fr": "Catégorie:Bellerive"}),
        "sitelinks": _sitelinks(frwiki="Catégorie:Bellerive"),
    },
    # A founder named by the town, after it in the dump, whose name is shared by all
    # languages: Wikidata keeps it under "mul" only (no "en").
    {"id": "Q970", "labels": _labels({"mul": "Kestrel Werke"})},
    {
        "id": "Q950",
        "labels": _labels({"nl": "Kervalec"}),
        "sitelinks": _sitelinks(nlwiki="Kervalec"),
    },
]


def dump_bytes(complete: bool = True) -> bytes:
    lines = [b"["]
    rows = [_line(e) for e in DUMP_ENTITIES]
    rows.insert(1, b'{"type":"property","id":"P31","labels":{},"descriptions":{}}')
    lines += [row + b"," for row in rows[:-1]] + [rows[-1]]
    if complete:
        lines.append(b"]")
    return b"\n".join(lines) + b"\n"


def build(out: Path, data: bytes) -> int:
    stdin = io.TextIOWrapper(io.BytesIO(data))
    with patch("sys.stdin", stdin), patch("sys.stderr", io.StringIO()):
        code: int = _extractor().main(
            ["--out", str(out), "--dump-date", "2026-11-09", "--source", "test"]
        )
    return code


class ExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "facts.sqlite"

    def test_articles_and_the_items_their_facts_name(self) -> None:
        self.assertEqual(build(self.out, dump_bytes()), 0)
        db = sqlite3.connect(self.out)
        self.assertEqual(
            sorted(db.execute("SELECT site, title, qid FROM sitelink")),
            [
                ("enwiki", "Lucienne Varnier", 901),
                ("frwiki", "Bellerive", 910),
                ("frwiki", "Lucienne Varnier", 901),
            ],
        )
        # A category and an item without an article in the five editions are left out.
        self.assertEqual(
            [r[0] for r in db.execute("SELECT qid FROM entity ORDER BY qid")],
            [901, 910, 960],
        )
        # The office has no article but a fact names it: its label is kept.
        labels = dict(db.execute("SELECT qid, labels FROM label"))
        self.assertEqual(json.loads(labels[920]), {"en": "mayor of Bellerive"})
        self.assertNotIn(950, labels)
        meta = dict(db.execute("SELECT key, value FROM meta"))
        self.assertEqual(meta["dump_date"], "2026-11-09")
        self.assertEqual(meta["count_entities"], "3")
        # A title listed twice stays with the item modified last (the person's two
        # copies give two more duplicate rows).
        self.assertEqual(meta["count_duplicate_titles_dropped"], "3")
        self.assertFalse(Path(str(self.out) + ".pending-labels.gz").exists())
        db.close()

    def test_a_failed_finish_can_be_run_again(self) -> None:
        extract = _extractor()
        with patch.object(
            extract.Builder, "one_item_per_title", side_effect=sqlite3.OperationalError
        ):
            stdin = io.TextIOWrapper(io.BytesIO(dump_bytes()))
            with patch("sys.stdin", stdin), patch("sys.stderr", io.StringIO()):
                code = extract.main(
                    ["--out", str(self.out), "--dump-date", "2026-11-09"]
                )
        self.assertEqual(code, 4)
        self.assertTrue(Path(str(self.out) + ".pending-labels.gz").exists())
        with patch("sys.stderr", io.StringIO()):
            code = extract.main(
                ["--out", str(self.out), "--dump-date", "2026-11-09", "--finish-only"]
            )
        self.assertEqual(code, 0)
        db = sqlite3.connect(self.out)
        labels = dict(db.execute("SELECT qid, labels FROM label"))
        self.assertEqual(json.loads(labels[920]), {"en": "mayor of Bellerive"})
        self.assertEqual(
            db.execute(
                "SELECT qid FROM sitelink WHERE site = 'frwiki' AND title = 'Bellerive'"
            ).fetchall(),
            [(910,)],
        )
        db.close()

    def test_a_truncated_dump_is_refused(self) -> None:
        self.assertEqual(build(self.out, dump_bytes(complete=False)), 3)

    def test_namespace_titles(self) -> None:
        extract = _extractor()
        self.assertFalse(extract.article_title("fr", "Catégorie:Bellerive"))
        self.assertFalse(extract.article_title("de", "Category:Bellerive"))
        self.assertTrue(extract.article_title("en", "Kestrel: A Novel"))


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "facts.sqlite"
        self.assertEqual(build(self.out, dump_bytes()), 0)
        self.env = {**KIWIX_ENV, wikidata_local.ENV: str(self.out)}

    def test_lookup_by_title_resolves_the_named_items(self) -> None:
        with patch.dict(os.environ, self.env):
            entry = wikidata_local.lookup("frwiki", "Lucienne_Varnier", ["fr"])
        assert entry is not None
        self.assertEqual(entry.qid, "Q901")
        self.assertEqual(entry.dump_date, "2026-11-09")
        self.assertEqual(entry.description, "ingénieure fictive")
        self.assertEqual(
            entry.labels, {"Q910": "Bellerive", "Q920": "mayor of Bellerive"}
        )
        self.assertEqual(entry.facts["date_of_birth"], "1911-03-03")

    def test_a_mul_label_names_the_item_in_every_language(self) -> None:
        for language in ("fr", "en", "it"):
            with self.subTest(language=language), patch.dict(os.environ, self.env):
                entry = wikidata_local.lookup("frwiki", "Bellerive", [language])
                assert entry is not None
                self.assertEqual(entry.labels, {"Q970": "Kestrel Werke"})

    def test_absent_extract_or_article_gives_nothing(self) -> None:
        missing = str(Path(self.tmp.name) / "none.sqlite")
        for env, site, title in (
            ({wikidata_local.ENV: ""}, "frwiki", "Lucienne Varnier"),
            ({wikidata_local.ENV: missing}, "frwiki", "Lucienne Varnier"),
            (self.env, "frwiki", "Odile Brancourt"),
            (self.env, "nlwiki", "Kervalec"),
            (self.env, "bad site", "Lucienne Varnier"),
        ):
            with self.subTest(env=env, title=title), patch.dict(os.environ, env):
                self.assertIsNone(wikidata_local.lookup(site, title, ["fr"]))

    async def test_strict_facts_come_from_the_extract_and_match_the_live_ones(
        self,
    ) -> None:
        from services.orchestrator import kiwix

        routes = {
            "query": json.dumps(SEARCH).encode(),
            "Q901": json.dumps(ENTITY).encode(),
            "Q910": json.dumps(LABELS).encode(),
        }
        with (
            patch.dict(os.environ, NO_KIWIX_ENV),
            patch.object(reference_fetch, "get", fake_get(routes)),
        ):
            live = (
                await source_tools.execute(
                    "encyclopedia", '{"query": "Lucienne Varnier"}', 5, "fr"
                )
            )["data"]
        with (
            patch.dict(os.environ, self.env),
            patch.object(kiwix, "_fetch", kiwix_ok),
            patch.object(reference_fetch, "get", third_party_unused),
            # No connection at all: the extract is a file read.
            patch("aiohttp.ClientSession", side_effect=AssertionError("network")),
            AtLevel(privacy.STRICT),
        ):
            local = (
                await source_tools.execute(
                    "encyclopedia", '{"query": "Lucienne Varnier"}', 5, "fr"
                )
            )["data"]
        assert isinstance(live, dict) and isinstance(local, dict)
        self.assertEqual(local["facts"], live["facts"])
        facts = local["facts"]
        assert isinstance(facts, dict)
        # Qualifiers survive the extract: office dates, point in time of a figure.
        self.assertEqual(facts["positions_held"][1]["start"], "1950-06-01")
        self.assertEqual(facts["population"]["point_in_time"], "2024-01-01")
        self.assertEqual(local["description"], live["description"])
        self.assertEqual(local["wikidata_url"], live["wikidata_url"])
        self.assertEqual(local["wikidata_modified"], "2026-11-01T10:00:00Z")
        self.assertEqual(local["url"], "https://fr.wikipedia.org/wiki/Lucienne_Varnier")
        self.assertEqual(local["facts_date"], "2026-11-09")
        self.assertIn("dump of 2026-11-09", str(local["note"]))
        self.assertNotIn("summary only", str(local["note"]))

    async def test_standard_fallback_reads_the_extract_too(self) -> None:
        from services.orchestrator import kiwix

        with (
            patch.dict(os.environ, self.env),
            patch.object(kiwix, "_fetch", kiwix_ok),
            patch.object(reference_fetch, "get", live_down),
        ):
            result = await source_tools.execute(
                "encyclopedia", '{"query": "Lucienne Varnier"}', 5, "fr"
            )
        data = result["data"]
        assert isinstance(data, dict)
        self.assertEqual(data["live_error"], "source_unavailable")
        facts = data["facts"]
        assert isinstance(facts, dict)
        self.assertEqual(facts["place_of_birth"], "Bellerive")
        self.assertNotIn("were not fetched", str(data["note"]))

    async def test_unreadable_extract_keeps_the_summary(self) -> None:
        from services.orchestrator import kiwix

        broken = Path(self.tmp.name) / "broken.sqlite"
        broken.write_bytes(b"not a database" * 100)
        with (
            patch.dict(os.environ, {**self.env, wikidata_local.ENV: str(broken)}),
            patch.object(kiwix, "_fetch", kiwix_ok),
            patch.object(reference_fetch, "get", third_party_unused),
            AtLevel(privacy.STRICT),
            self.assertLogs("services.orchestrator.encyclopedia", "WARNING"),
        ):
            result = await source_tools.execute(
                "encyclopedia", '{"query": "Lucienne Varnier"}', 5, "fr"
            )
        data = result["data"]
        assert isinstance(data, dict)
        self.assertNotIn("facts", data)
        self.assertIn("not available at this privacy level", str(data["note"]))


if __name__ == "__main__":
    unittest.main()
