#!/usr/bin/env python3
"""Build the local Wikidata facts extract read by services/orchestrator/wikidata_local.py.

Input: the Wikidata JSON dump (https://dumps.wikimedia.org/wikidatawiki/entities/,
``wikidata-YYYYMMDD-all.json.gz``), decompressed, on stdin: one entity per line. The
dump is never stored; a monthly job streams it from a mirror:

    curl -sf URL | pigz -dc | python tools/wikidata_extract.py --out NEW.sqlite \
        --dump-date 2026-11-09 --source URL

Output: one SQLite file (docs/23-specialized-sources.md, "Local Wikidata facts"):

- ``entity``: every item with an article on one of the configured Wikipedia editions
  (``--sites``, main namespace only), its facts, descriptions and modification date.
  Facts are computed by ``encyclopedia.raw_facts`` on the dump's JSON, which has the
  shape of ``wbgetentities``: the same properties, ranks and qualifiers (start and end
  dates of offices, point in time of a population) as the live path, with the items
  they name left as ``{"item": "Q..."}`` references;
- ``label``: labels of those items and of every item their facts name (a place, an
  occupation, a unit), in the configured languages plus ``mul``;
- ``sitelink``: article titles by edition, indexed by (site, title), one item per
  title (the item modified last when the dump lists a title under two items);
- ``meta``: dump date, source, counts, schema version.

Labels of items without an article are kept in a compressed temporary file next to the
output while the dump streams by (a fact can name an item that comes later in the
dump), then only the named ones are inserted and the file is deleted.
"""

from __future__ import annotations

import argparse
import gzip
import importlib
import json
import os
import re
import resource
import sqlite3
import sys
import time
import traceback
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.orchestrator.encyclopedia import raw_facts  # noqa: E402

SCHEMA_VERSION = "1"
LANGUAGES = ("fr", "en", "de", "es", "it")
# Namespace prefixes of non-article pages (canonical English names work on every
# edition; local names per language). An item whose only articles are such pages
# (categories, templates, modules, portals) is left out: the mirror has articles only.
_CANONICAL = {
    "Book",
    "Category",
    "Draft",
    "File",
    "Help",
    "Image",
    "MediaWiki",
    "Module",
    "Portal",
    "Project",
    "Special",
    "Template",
    "TimedText",
    "User",
    "Wikipedia",
}
_LOCAL_NS = {
    "fr": {
        "Aide",
        "Catégorie",
        "Fichier",
        "Modèle",
        "Portail",
        "Projet",
        "Référence",
        "Utilisateur",
        "Wikipédia",
    },
    "de": {"Benutzer", "Datei", "Hilfe", "Kategorie", "Modul", "Vorlage"},
    "es": {
        "Anexo",
        "Archivo",
        "Ayuda",
        "Categoría",
        "Módulo",
        "Plantilla",
        "Usuario",
        "Wikiproyecto",
    },
    "it": {"Aiuto", "Categoria", "Modulo", "Portale", "Progetto", "Utente"},
    "en": set(),
}
_QID = re.compile(rb'"id":"Q(\d+)"')
_REF = re.compile(r'"(?:item|unit_item)":"Q(\d+)"')
BATCH = 2000


def _loader() -> Callable[[bytes], Any]:
    """orjson when installed (several times faster on the dump), else the stdlib."""
    try:
        module = importlib.import_module("orjson")
    except ImportError:
        return json.loads
    loads: Callable[[bytes], Any] = module.loads
    return loads


def article_title(language: str, title: str) -> bool:
    prefix, colon, _ = title.partition(":")
    if not colon:
        return True
    return prefix not in _CANONICAL and prefix not in _LOCAL_NS.get(language, set())


def _texts(values: object, languages: Iterable[str]) -> dict[str, str]:
    if not isinstance(values, dict):
        return {}
    out: dict[str, str] = {}
    for language in languages:
        value = values.get(language)
        if isinstance(value, dict) and isinstance(value.get("value"), str):
            out[language] = value["value"]
    return out


def _labels_slice(line: bytes) -> bytes | None:
    """The raw ``labels`` object of a dump line, without parsing the whole line.

    Inside a JSON string every quote is escaped, so ``},"descriptions":`` can only be
    the end of the labels object followed by the next key."""
    start = line.find(b'"labels":')
    if start < 0 or start > 200:
        return None
    end = line.find(b',"descriptions":', start)
    if end < 0:
        return None
    return line[start + 9 : end]


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE entity(
  qid INTEGER PRIMARY KEY,
  facts TEXT NOT NULL,
  descriptions TEXT NOT NULL,
  modified TEXT
);
CREATE TABLE label(qid INTEGER PRIMARY KEY, labels TEXT NOT NULL);
CREATE TABLE sitelink(site TEXT NOT NULL, title TEXT NOT NULL, qid INTEGER NOT NULL);
"""
INDEXES = "CREATE INDEX IF NOT EXISTS sitelink_title ON sitelink(site, title);"


class Builder:
    def __init__(
        self,
        out: Path,
        sites: tuple[str, ...],
        log: Callable[[str], None],
        resume: bool = False,
    ):
        self.out = out
        self.sites = sites
        self.label_languages = (*sites, "mul")
        self.site_keys = [f'"{s}wiki":{{'.encode() for s in sites]
        self.log = log
        self.loads = _loader()
        self.db = sqlite3.connect(out, isolation_level=None)
        for pragma in (
            "journal_mode=OFF",
            "synchronous=OFF",
            "cache_size=-262144",
            "temp_store=FILE",
            "page_size=8192",
        ):
            self.db.execute(f"PRAGMA {pragma}")
        self.pending_path = out.with_name(out.name + ".pending-labels.gz")
        self.pending: TextIO | None = None
        self.refs: set[int] = set()
        if resume:
            # A stream already read (--finish-only): the named items come from the
            # stored facts, the other labels from the pending file left beside it.
            for (facts,) in self.db.execute("SELECT facts FROM entity"):
                self.refs.update(int(ref) for ref in _REF.findall(facts))
        else:
            self.db.executescript(SCHEMA)
            self.pending = gzip.open(
                self.pending_path, "wt", compresslevel=1, encoding="utf-8"
            )
        self.entities: list[tuple[int, str, str, str | None]] = []
        self.labels: list[tuple[int, str]] = []
        self.links: list[tuple[str, str, int]] = []
        self.counts = {"lines": 0, "items": 0, "kept": 0, "sitelinks": 0, "pending": 0}
        self.bytes = 0
        self.complete = False

    def flush(self) -> None:
        self.db.execute("BEGIN")
        # The dump is not a snapshot: an item can appear twice; the later copy wins.
        self.db.executemany(
            "INSERT OR REPLACE INTO entity VALUES (?,?,?,?)", self.entities
        )
        self.db.executemany("INSERT OR REPLACE INTO label VALUES (?,?)", self.labels)
        self.db.executemany("INSERT INTO sitelink VALUES (?,?,?)", self.links)
        self.db.execute("COMMIT")
        self.entities.clear()
        self.labels.clear()
        self.links.clear()

    def line(self, raw: bytes) -> None:
        self.counts["lines"] += 1
        self.bytes += len(raw)
        if not raw.startswith(b'{"type":"item"'):
            if raw.startswith(b"]"):
                self.complete = True
            return
        match = _QID.search(raw, 0, 80)
        if match is None:
            return
        qid = int(match.group(1))
        self.counts["items"] += 1
        tail_at = raw.rfind(b'"sitelinks":')
        tail = raw[tail_at:] if tail_at >= 0 else b""
        if tail and any(key in tail for key in self.site_keys):
            if self.article(qid, raw):
                return
        piece = _labels_slice(raw)
        if piece is None:
            return
        try:
            labels = _texts(self.loads(piece), self.label_languages)
        except ValueError:
            return
        if labels and self.pending is not None:
            self.pending.write(f"{qid}\t{_dumps(labels)}\n")
            self.counts["pending"] += 1

    def article(self, qid: int, raw: bytes) -> bool:
        """Store an item with at least one main-namespace article; False otherwise."""
        entity = self.loads(raw.rstrip(b",\r\n"))
        sitelinks = entity.get("sitelinks")
        if not isinstance(sitelinks, dict):
            return False
        titles: list[tuple[str, str]] = []
        for language in self.sites:
            link = sitelinks.get(f"{language}wiki")
            title = link.get("title") if isinstance(link, dict) else None
            if isinstance(title, str) and article_title(language, title):
                titles.append((f"{language}wiki", title))
        if not titles:
            return False
        facts, refs = raw_facts(entity)
        self.refs.update(int(ref[1:]) for ref in refs)
        modified = entity.get("modified")
        self.entities.append(
            (
                qid,
                _dumps(facts),
                _dumps(_texts(entity.get("descriptions"), self.sites)),
                modified if isinstance(modified, str) else None,
            )
        )
        self.labels.append(
            (qid, _dumps(_texts(entity.get("labels"), self.label_languages)))
        )
        self.links.extend((site, title, qid) for site, title in titles)
        self.counts["kept"] += 1
        self.counts["sitelinks"] += len(titles)
        if len(self.entities) >= BATCH:
            self.flush()
        return True

    def finish(self, meta: dict[str, str]) -> dict[str, int]:
        self.flush()
        if self.pending is not None:
            self.pending.close()
        self.log(f"stream done: {self.counts}; named items to label: {len(self.refs)}")
        labelled = {row[0] for row in self.db.execute("SELECT qid FROM label")}
        wanted = self.refs - labelled
        del labelled
        added = 0
        rows: list[tuple[int, str]] = []
        with gzip.open(self.pending_path, "rt", encoding="utf-8") as pending:
            for row in pending:
                qid_text, _, labels = row.rstrip("\n").partition("\t")
                qid = int(qid_text)
                if qid in wanted:
                    rows.append((qid, labels))
                    wanted.discard(qid)
                    if len(rows) >= 10_000:
                        self.labels.extend(rows)
                        added += len(rows)
                        rows.clear()
                        self.flush()
        self.labels.extend(rows)
        added += len(rows)
        self.flush()
        self.log(f"labels of named items added: {added}; still unnamed: {len(wanted)}")
        self.log("building the title index")
        self.db.execute(INDEXES)
        dropped = self.one_item_per_title()
        self.log(f"titles claimed by two items resolved: {dropped} row(s) dropped")
        counts = {
            "entities": self.db.execute("SELECT count(*) FROM entity").fetchone()[0],
            "sitelinks": self.db.execute("SELECT count(*) FROM sitelink").fetchone()[0],
            "labels": self.db.execute("SELECT count(*) FROM label").fetchone()[0],
            "items_read": self.counts["items"],
            "named_without_label": len(wanted),
            "duplicate_titles_dropped": dropped,
        }
        rows_meta = {
            **meta,
            "schema_version": SCHEMA_VERSION,
            "sites": ",".join(f"{s}wiki" for s in self.sites),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **{f"count_{k}": str(v) for k, v in counts.items()},
        }
        self.db.executemany(
            "INSERT OR REPLACE INTO meta VALUES (?,?)", sorted(rows_meta.items())
        )
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("ANALYZE")
        self.db.close()
        # Kept until here so that a failed finish can be run again (--finish-only).
        self.pending_path.unlink()
        return counts

    def one_item_per_title(self) -> int:
        """Keep one item per article title. The dump is written while Wikidata is
        edited, so a title moved from one item to another (a merge, a fix) can appear
        under both: the item modified last holds it."""
        dups = self.db.execute(
            "SELECT site, title FROM sitelink GROUP BY site, title HAVING count(*) > 1"
        ).fetchall()
        dropped = 0
        self.db.execute("BEGIN")
        for site, title in dups:
            rows = self.db.execute(
                "SELECT s.rowid, s.qid, e.modified FROM sitelink s "
                "LEFT JOIN entity e ON e.qid = s.qid WHERE s.site = ? AND s.title = ?",
                (site, title),
            ).fetchall()
            keep = max(rows, key=lambda row: (row[2] or "", row[1]))
            for row in rows:
                if row[0] != keep[0]:
                    self.db.execute("DELETE FROM sitelink WHERE rowid = ?", (row[0],))
                    dropped += 1
        self.db.execute("COMMIT")
        return dropped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--dump-date", required=True, help="YYYY-MM-DD of the dump")
    parser.add_argument("--source", default="", help="URL of the dump read")
    parser.add_argument("--sites", default=",".join(LANGUAGES))
    parser.add_argument("--progress", type=float, default=300.0, help="seconds")
    parser.add_argument(
        "--allow-partial", action="store_true", help="accept input without the final ]"
    )
    parser.add_argument(  # exit 4 of a stream run leaves what this needs
        "--finish-only",
        action="store_true",
        help="finish an --out whose stream was read (labels, index, meta); no stdin",
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", args.dump_date):
        parser.error("--dump-date is YYYY-MM-DD")
    sites = tuple(s.strip() for s in args.sites.split(",") if s.strip())
    if args.out.exists() != args.finish_only:
        parser.error(f"{args.out} {'exists' if args.out.exists() else 'is missing'}")
    # SQLite's sort files (index build) go next to the output, not to /var/tmp.
    os.environ.setdefault("SQLITE_TMPDIR", str(args.out.resolve().parent))

    def log(text: str) -> None:
        print(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {text}", file=sys.stderr, flush=True
        )

    started = time.monotonic()
    meta = {"dump_date": args.dump_date, "source": args.source, "license": "CC0 1.0"}
    if args.finish_only:
        counts = Builder(args.out, sites, log, resume=True).finish(meta)
        log(f"finished in {time.monotonic() - started:.0f} s: {counts}")
        return 0
    builder = Builder(args.out, sites, log)
    log(
        f"reading the dump on stdin ({'orjson' if builder.loads is not json.loads else 'json'})"
    )
    next_report = started + args.progress
    stdin = sys.stdin.buffer
    for raw in stdin:
        builder.line(raw)
        if builder.counts["lines"] % 20_000 == 0 and time.monotonic() > next_report:
            next_report = time.monotonic() + args.progress
            elapsed = time.monotonic() - started
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
            log(
                f"{builder.counts} {builder.bytes / 1e9:.1f} GB "
                f"{builder.bytes / 1e6 / elapsed:.0f} MB/s peak RSS {rss} MB"
            )
    if not builder.complete and not args.allow_partial:
        if builder.pending is not None:
            builder.pending.close()
        builder.db.close()
        log("input ended before the closing ] of the dump: incomplete, nothing kept")
        return 3
    try:
        counts = builder.finish(meta)
    except (sqlite3.Error, OSError, ValueError):
        # The stream's output and the pending labels stay: --finish-only resumes.
        log("finish failed after the whole dump was read:\n" + traceback.format_exc())
        return 4
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    log(
        f"done in {time.monotonic() - started:.0f} s: {counts}; "
        f"{os.path.getsize(args.out) / 1e9:.2f} GB; peak RSS {rss} MB"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
