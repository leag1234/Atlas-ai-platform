"""Validate consolidation transport without repairing or inventing facts."""

from pydantic import BaseModel, ConfigDict, Field


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=500, pattern=r"\S")
    evidence: str = Field(min_length=1, max_length=500, pattern=r"\S")


class Consolidation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answer: str = Field(min_length=1, max_length=32000, pattern=r"\S")
    facts: list[Fact] = Field(max_length=8)


def consolidated(text: str, question: str) -> tuple[str, list[str]]:
    result = Consolidation.model_validate_json(text)
    if any(f.evidence not in question for f in result.facts):
        raise ValueError("ungrounded_memory")
    return result.answer, list(dict.fromkeys(f.text for f in result.facts))
