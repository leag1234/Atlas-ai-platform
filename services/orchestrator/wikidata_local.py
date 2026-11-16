"""Wikidata facts from the local extract: a read-only SQLite file, no network.

The extract (``ATLAS_WIKIDATA_LOCAL``, built monthly from the Wikidata JSON dump by
``tools/wikidata_extract.py``, docs/23-specialized-sources.md) holds, for every item
with an article on one of the mirrored Wikipedia editions, the facts the live path
reads (``encyclopedia.raw_facts`` on the dump's JSON: the same properties, ranks and
qualifiers), its descriptions, and the labels of the items those facts name. A lookup
is by article title, as the local mirror (kiwix) gives it. The file is opened read-only
for each lookup, so a refreshed extract renamed over the old one is read at once.

An unset path or a missing file means no local facts: the encyclopedia then answers
from the article summary, as before the extract existed.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from urllib.parse import quote

ENV = "ATLAS_WIKIDATA_LOCAL"
_SITE = re.compile(r"[a-z]{2,3}wiki")
_QID = re.compile(r"Q\d+")
_CHUNK = 400


class LocalFactsError(Exception):
    """The extract exists but cannot be read (corrupt, wrong schema)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Entry:
    """One item of the extract: facts with item references unresolved, and the labels
    of those references in the first language available."""

    qid: str
    facts: dict[str, object]
    labels: dict[str, str]
    description: str
    modified: str | None
    dump_date: str


def path() -> str:
    return os.environ.get(ENV, "").strip()


def configured() -> bool:
    file = path()
    return bool(file) and os.path.isfile(file)


def _titles(title: str) -> list[str]:
    """The title as given, then as Wikipedia stores it (spaces, first letter upper)."""
    plain = " ".join(title.replace("_", " ").split())
    upper = plain[:1].upper() + plain[1:]
    return list(dict.fromkeys(t for t in (title, plain, upper) if t))


def _refs(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key in ("item", "unit_item"):
            ref = value.get(key)
            if isinstance(ref, str) and _QID.fullmatch(ref):
                yield ref
        for inner in value.values():
            if isinstance(inner, dict | list):
                yield from _refs(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _refs(inner)


def _pick(texts: object, languages: Iterable[str]) -> str:
    if not isinstance(texts, dict):
        return ""
    for language in [*languages, "mul", "en"]:
        value = texts.get(language)
        if isinstance(value, str) and value:
            return value
    return ""


def _connect(file: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(file)}?mode=ro", uri=True, timeout=2.0)


def lookup(site: str, title: str, languages: list[str]) -> Entry | None:
    """The item whose article on ``site`` (``frwiki``) is ``title``, or None when no
    extract is configured or it has no such article."""
    file = path()
    if not file or not os.path.isfile(file) or not _SITE.fullmatch(site):
        return None
    try:
        db = _connect(file)
    except sqlite3.Error as exc:
        raise LocalFactsError("local_facts_unavailable", str(exc)) from None
    try:
        return _lookup(db, site, title, languages)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise LocalFactsError(
            "local_facts_unavailable",
            f"the local Wikidata extract is unreadable: {exc}",
        ) from None
    finally:
        db.close()


def _lookup(
    db: sqlite3.Connection, site: str, title: str, languages: list[str]
) -> Entry | None:
    qid: int | None = None
    for candidate in _titles(title):
        row = db.execute(
            "SELECT qid FROM sitelink WHERE site = ? AND title = ?", (site, candidate)
        ).fetchone()
        if row:
            qid = int(row[0])
            break
    if qid is None:
        return None
    row = db.execute(
        "SELECT facts, descriptions, modified FROM entity WHERE qid = ?", (qid,)
    ).fetchone()
    if not row:
        return None
    facts = json.loads(row[0])
    if not isinstance(facts, dict):
        raise ValueError(f"facts of Q{qid} are not an object")
    refs = sorted({int(ref[1:]) for ref in _refs(facts)})
    labels: dict[str, str] = {}
    for start in range(0, len(refs), _CHUNK):
        chunk = refs[start : start + _CHUNK]
        marks = ",".join("?" * len(chunk))
        for ref, texts in db.execute(
            f"SELECT qid, labels FROM label WHERE qid IN ({marks})", chunk
        ):
            label = _pick(json.loads(texts), languages)
            if label:
                labels[f"Q{ref}"] = label
    meta = db.execute("SELECT value FROM meta WHERE key = 'dump_date'").fetchone()
    return Entry(
        qid=f"Q{qid}",
        facts=facts,
        labels=labels,
        description=_pick(json.loads(row[1]), languages),
        modified=row[2] if isinstance(row[2], str) else None,
        dump_date=str(meta[0]) if meta else "",
    )
