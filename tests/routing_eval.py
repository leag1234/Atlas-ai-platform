"""POC-E8: fixed labels, full confusion matrix, no provider or GPU calls."""

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from agent_provider import classify


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    lang: Literal["fr", "de", "es", "it", "en"]
    statut: Literal["draft", "valide"]
    label: Literal["simple", "complexe"]
    critique: bool = False
    input: str = Field(min_length=1)


def evaluate(cases: list[Case]) -> dict[str, object]:
    if not cases or len({c.id for c in cases}) != len(cases):
        raise ValueError("empty_or_duplicate_cases")
    if any(case.statut != "valide" for case in cases):
        raise ValueError("unvalidated_cases")
    matrix = {
        a: dict.fromkeys(("simple", "complexe"), 0) for a in ("simple", "complexe")
    }
    critical, correct = 0, 0
    languages: dict[str, list[int]] = {}
    decisions = []
    for case in cases:
        prediction = (
            "simple"
            if classify([{"role": "user", "content": case.input}]) == "chat_simple"
            else "complexe"
        )
        matrix[case.label][prediction] += 1
        match = int(prediction == case.label)
        correct += match
        critical += int(
            case.critique and case.label == "complexe" and prediction == "simple"
        )
        counts = languages.setdefault(case.lang, [0, 0])
        counts[0] += match
        counts[1] += 1
        decisions.append(
            {
                "id": case.id,
                "label": case.label,
                "prediction": prediction,
                "statut": case.statut,
            }
        )
    return {
        "accuracy": correct / len(cases),
        "sous_routage_critique": critical,
        "confusion_matrix": matrix,
        "by_language": {
            lang: good / total for lang, (good, total) in languages.items()
        },
        "cases": decisions,
    }


if __name__ == "__main__":
    cases = TypeAdapter(list[Case]).validate_python(
        yaml.safe_load(Path("evals/golden/e8_routage.yaml").read_text())
    )
    if len(cases) < 30 or len({case.lang for case in cases}) != 5:
        raise ValueError("incomplete_E8")
    report = evaluate(cases)
    target = Path("BRAIN/eval/routing.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
