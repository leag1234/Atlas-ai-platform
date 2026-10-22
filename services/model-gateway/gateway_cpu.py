"""CPU inference configured solely inside the model gateway (POC-A3)."""

from packages.limits import LimitError

import json
import math
import logging
from time import monotonic
import sys

import numpy as np
import torch
from sentence_transformers import CrossEncoder, SentenceTransformer

EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
RERANK_MODEL = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
RERANK_REVISION = "1427fd652930e4ba29e8149678df786c240d8825"


def validate_texts(texts: list[str], maximum: int) -> None:
    if not 1 <= len(texts) <= maximum:
        raise LimitError(
            "invalid_cardinality", len(texts), maximum, "items (minimum 1)"
        )
    for text in texts:
        if len(text) > 32000:
            raise LimitError("invalid_text", len(text), 32000, "characters")
    if any(not t.strip() for t in texts):
        raise ValueError("invalid_text")
    if sum(map(len, texts)) > 128000:
        raise LimitError("context_exceeded", sum(map(len, texts)), 128000, "characters")


class CPUModels:
    def __init__(self) -> None:
        torch.set_num_threads(2)
        self.encoder = SentenceTransformer(
            EMBEDDING_MODEL,
            revision=EMBEDDING_REVISION,
            device="cpu",
            trust_remote_code=False,
            model_kwargs={"use_safetensors": True},
        )
        self.ranker = CrossEncoder(
            RERANK_MODEL,
            revision=RERANK_REVISION,
            device="cpu",
            trust_remote_code=False,
            model_kwargs={"use_safetensors": True},
            max_length=512,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        started = monotonic()
        validate_texts(texts, 32)
        vectors = self.encoder.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        result = [[float(v) for v in row] for row in vectors]
        if len(result) != len(texts) or any(
            not 1 <= len(row) <= 1024
            or not all(math.isfinite(v) for v in row)
            or not any(row)
            for row in result
        ):
            raise ValueError("invalid_provider_vector")
        logging.getLogger(__name__).info(
            json.dumps(
                {
                    "event": "embeddings",
                    "count": len(texts),
                    "seconds": monotonic() - started,
                }
            )
        )
        return result

    def rerank(self, question: str, texts: list[str]) -> list[float]:
        started = monotonic()
        validate_texts([question], 1)
        validate_texts(texts, 64)
        if len(question) + sum(map(len, texts)) > 128000:
            raise LimitError(
                "context_exceeded",
                len(question) + sum(map(len, texts)),
                128000,
                "characters",
            )
        values = self.ranker.predict(
            [(question, text) for text in texts],
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        scores = [float(v) for v in np.asarray(values).reshape(-1)]
        if len(scores) != len(texts) or not all(math.isfinite(v) for v in scores):
            raise ValueError("invalid_provider_scores")
        logging.getLogger(__name__).info(
            json.dumps(
                {
                    "event": "rerank",
                    "count": len(texts),
                    "seconds": monotonic() - started,
                }
            )
        )
        return scores


def main() -> None:
    # Local diagnostic CLI, using the same engines as the future HTTP boundary.
    logging.basicConfig(level=logging.INFO)
    request = json.load(sys.stdin)
    texts = request["texts"]
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        raise ValueError("invalid_texts")
    validate_texts(texts, 32 if request["operation"] == "embeddings" else 64)
    if request["operation"] not in {"embeddings", "rerank"}:
        raise ValueError("invalid_operation")
    backend = CPUModels()
    if request["operation"] == "embeddings":
        print(
            json.dumps(
                {"vectors": backend.embed(texts), "revision": EMBEDDING_REVISION}
            )
        )
    else:
        question = request["question"]
        if not isinstance(question, str):
            raise ValueError("invalid_question")
        print(json.dumps({"scores": backend.rerank(question, texts)}))


if __name__ == "__main__":
    main()
