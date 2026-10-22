"""E1 deterministic scoring and end-to-end citation resolution, no label leakage."""

import json
import logging
import os
from packages.ports import atlas_ports
from pathlib import Path
from collections import defaultdict
import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from services.retrieval.answer import answer
from services.retrieval.search import Gateway, rank
from services.retrieval.store import Store


class Case(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str
    lang: str
    input: str = Field(min_length=1)
    doc_id_attendu: str
    passage_attendu: str = Field(min_length=1)


def metrics(ranks: list[int]) -> dict[str, int | float]:
    if not ranks:
        raise ValueError("empty_evaluation")
    return {
        "cases": len(ranks),
        "recall_at_8": sum(r > 0 for r in ranks) / len(ranks),
        "mrr": sum(1 / r for r in ranks if r) / len(ranks),
    }


def evaluate(cases: list[Case], store: Store, gateway: Gateway) -> dict[str, object]:
    chunks = store.read()
    if len(cases) < 7 or not chunks or len({c.id for c in cases}) != len(cases):
        raise ValueError("invalid_evaluation")
    by_language: dict[str, list[int]] = defaultdict(list)
    for case in cases:
        found = rank(case.input, chunks, gateway)
        position = next(
            (
                i
                for i, c in enumerate(found, 1)
                if c.doc_id == case.doc_id_attendu
                and case.passage_attendu.casefold() in c.text.casefold()
            ),
            0,
        )
        by_language[case.lang].append(position)
    # Fixed questions, independent of answer keys; actual retrieval feeds generation.
    for question in [cases[0].input, cases[6].input, cases[3].input]:
        result = answer(question, rank(question, chunks, gateway), gateway, store)
        if result.refused or not result.citations:
            raise ValueError("citations_missing")
    report: dict[str, object] = dict(
        metrics([r for values in by_language.values() for r in values])
    )
    report.update(
        citations_resolues=True,
        statut="indicatif",
        par_langue={lang: metrics(values) for lang, values in by_language.items()},
    )
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    target = Path("BRAIN/eval/retrieval.json")
    target.unlink(missing_ok=True)
    cases = TypeAdapter(list[Case]).validate_python(
        yaml.safe_load(Path("evals/golden/e1_retrieval.yaml").read_text())
    )
    if len(cases) < 40:
        raise ValueError("incomplete_e1")
    report = evaluate(
        cases,
        Store(os.environ["ATLAS_RETRIEVAL_DSN"]),
        Gateway(os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway"))),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
