"""POC-R1/R2: independent grading and bounded observable calls."""

import json
import re
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Grade(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    score: int = Field(ge=1, le=5)
    supported: bool
    meaning_reversed: bool
    rationale: str = Field(min_length=1)


def parse_grade(text: str) -> Grade:
    """Extract the first syntactically valid object; never repair its fields."""
    decoder = json.JSONDecoder()
    for offset, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[offset:])
        except json.JSONDecodeError:
            continue
        return Grade.model_validate(value)
    raise ValueError("unparseable_grade")


def messages_for(case: dict[str, Any]) -> list[dict[str, str]]:
    if "turns" in case:
        return [
            {"role": "user", "content": t["user"]} for t in case["turns"] if "user" in t
        ]
    if "direction" in case:
        question = (
            Path("prompts/eval-translation.txt")
            .read_text()
            .format(
                language={
                    "fr": "French",
                    "de": "German",
                    "en": "English",
                    "es": "Spanish",
                    "it": "Italian",
                }[case["direction"].split("->")[1]],
                text=case["input_text"],
            )
        )
    else:
        question = case["input"]
    return [{"role": "user", "content": question}]


class Session:
    def __init__(
        self,
        complete: Callable[[str, list[dict[str, str]]], dict[str, Any]],
        limit: float = 3.0,
        seconds: float = 1200,
    ) -> None:
        self.complete, self.limit = complete, limit
        self.end = monotonic() + seconds
        self.cost = 0.0
        self.telemetry: list[dict[str, Any]] = []

    def call(self, role: str, messages: list[dict[str, str]]) -> str:
        # Every gateway call is bounded by 0.05 EUR and 120 seconds.
        if self.cost + 0.05 > self.limit or monotonic() + 120 >= self.end:
            raise ValueError("run_budget")
        result = self.complete(role, [dict(message) for message in messages])
        self.cost += result["telemetry"]["cost"]
        self.telemetry.append(result["telemetry"] | {"role": role})
        if self.cost >= self.limit or monotonic() >= self.end:
            raise ValueError("run_budget")
        return str(result["text"])

    def answer(self, case: dict[str, Any]) -> str:
        history: list[dict[str, str]] = []
        answer = ""
        for turn in messages_for(case):
            history.append(turn)
            answer = self.call("system", history)
            history.append({"role": "assistant", "content": answer})
        return answer

    def judge(
        self,
        role: str,
        case: dict[str, Any],
        answer: str,
        evidence: list[dict[str, Any]],
    ) -> Grade:
        if case["id"].startswith("E2-"):
            references = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
            evidence = [
                {"reference": i, "chunk_id": p.get("chunk_id"), "text": p["text"]}
                for i, p in enumerate(evidence, 1)
                if i in references
            ]
        elif case["id"].startswith("E3-"):
            # The human-authored absent-answer key is the reference for refusal.
            evidence = []
        rubric = Path("prompts/eval-judge.txt").read_text()
        if case["id"].startswith("E3-"):
            rubric += "\n" + Path("prompts/eval-refusal.txt").read_text()
        text = self.call(
            role,
            [
                {
                    "role": "system",
                    "content": rubric,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"case": case, "answer": answer, "evidence": evidence},
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        return parse_grade(text)


def matches_record(
    record: dict[str, Any], role: str, messages: list[dict[str, str]], model: str | None
) -> bool:
    return bool(
        record["role"] == role
        and record["messages"] == messages
        and (model is None or record["result"]["model"] == model)
    )
