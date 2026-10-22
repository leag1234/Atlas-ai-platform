"""M5 evaluation driver: real CPU retrieval, recorded/live model boundaries."""

import asyncio
import gzip
import json
import os
from pathlib import Path
from threading import Thread
from typing import Any

from agent_provider import classify
from eval_provider import EvalProvider
from gateway_cpu import CPUModels
from http_gateway import serve
from test_storage import StorageTests
from agent_gate_eval import replay

from evals import agent
from evals.calibration import LANGUAGES, best_effort
from evals.execution import Session, matches_record
from evals.reporting import publish
from evals.suites import cases, judged, report, row
from services.retrieval.pipeline import ingest
from services.retrieval.search import Gateway, rank
from services.retrieval.store import Store


class RecordedProvider:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.path = (
            Path("tests/cassettes/evaluation.json.gz")
            if mode == "replay"
            else Path("BRAIN/eval/m5-recordings/calls-text.json")
        )
        self.records: list[dict[str, Any]] = (
            json.loads(
                gzip.decompress(self.path.read_bytes())
                if self.path.suffix == ".gz"
                else self.path.read_bytes()
            )
            if self.path.exists()
            else []
        )
        self.provider = EvalProvider()

    def complete(self, role: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        record = next(
            (
                r
                for r in self.records
                if matches_record(
                    r,
                    role,
                    messages,
                    self.provider.roles[role],
                )
            ),
            None,
        )
        if record is not None:
            return dict(record["result"])
        if self.mode == "replay":
            raise ValueError("unrecorded_request")
        spent = sum(r["result"]["telemetry"]["cost"] for r in self.records)
        if spent + 0.05 >= 3:
            raise ValueError("recording_series_budget")
        result = self.provider.complete(role, messages)
        self.records.append(dict(role=role, messages=messages, result=result))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.records, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "role": role,
                    "calls": len(self.records),
                    "cost": sum(r["result"]["telemetry"]["cost"] for r in self.records),
                }
            ),
            flush=True,
        )
        return result


def evaluate(gateway: Gateway, store: Store, mode: str) -> None:
    chunks = store.read()
    provider = RecordedProvider(mode)
    session = Session(provider.complete)

    def retrieve(question: str) -> list[dict[str, Any]]:
        return [
            c.model_dump(exclude={"embedding"}) for c in rank(question, chunks, gateway)
        ]

    rows = []
    for case in cases(1):
        found = retrieve(case["input"])
        position = next(
            (
                i
                for i, p in enumerate(found, 1)
                if p["doc_id"] == case["doc_id_attendu"]
                and case["passage_attendu"].casefold() in p["text"].casefold()
            ),
            0,
        )
        rows.append(
            row(
                case,
                1,
                float(position > 0),
                rank=position,
                mrr=1 / position if position else 0.0,
                retrieved=[p["chunk_id"] for p in found],
            )
        )
    observations, samples = judged(session, retrieve)
    rows.extend(observations)
    for suite in (4, 6):
        selected = cases(suite)
        results = asyncio.run(replay(Path("tests/cassettes/agent"), selected))
        for case, result in zip(selected, results, strict=True):
            if suite == 4:
                errors = agent.check_tools(case, result)
                rows.append(
                    row(
                        case,
                        suite,
                        float(not errors),
                        errors=errors,
                        provenance="M3 recorded trace replay",
                    )
                )
            else:
                evidence = [
                    {"tool": e["tool"], "output": e["output"]} for e in result.trace
                ]
                grade = session.judge("production", case, result.text, evidence)
                sourced = bool(agent.cited_pages(result)) and result.state == "done"
                rows.append(
                    row(
                        case,
                        suite,
                        grade.score / 5,
                        critical_failure=not sourced,
                        grade=grade.model_dump(),
                        answer=result.text,
                        evidence=evidence,
                        provenance="M3 recorded trace; M5 judgment",
                    )
                )
                rows.append(
                    row(
                        dict(case, id="E5-" + case["id"]),
                        5,
                        float(sourced and grade.supported),
                        grade=grade.model_dump(),
                    )
                )
    for case in cases(8):
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
                prediction=prediction,
                critical_failure=case.get("critique", False)
                and case["label"] == "complexe"
                and prediction == "simple",
            )
        )
    # Fixed round-robin across suites, independent of every observed score.
    selected_samples = []
    for lang in sorted(LANGUAGES):
        groups = [
            [
                s
                for s in samples
                if s["lang"] == lang and s["id"].startswith(f"E{suite}-")
            ]
            for suite in (2, 3, 7, 9)
        ]
        ordered = [
            group[i]
            for i in range(max(map(len, groups)))
            for group in groups
            if i < len(group)
        ]
        selected_samples.extend(ordered[:6])
    if len(selected_samples) != 30:
        raise ValueError("insufficient_calibration_sample")
    pairs = []
    excluded = []
    for sample in selected_samples:
        try:
            grade = session.judge(
                "reference", sample["case"], sample["answer"], sample["evidence"]
            )
        except ValueError as error:
            if str(error) != "unparseable_grade":
                raise
            excluded.append({"id": sample["id"], "reason": str(error)})
            continue
        pairs.append(
            {k: sample[k] for k in ("id", "lang", "production")}
            | {"reference": grade.score}
        )
    summary = report(rows) | {
        "mode": mode,
        "calibration_sample": [s["id"] for s in selected_samples],
    }
    calibration = best_effort(pairs, excluded) | {
        "models": dict(provider.provider.roles)
    }
    publish(Path("BRAIN/eval"), summary, session.telemetry, calibration)
    print(
        json.dumps(
            {
                "suites": summary["suites"],
                "quality_go": summary["quality_go"],
                "cost": session.cost,
            }
        ),
        flush=True,
    )
    if not summary["poc_passed"]:
        raise ValueError("quality_threshold")


def main() -> None:
    mode = os.environ.get("ATLAS_M5_MODE", "record")
    if mode not in {"record", "replay"}:
        raise ValueError("invalid_eval_mode")
    try:
        StorageTests.setUpClass()
        store = Store(StorageTests.dsn)
        store.initialize()
        with serve(CPUModels(), 0) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                gateway = Gateway(f"http://127.0.0.1:{server.server_port}")
                ingest(Path("corpus"), store, gateway)
                evaluate(gateway, store, mode)
            finally:
                server.shutdown()
                thread.join()
    finally:
        StorageTests.doClassCleanups()


if __name__ == "__main__":
    main()
