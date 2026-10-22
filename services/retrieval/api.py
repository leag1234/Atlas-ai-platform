"""Retrieval-owned HTTP boundary for M7; no cross-service database access."""

import os
from packages.ports import atlas_ports

from fastapi import FastAPI, HTTPException
from psycopg import Error as DatabaseError

from services.retrieval.search import Gateway, rank_scored
from services.retrieval.store import Chunk, Store
from services.retrieval.tool import Request

from services.retrieval.project_api import router

app = FastAPI(title="ATLAS retrieval")
app.include_router(router)


def store() -> Store:
    return Store(os.environ["ATLAS_RETRIEVAL_DSN"])


def public(chunk: Chunk) -> dict[str, object]:
    return chunk.model_dump(include={"chunk_id", "doc_id", "source", "text"})


@app.post("/search")
def search(request: Request) -> dict[str, object]:
    try:
        ranked = rank_scored(
            request.query,
            store().read(),
            Gateway(os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway"))),
        )
        return {
            "passages": [{**public(chunk), "score": score} for chunk, score in ranked]
        }
    except (ValueError, RuntimeError, OSError, DatabaseError):
        raise HTTPException(503, "retrieval_unavailable") from None


@app.get("/sources/{chunk_id}")
def source(chunk_id: str) -> dict[str, object]:
    if len(chunk_id) != 64 or any(c not in "0123456789abcdef" for c in chunk_id):
        raise HTTPException(400, "invalid_chunk_id")
    try:
        return public(store().resolve(chunk_id))
    except KeyError:
        raise HTTPException(404, "chunk_not_found") from None
    except (OSError, DatabaseError):
        raise HTTPException(503, "retrieval_unavailable") from None
