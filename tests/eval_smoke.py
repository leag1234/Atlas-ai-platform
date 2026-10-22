"""POC-R4: fifteen recorded cases across tools, routing and judged behavior."""

import asyncio
import json
from pathlib import Path

from agent_provider import classify
from agent_gate_eval import replay
from m5_gate import RecordedProvider

from evals import agent
from evals.execution import Session
from evals.reporting import aggregate, publish
from evals.suites import cases, row


def main() -> None:
    rows = []
    session = Session(RecordedProvider("replay").complete, limit=0.3, seconds=180)
    for case in cases(7)[:5]:
        answer = session.answer(case)
        grade = session.judge("production", case, answer, [])
        rows.append(
            row(
                case,
                7,
                grade.score / 5,
                critical_failure=case.get("severity") == "high" and grade.score < 4,
            )
        )
    selected = cases(4)[:5]
    results = asyncio.run(replay(Path("tests/cassettes/agent"), selected))
    for case, result in zip(selected, results, strict=True):
        rows.append(row(case, 4, float(not agent.check_tools(case, result))))
    for case in cases(8)[:5]:
        prediction = (
            "simple"
            if classify([{"role": "user", "content": case["input"]}]) == "chat_simple"
            else "complexe"
        )
        rows.append(
            row(
                case,
                8,
                float(prediction == case["label"]),
                critical_failure=case.get("critique", False)
                and prediction == "simple"
                and case["label"] == "complexe",
            )
        )
    if len(rows) != 15 or len({r["lang"] for r in rows}) < 3:
        raise ValueError("invalid_smoke_coverage")
    summary = aggregate(rows, full=False)
    publish(
        Path("BRAIN/eval/smoke"),
        summary,
        session.telemetry,
        {"type": "not recalibrated during smoke"},
    )
    if not summary["quality_go"]:
        raise ValueError("smoke_quality")
    print(
        json.dumps(
            {
                "executed_cases": len(rows),
                "languages": sorted({r["lang"] for r in rows}),
                "live_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
