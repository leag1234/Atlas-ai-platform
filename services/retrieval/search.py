"""Hybrid retrieval; this module never reads evaluation labels (POC-F2)."""

from packages.limits import LimitError

import json
import logging
import math
import re
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

from services.retrieval.store import Chunk


class Vectors(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(min_length=1)
    vectors: list[list[float]]


class Score(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    chunk_id: str
    score: float = Field(allow_inf_nan=False)


class Scores(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scores: list[Score]


class Gateway:
    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    def post(self, operation: str, payload: dict[str, object]) -> object:
        data = json.dumps(payload, allow_nan=False).encode()
        if len(data) > 800000:
            raise LimitError("context_exceeded", len(data), 800000, "request bytes")
        request = Request(
            self.url + "/" + operation,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read(800001)
            if len(body) > 800000:
                raise LimitError(
                    "provider_response_limit", len(body), 800000, "response bytes"
                )
            return json.loads(body)
        except HTTPError as exc:
            raise ValueError("gateway_http_error") from exc
        except (URLError, TimeoutError) as exc:
            raise ValueError("gateway_unavailable") from exc

    def embed(self, texts: list[str], kind: str) -> tuple[list[list[float]], str]:
        result = Vectors.model_validate(
            self.post("embeddings", {"texts": texts, "kind": kind})
        )
        dimensions = {len(v) for v in result.vectors}
        if (
            len(result.vectors) != len(texts)
            or len(dimensions) != 1
            or any(
                not 1 <= len(v) <= 1024
                or not any(v)
                or not all(math.isfinite(x) for x in v)
                for v in result.vectors
            )
        ):
            raise ValueError("invalid_provider_vector")
        return result.vectors, result.revision

    def scores(self, question: str, chunks: list[Chunk]) -> dict[str, float]:
        result = Scores.model_validate(
            self.post(
                "rerank",
                {
                    "question": question,
                    "passages": [
                        {"chunk_id": c.chunk_id, "text": c.text} for c in chunks
                    ],
                },
            )
        )
        values = {s.chunk_id: s.score for s in result.scores}
        if len(values) != len(result.scores) or set(values) != {
            c.chunk_id for c in chunks
        }:
            raise ValueError("invalid_provider_scores")
        return values


def tokens(text: str) -> list[str]:
    return re.findall(r"[\u3400-\u9fff]|[^\W_\u3400-\u9fff]+", text.casefold())


def rank(
    question: str, chunks: list[Chunk], gateway: Gateway, k: int = 8
) -> list[Chunk]:
    return [chunk for chunk, _ in rank_scored(question, chunks, gateway, k)]


def rank_scored(
    question: str, chunks: list[Chunk], gateway: Gateway, k: int = 8
) -> list[tuple[Chunk, float]]:
    if len(question) > 32000:
        raise LimitError("invalid_query", len(question), 32000, "characters")
    if not 1 <= k <= 8:
        raise LimitError("invalid_query", k, 8, "results (minimum 1)")
    if not question.strip():
        raise ValueError("invalid_query")
    if not chunks:
        return []
    vectors, revision = gateway.embed([question], "query")
    query = vectors[0]
    if any(
        c.embedding_revision != revision or len(c.embedding) != len(query)
        for c in chunks
    ):
        raise ValueError("index_revision")
    terms = [Counter(tokens(c.text)) for c in chunks]
    average = sum(sum(t.values()) for t in terms) / len(terms)
    lexical = [0.0] * len(chunks)
    for word in set(tokens(question)):
        frequency = sum(word in t for t in terms)
        idf = math.log(1 + (len(chunks) - frequency + 0.5) / (frequency + 0.5))
        for i, counts in enumerate(terms):
            tf = counts[word]
            lexical[i] += (
                idf
                * tf
                * 2.2
                / (tf + 1.2 * (0.25 + 0.75 * sum(counts.values()) / max(average, 1)))
            )
    dense = [
        sum(a * b for a, b in zip(query, c.embedding, strict=True))
        / (
            math.sqrt(sum(v * v for v in query))
            * math.sqrt(sum(v * v for v in c.embedding))
        )
        for c in chunks
    ]
    fusion = [0.0] * len(chunks)
    for scores in [lexical, dense]:
        for position, i in enumerate(
            sorted(range(len(chunks)), key=lambda i: (-scores[i], chunks[i].chunk_id))
        ):
            fusion[i] += 1 / (60 + position + 1)
    candidates = [
        chunks[i]
        for i in sorted(
            range(len(chunks)), key=lambda i: (-fusion[i], chunks[i].chunk_id)
        )[:32]
    ]
    reranked = gateway.scores(question, candidates)
    result = sorted(candidates, key=lambda c: (-reranked[c.chunk_id], c.chunk_id))[:k]
    logging.getLogger(__name__).info(
        json.dumps(
            {
                "event": "retrieved",
                "candidates": len(candidates),
                "returned": len(result),
            }
        )
    )
    return [(chunk, reranked[chunk.chunk_id]) for chunk in result]
