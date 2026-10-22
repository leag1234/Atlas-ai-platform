"""Retrieval owns this derived index; corpus files remain the source of truth."""

import json
import logging
import math
from pathlib import Path
from typing import Self

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Chunk(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    chunk_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    doc_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    langue: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    embedding_revision: str = Field(min_length=1)
    position: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=32000)
    embedding: list[float] = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def valid_content(self) -> Self:
        if not all(
            v.strip()
            for v in [
                self.doc_id,
                self.source,
                self.langue,
                self.text,
                self.embedding_revision,
            ]
        ):
            raise ValueError("blank_field")
        if not all(math.isfinite(v) for v in self.embedding) or not any(self.embedding):
            raise ValueError("invalid_vector")
        return self


class Store:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def initialize(self) -> None:
        schema = Path(__file__).resolve().parents[2] / "contracts/m2-storage.sql"
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            row = connection.execute(
                "SELECT to_regnamespace('atlas_retrieval')"
            ).fetchone()
            if row is None or row[0] is None:
                connection.execute(schema.read_text())

    def sync(self, chunks: list[Chunk]) -> None:
        # Revalidate mutable vector lists before touching any persistent state.
        chunks = [Chunk.model_validate(c.model_dump()) for c in chunks]
        revisions = {(c.embedding_revision, len(c.embedding)) for c in chunks}
        if len(revisions) > 1:
            raise ValueError("mixed_embedding_revisions")
        docs: dict[str, Chunk] = {}
        for chunk in chunks:
            previous = docs.setdefault(chunk.doc_id, chunk)
            if (previous.source, previous.langue, previous.source_sha256) != (
                chunk.source,
                chunk.langue,
                chunk.source_sha256,
            ):
                raise ValueError("inconsistent_metadata")
        with psycopg.connect(self.dsn) as connection:
            connection.execute("LOCK TABLE atlas_retrieval.documents IN EXCLUSIVE MODE")
            # Full synchronization is atomic, including removal of deleted sources.
            connection.execute("DELETE FROM atlas_retrieval.documents")
            for c in docs.values():
                connection.execute(
                    "INSERT INTO atlas_retrieval.documents VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        c.doc_id,
                        c.source,
                        c.langue,
                        c.source_sha256,
                        c.embedding_revision,
                        len(c.embedding),
                    ),
                )
            for c in chunks:
                connection.execute(
                    "INSERT INTO atlas_retrieval.chunks VALUES (%s,%s,%s,%s,%s::vector)",
                    (c.chunk_id, c.doc_id, c.position, c.text, json.dumps(c.embedding)),
                )
        logging.getLogger(__name__).info(
            json.dumps(
                {"event": "index_synced", "documents": len(docs), "chunks": len(chunks)}
            )
        )

    def read(self, chunk_id: str | None = None) -> list[Chunk]:
        query = """SELECT c.chunk_id, c.doc_id, c.position, c.text,
            c.embedding::text AS embedding, d.source, d.langue, d.source_sha256,
            d.embedding_revision FROM atlas_retrieval.chunks c
            JOIN atlas_retrieval.documents d USING (doc_id)"""
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            rows = connection.execute(
                query
                + (" WHERE c.chunk_id=%s" if chunk_id is not None else "")
                + " ORDER BY c.chunk_id",
                (chunk_id,) if chunk_id is not None else (),
            ).fetchall()
        return [
            Chunk.model_validate({**r, "embedding": json.loads(r["embedding"])})
            for r in rows
        ]

    def resolve(self, chunk_id: str) -> Chunk:
        chunks = self.read(chunk_id)
        if not chunks:
            raise KeyError("chunk_not_found")
        return chunks[0]


def main() -> None:
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(description="Manage the derived retrieval index")
    parser.add_argument("action", choices=["init", "sync", "read"])
    args = parser.parse_args()
    store = Store(os.environ["ATLAS_RETRIEVAL_DSN"])
    if args.action == "init":
        store.initialize()
    elif args.action == "sync":
        data = json.load(sys.stdin)
        if not isinstance(data, list):
            raise ValueError("expected_chunk_list")
        store.sync([Chunk.model_validate(item) for item in data])
    else:
        print(json.dumps([c.model_dump() for c in store.read()], ensure_ascii=False))


if __name__ == "__main__":
    main()
