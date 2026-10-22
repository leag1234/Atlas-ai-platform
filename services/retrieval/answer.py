"""Validate gateway answers and resolve every cited source in the owned index."""

import re
from pydantic import BaseModel, ConfigDict, Field
from services.retrieval.search import Gateway
from services.retrieval.store import Chunk, Store


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=32000)
    citations: list[str] = Field(max_length=8)
    refused: bool


def answer(
    question: str, chunks: list[Chunk], gateway: Gateway, store: Store
) -> Answer:
    result = Answer.model_validate(
        gateway.post(
            "answer",
            {
                "question": question,
                "passages": [{"chunk_id": c.chunk_id, "text": c.text} for c in chunks],
            },
        )
    )
    cited = set(result.citations)
    references = set(re.findall(r"\[([a-f0-9]{64})\]", result.text))
    if (
        not result.text.strip()
        or len(cited) != len(result.citations)
        or cited != references
        or (result.refused and cited)
        or (not result.refused and not cited)
        or not cited <= {c.chunk_id for c in chunks}
    ):
        raise ValueError("invalid_citation")
    for key in cited:
        if store.resolve(key) != next(c for c in chunks if c.chunk_id == key):
            raise ValueError("citation_changed")
    return result
