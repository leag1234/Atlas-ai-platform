"""Retrieval-owned personal memory, atomically scoped in SQLite or forced RLS."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from psycopg.types.json import Jsonb

from packages.context_usage import encoded, tokens
from packages.user_memory import (
    CONTEXT_TOKENS,
    MAX_ENTRIES,
    MAX_TOKENS,
    MemoryWrite,
    projection,
)
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_store import ProjectPool
from services.retrieval.projects import Projects


def policy_enabled(org: str) -> bool:
    return os.environ.get("ATLAS_USER_MEMORY_ENABLED", "1") != "0" and org not in {
        v.strip()
        for v in os.environ.get("ATLAS_USER_MEMORY_DISABLED_ORGS", "").split(",")
    }


class UserMemory:
    def __init__(
        self, path: Path | None = None, pool: ProjectPool | None = None
    ) -> None:
        self.pool = pool
        self.sqlite = Projects(path) if path is not None else None
        if self.sqlite:
            with self.sqlite.connection() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS user_memory (org TEXT, subject TEXT, data TEXT NOT NULL, PRIMARY KEY(org,subject))"
                )

    @contextmanager
    def transaction(self, org: str, subject: str) -> Iterator[dict[str, Any]]:
        if not org or not subject or subject == "unknown":
            raise ProjectError(401, "unauthenticated")
        if self.pool:
            with self.pool.connection() as db:
                db.execute("SELECT set_config('atlas.memory_org',%s,true)", (org,))
                db.execute(
                    "SELECT set_config('atlas.memory_subject',%s,true)", (subject,)
                )
                db.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (encoded([org, subject]),),
                )
                row = db.execute(
                    "SELECT data FROM atlas_projects.user_memory WHERE org=%s AND subject=%s",
                    (org, subject),
                ).fetchone()
                data = row["data"] if row else dict(enabled=True, entries=[])
                yield data
                db.execute(
                    "INSERT INTO atlas_projects.user_memory VALUES(%s,%s,%s) ON CONFLICT(org,subject) DO UPDATE SET data=excluded.data",
                    (org, subject, Jsonb(data)),
                )
        else:
            assert self.sqlite is not None
            with self.sqlite.connection() as sqlite:
                sqlite.execute("BEGIN IMMEDIATE")
                row = sqlite.execute(
                    "SELECT data FROM user_memory WHERE org=? AND subject=?",
                    (org, subject),
                ).fetchone()
                data = (
                    json.loads(row["data"]) if row else dict(enabled=True, entries=[])
                )
                yield data
                sqlite.execute(
                    "INSERT INTO user_memory VALUES(?,?,?) ON CONFLICT(org,subject) DO UPDATE SET data=excluded.data",
                    (org, subject, encoded(data)),
                )

    def view(self, org: str, subject: str) -> dict[str, Any]:
        with self.transaction(org, subject) as data:
            return dict(
                data,
                policy_enabled=policy_enabled(org),
                effective_enabled=data["enabled"] and policy_enabled(org),
                limits=dict(
                    entries=MAX_ENTRIES,
                    tokens=MAX_TOKENS,
                    context_tokens=CONTEXT_TOKENS,
                ),
            )

    @staticmethod
    def check(entries: list[dict[str, Any]]) -> None:
        for measured, limit, unit in (
            (len(entries), MAX_ENTRIES, "entries"),
            (tokens(encoded(entries)), MAX_TOKENS, "tokens"),
            (
                tokens(projection([e for e in entries if e["kind"] == "preference"])),
                CONTEXT_TOKENS,
                "preference context tokens",
            ),
        ):
            if measured > limit:
                raise ProjectError(
                    429,
                    "memory_capacity",
                    f"Memory capacity: {measured} {unit}; limit {limit}. Edit or delete an entry first.",
                    measurements=[dict(measured=measured, limit=limit, unit=unit)],
                )

    def write(
        self, org: str, subject: str, value: MemoryWrite, identifier: str | None = None
    ) -> dict[str, Any]:
        with self.transaction(org, subject) as data:
            if not policy_enabled(org) or not data["enabled"]:
                raise ProjectError(
                    403,
                    "memory_disabled",
                    "Personal memory is disabled. Nothing was saved.",
                )
            entries = data["entries"]
            old = next((e for e in entries if e["id"] == identifier), None)
            if identifier and old is None:
                raise ProjectError(404, "not_found")
            if not identifier:
                duplicate = next(
                    (
                        e
                        for e in entries
                        if e["text"] == value.text and e["kind"] == value.kind
                    ),
                    None,
                )
                if duplicate:
                    return dict(duplicate)
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            entry = dict(
                id=identifier or str(uuid4()),
                **value.model_dump(),
                source="user",
                created_at=old["created_at"] if old else now,
                observed_at=now,
            )
            candidate = [e for e in entries if e["id"] != identifier] + [entry]
            self.check(candidate)
            data["entries"] = candidate
            return entry

    def setting(self, org: str, subject: str, enabled: bool) -> dict[str, Any]:
        with self.transaction(org, subject) as data:
            data["enabled"] = enabled
        return self.view(org, subject)

    def delete(self, org: str, subject: str, identifier: str | None = None) -> None:
        with self.transaction(org, subject) as data:
            entries = data["entries"]
            if identifier and not any(e["id"] == identifier for e in entries):
                raise ProjectError(404, "not_found")
            data["entries"] = (
                [e for e in entries if e["id"] != identifier] if identifier else []
            )
