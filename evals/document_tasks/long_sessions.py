"""D31-D33 independent transcript/artifact checks. No live runner or oracle agent."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from packages.context_usage import measure

ROOT = Path(__file__).parent
PLAN = ["Inspect source", "Calculate allocation", "Write register", "Verify totals"]
CONSTRAINT = "Ship only to France or Germany. Never ship outside these countries."


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def verify(task: str, output: Path, *, controls: bool = False) -> list[str]:
    """Input captures belong to the trusted runner, never to model-written artifacts."""
    errors = []
    try:
        capture = read(output / "capture.json")
        expected_mode = "synthetic-verifier-control" if controls else "recorded"
        if capture["mode"] != expected_mode:
            errors.append("capture_mode")
        raw = (output / "transcript.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != capture["transcript_sha256"]:
            errors.append("transcript_digest")
        if not controls:
            recording = (output / "recording.json").read_bytes()
            if hashlib.sha256(recording).hexdigest() != capture["recording_sha256"]:
                errors.append("recording_digest")
            if (
                read(output / "recording.json")["responses"]
                != read(output / "transcript.json")["responses"]
            ):
                errors.append("recording_responses")
        transcript = json.loads(raw)
        turns = transcript["turns"]
        count = 60 if task == "D33" else 40
        if [t["number"] for t in turns] != list(range(1, count + 1)):
            errors.append("turn_sequence")
        scenario = read(ROOT / task / "private/expectations.json")
        if [t["user"] for t in turns] != scenario["user_turns"]:
            errors.append("user_transcript")
        artifact = read(output / "artifacts/register.json")
        if task == "D31":
            if artifact != {"units": 48, "unit_cost_eur": 37.25, "total_eur": 1788.0}:
                errors.append("fact_artifact")
            if not all(v in turns[39]["assistant"] for v in ("37.25", "48", "1788")):
                errors.append("turn_40_fact_use")
        elif task == "D32":
            reductions = transcript["clearings"]
            if len(reductions) < 3 or len({e["turn"] for e in reductions}) < 3:
                errors.append("three_reductions")
            for event in reductions:
                if not event["ids"] or event["tokens_removed"] <= 0:
                    errors.append("reduction_evidence")
            if artifact != {
                "destinations": ["France", "Germany"],
                "rejected": ["Canada"],
            }:
                errors.append("constraint_artifact")
            for call in transcript["calls"]:
                if not any(
                    CONSTRAINT in str(m.get("content", "")) for m in call["messages"]
                ):
                    errors.append("pinned_constraint_lost")
            if (
                "Canada" not in turns[-1]["assistant"]
                or "reject" not in turns[-1]["assistant"].lower()
            ):
                errors.append("constraint_answer")
        else:
            if artifact != {"plan": PLAN, "completed": PLAN, "total_eur": 1788.0}:
                errors.append("plan_artifact")
            for call in transcript["calls"]:
                # Recompute from actual captured messages; a reported count alone
                # cannot make a huge context pass. Framing is included.
                context = measure(call["messages"], call["tools"], None)
                if context["total_tokens"] > scenario["token_budget_per_turn"]:
                    errors.append("context_budget")
                if call["input_tokens_estimate"] != context["total_tokens"]:
                    errors.append("false_token_count")
                body = json.dumps(call["messages"])
                if not all(step in body for step in PLAN):
                    errors.append("plan_lost")
            if not all(step in turns[-1]["assistant"] for step in PLAN):
                errors.append("plan_answer")
        if {c["turn"] for c in transcript["calls"]} != set(range(1, count + 1)):
            errors.append("missing_model_calls")
        if (
            hashlib.sha256(
                (output / "artifacts/register.json").read_bytes()
            ).hexdigest()
            != capture["artifact_sha256"]
        ):
            errors.append("artifact_digest")
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        errors.append("missing_or_invalid_evidence")
    return sorted(set(errors))


def main(task: str | None = None) -> int:
    parser = argparse.ArgumentParser()
    if task is None:
        parser.add_argument("--task", choices=("D31", "D32", "D33"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--controls", action="store_true")
    args = parser.parse_args()
    failures = verify(task or args.task, args.output, controls=args.controls)
    print(json.dumps(dict(passed=not failures, failures=failures, live=False)))
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
