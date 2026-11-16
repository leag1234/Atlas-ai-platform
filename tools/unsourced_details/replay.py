"""Replay the unsourced-details treatment on recorded turns.

Takes a run written by run.py (each held turn keeps its draft answer and the
texts grounding collected) and applies ``unsourced_details.review`` to the
draft with the same evidence, then grounding's own check, as the pipeline
does. Only the revision call is live (through the instance's model gateway):
the comparison with the recorded answers is free of search and model variance.

    PYTHONPATH="services/model-gateway:services/edge-bff:." \\
    python tools/unsourced_details/replay.py RUN OUT --mode revise [--parallel 4]

Writes OUT/records.jsonl in run.py's format (annotate.py reads it); cost and
seconds are the recorded turn's plus the treatment's.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import run as live  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("run", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--mode", default="revise")
    parser.add_argument("--instance", default="staging")
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument(
        "--no-reference",
        action="store_true",
        help="skip the encyclopedia article check",
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    live.configure(args.instance, args.out, [])
    os.chdir(ROOT)
    from services.orchestrator import grounding, unsourced_details

    records = [
        json.loads(line)
        for line in (args.run / "records.jsonl").read_text().splitlines()
        if line.strip()
    ]
    gate = asyncio.Semaphore(args.parallel)

    os.environ["ATLAS_UNSOURCED_DETAILS"] = args.mode

    async def one(record: dict[str, Any]) -> dict[str, Any]:
        held = record.get("grounding") or {}
        # A turn the pipeline would hold with the setting on: grounding's cue or
        # the module's own; its draft and evidence are the recorded ones (the
        # source tools' texts when grounding did not hold it).
        if (
            record.get("state") != "done"
            or record.get("task_type") not in {"text"}
            or not (held or unsourced_details.wants_review(record["question"]))
        ):
            return {**record, "unsourced": None}
        draft = held.get("draft", record["answer"])
        evidence = held.get("evidence") or [
            text for tool in record.get("tools", []) for text in tool.get("texts", [])
        ]
        async with gate:
            answer, report = await unsourced_details.review(
                draft,
                evidence,
                [record["question"]],
                profile=record["profile"],
                language=record["lang"],
                question=record["question"],
                urls=record.get("retrieved_urls", []),
                setting=args.mode,
                reference=not args.no_reference,
            )
        final = answer
        if evidence:
            check = grounding.Grounding()
            for passage in evidence:
                check.add(passage)
            final = check.check(answer, record["question"], record["lang"])
        return {
            **record,
            "answer": final,
            "cost_eur": record["cost_eur"] + float(report.cost_eur),
            "seconds": round(record["seconds"] + report.seconds, 2),
            "unsourced": {**report.metadata(), "removed": report.removed},
            "retrieved_urls": record.get("retrieved_urls", []) + report.reference_urls,
            "tools": record.get("tools", [])
            + [
                {
                    "tool": "reference_check",
                    "arguments": "",
                    "texts": report.reference_texts,
                }
            ],
        }

    results = await asyncio.gather(*(one(r) for r in records))
    with (args.out / "records.jsonl").open("w") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    spent = sum(float((r.get("unsourced") or {}).get("cost_eur", 0)) for r in results)
    print(f"replayed {len(results)} turns, treatment cost {spent:.4f} EUR")


if __name__ == "__main__":
    asyncio.run(main())
