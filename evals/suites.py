"""Complete POC-E1..E9 scoring; labels never enter retrieval or generation."""

import json
import re
from collections.abc import Callable
from pathlib import Path
from datetime import date
from typing import Any

import yaml

from evals.execution import Session
from evals.reporting import aggregate


def cases(suite: int) -> list[dict[str, Any]]:
    path = next(Path("evals/golden").glob(f"e{suite}_*.yaml"))
    values: list[dict[str, Any]] = yaml.safe_load(path.read_text())
    # YAML dates retain their exact ISO value across the JSON provider boundary.
    for case in values:
        for key, value in case.items():
            if isinstance(value, date):
                case[key] = value.isoformat()
    return [
        c
        for c in values
        if c.get("statut") != "stale"
        and (suite not in {6, 7, 9} or c.get("statut") == "valide")
    ]


def row(
    case: dict[str, Any], suite: int, score: float, **evidence: Any
) -> dict[str, Any]:
    return dict(
        id=case["id"],
        suite=f"e{suite}",
        lang=case.get("lang", case.get("direction", "").split("->")[-1]),
        score=score,
        statut=case.get("statut", "indicatif"),
        **evidence,
    )


def judged(
    session: Session,
    retrieve: Callable[[str], list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows, samples = [], []
    for suite in (2, 3, 7, 9):
        for case in cases(suite):
            evidence = retrieve(case["input"]) if suite in {2, 3} else []
            if suite in {2, 3}:
                sources = [
                    {"reference": i, "text": p["text"]}
                    for i, p in enumerate(evidence, 1)
                ]
                answer = session.call(
                    "system",
                    [
                        {
                            "role": "system",
                            "content": Path("prompts/rag.txt").read_text(),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"sources": sources, "question": case["input"]},
                                ensure_ascii=False,
                            ),
                        },
                    ],
                )
            else:
                answer = session.answer(case)
            grade = session.judge("production", case, answer, evidence)
            score = grade.score / 5
            critical = (
                grade.meaning_reversed
                if suite == 9
                else case.get("severity") == "high" and grade.score < 4
            )
            citations = [int(n) for n in re.findall(r"\[(\d+)\]", answer)]
            if suite == 2:
                valid = bool(citations) and all(
                    1 <= n <= len(evidence) for n in citations
                )
                valid = valid and any(
                    evidence[n - 1]["doc_id"] == case["doc_id_attendu"]
                    for n in citations
                )
                if not valid:
                    score = 0.0
                rows.append(
                    row(
                        dict(case, id="E5-" + case["id"]),
                        5,
                        float(valid and grade.supported),
                        answer=answer,
                        evidence=evidence,
                    )
                )
            if suite == 3:
                score = float(answer.strip() == "INSUFFICIENT" and grade.score >= 4)
            if case["id"] == "E7-012":
                try:
                    value = json.loads(answer)
                    valid = (
                        isinstance(value, dict)
                        and set(value) == {"risk", "reason"}
                        and value["risk"] in {"low", "medium", "high"}
                        and isinstance(value["reason"], str)
                    )
                except (ValueError, TypeError):
                    valid = False
                critical = critical or not valid
            if case["id"] == "E7-013":
                critical = critical or len(answer.split()) > 80
            if case["id"] == "E7-011":
                critical = (
                    critical
                    or len(re.findall(r"[^.!?]+[.!?](?:\s|$)", answer)) != 3
                    or bool(re.search(r"(?m)^\s*[-•]", answer))
                )
            rows.append(
                row(
                    case,
                    suite,
                    score,
                    answer=answer,
                    grade=grade.model_dump(),
                    evidence=evidence,
                    critical_failure=critical,
                )
            )
            samples.append(
                dict(
                    id=case["id"],
                    lang=rows[-1]["lang"],
                    case=case,
                    answer=answer,
                    evidence=evidence,
                    production=grade.score,
                )
            )
    return rows, samples


def report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = aggregate(rows)
    result["limitations"] = [
        "CONTRADICTION résolue en autonomie: E9 sur les cinq cas validés ; FLORES post-PoC.",
        "CONTRADICTION résolue en autonomie: écart linguistique <=15% cible à terme (docs/13 POC-EL) ; qualité GO reste fausse si écart, gate PoC conserve seuils moyens et échecs critiques.",
        "Répartition linguistique indicative PoC ; statuts de référence conservés, E1–E3 indicatifs et E4 déterministe draft.",
        "Calibration croisée informative ; validation humaine pré-GA. Cache fournisseur indisponible = null.",
    ]
    return result
