"""Private project state owned by retrieval; corrections invalidate stale writes."""

from packages.limits import LimitError

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from services.retrieval.search import tokens
from services.retrieval.project_store import ScopedProjects as ScopedProjects


def bounded(value: str, limit: int, empty: bool = False) -> str:
    if isinstance(value, str) and len(value) > limit:
        raise LimitError("invalid_text", len(value), limit, "characters")
    if (
        not isinstance(value, str)
        or len(value) > limit
        or (not empty and not value.strip())
    ):
        raise ValueError("invalid_text")
    return value


class Projects:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        if path.is_symlink():
            raise ValueError("invalid_storage")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT, instructions TEXT,
                    privacy TEXT NOT NULL DEFAULT 'standard'
                    CHECK(privacy IN ('standard','strict')), revision INTEGER);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id), name TEXT);
                CREATE TABLE IF NOT EXISTS turns (
                    project TEXT, conversation TEXT REFERENCES conversations(id),
                    question TEXT, answer TEXT, revision INTEGER);
                CREATE TABLE IF NOT EXISTS facts (
                    id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id),
                    conversation_id TEXT, text TEXT, UNIQUE(project,text));
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def require(
        db: sqlite3.Connection, project: str, conversation: str | None = None
    ) -> sqlite3.Row:
        row = db.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
        if not isinstance(row, sqlite3.Row):
            raise KeyError("not_found")
        if (
            conversation is not None
            and db.execute(
                "SELECT 1 FROM conversations WHERE project=? AND id=?",
                (project, conversation),
            ).fetchone()
            is None
        ):
            raise KeyError("not_found")
        return row

    def create(self, name: str, instructions: str, privacy: str = "standard") -> str:
        bounded(name, 120)
        bounded(instructions, 4000, True)
        if privacy not in ("standard", "strict"):
            raise ValueError("invalid_privacy")
        identifier = str(uuid4())
        with self.connection() as db:
            db.execute(
                "INSERT INTO projects VALUES (?,?,?,?,0)",
                (identifier, name, instructions, privacy),
            )
        return identifier

    def listing(self) -> list[dict[str, object]]:
        with self.connection() as db:
            return [
                dict(r) for r in db.execute("SELECT * FROM projects ORDER BY rowid")
            ]

    def get(self, project: str) -> dict[str, object]:
        with self.connection() as db:
            return dict(self.require(db, project))

    def update(self, project: str, name: str, instructions: str) -> None:
        bounded(name, 120)
        bounded(instructions, 4000, True)
        with self.connection() as db:
            self.require(db, project)
            db.execute(
                "UPDATE projects SET name=?,instructions=? WHERE id=?",
                (name, instructions, project),
            )

    def set_privacy(self, project: str, privacy: str) -> None:
        if privacy not in ("standard", "strict"):
            raise ValueError("invalid_privacy")
        with self.connection() as db:
            self.require(db, project)
            db.execute("UPDATE projects SET privacy=? WHERE id=?", (privacy, project))

    def conversation(self, project: str, name: str) -> str:
        bounded(name, 120)
        identifier = str(uuid4())
        with self.connection() as db:
            self.require(db, project)
            db.execute(
                "INSERT INTO conversations VALUES (?,?,?)", (identifier, project, name)
            )
        return identifier

    def conversations(self, project: str) -> list[dict[str, object]]:
        with self.connection() as db:
            self.require(db, project)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,name FROM conversations WHERE project=? ORDER BY rowid",
                    (project,),
                )
            ]

    def history(self, project: str, conversation: str) -> list[dict[str, object]]:
        with self.connection() as db:
            self.require(db, project, conversation)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT question,answer FROM turns WHERE project=? AND conversation=? ORDER BY rowid",
                    (project, conversation),
                )
            ]

    def facts(self, project: str) -> list[dict[str, object]]:
        with self.connection() as db:
            self.require(db, project)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,text,conversation_id FROM facts WHERE project=? ORDER BY rowid",
                    (project,),
                )
            ]

    def context(self, project: str, conversation: str, query: str) -> dict[str, object]:
        bounded(query, 32000)
        with self.connection() as db:
            # One read transaction gives revision and facts from the same snapshot.
            db.execute("BEGIN")
            row = self.require(db, project, conversation)
            terms = set(tokens(query))
            facts = [
                dict(r)
                for r in db.execute(
                    "SELECT id,text,conversation_id FROM facts WHERE project=?",
                    (project,),
                )
            ]
            ranked = sorted(
                facts, key=lambda f: -len(terms & set(tokens(str(f["text"]))))
            )
            selected = [
                {**f, "kind": "memory"}
                for f in ranked
                if terms & set(tokens(str(f["text"])))
            ][:8]
            history = [
                dict(r)
                for r in db.execute(
                    "SELECT question,answer FROM turns WHERE project=? AND conversation=? AND revision=? ORDER BY rowid DESC LIMIT 8",
                    (project, conversation, row["revision"]),
                )
            ]
            return {
                "instructions": row["instructions"],
                "privacy": row["privacy"],
                "revision": row["revision"],
                "facts": selected,
                "history": list(reversed(history)),
            }

    def turn(
        self,
        project: str,
        conversation: str,
        question: str,
        answer: str,
        facts: list[str],
        revision: int,
    ) -> None:
        bounded(question, 32000)
        bounded(answer, 32000)
        if len(facts) > 8:
            raise LimitError("invalid_facts", len(facts), 8, "facts")
        if len(facts) > 8 or type(revision) is not int or revision < 0:
            raise ValueError("invalid_facts")
        for fact in facts:
            bounded(fact, 500)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.require(db, project, conversation)
            # Stale turns remain inspectable but cannot feed the future context.
            db.execute(
                "INSERT INTO turns VALUES (?,?,?,?,?)",
                (
                    project,
                    conversation,
                    question,
                    answer,
                    revision if revision == row["revision"] else -1,
                ),
            )
            if revision == row["revision"]:
                for fact in facts:
                    db.execute(
                        "INSERT OR IGNORE INTO facts VALUES (?,?,?,?)",
                        (str(uuid4()), project, conversation, fact),
                    )

    def edit(self, project: str, fact: str, text: str) -> None:
        bounded(text, 500)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.require(db, project)
            changed = db.execute(
                "UPDATE facts SET text=? WHERE project=? AND id=?",
                (text, project, fact),
            )
            if changed.rowcount != 1:
                raise KeyError("not_found")
            db.execute("UPDATE projects SET revision=revision+1 WHERE id=?", (project,))

    def erase(self, project: str, fact: str | None = None) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.require(db, project)
            if fact is None:
                db.execute("DELETE FROM facts WHERE project=?", (project,))
            elif (
                db.execute(
                    "DELETE FROM facts WHERE project=? AND id=?", (project, fact)
                ).rowcount
                != 1
            ):
                raise KeyError("not_found")
            db.execute("UPDATE projects SET revision=revision+1 WHERE id=?", (project,))
