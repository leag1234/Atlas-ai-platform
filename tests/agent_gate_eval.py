"""M3 test driver: live fault-injection or exact recorded provider replay, never runtime."""

import asyncio
import hashlib
import gzip
import json
import os
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

from evals import agent
from record_agent import main as record_main
from services.orchestrator.loop import (
    Call,
    Message,
    Query,
    Reservation,
    Result,
    Turn,
    run,
)
from services.orchestrator.model import Configuration, GatewayModel


class ReplayModel(GatewayModel):
    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__(
            "http://unused.invalid",
            Configuration.model_validate(record["configuration"]),
        )
        self.records = iter(record["model"])
        self.count = 0

    async def complete(self, messages: list[Message], timeout: float) -> Turn:
        item = next(self.records)
        if messages != item["messages"]:
            raise AssertionError("unrecorded_model_request")
        self.count += 1
        self.observe(messages, item["prompt_tokens"])
        response = item["response"]
        usage = response["usage"]
        return Turn(
            response["text"],
            tuple(Call(**c) for c in response["calls"]),
            Reservation(usage["tokens"], Decimal(usage["cost"])),
        )


class ReplayTools:
    def __init__(self, record: dict[str, Any]) -> None:
        self.records = iter(record["tools"])
        self.count = 0

    def estimate(self, call: Call) -> Reservation:
        return Reservation(0, Decimal(0))

    async def execute(self, call: Call, timeout: float) -> Message:
        item = next(self.records)
        if asdict(call) != item["call"]:
            raise AssertionError("unrecorded_tool_request")
        self.count += 1
        output: Message = item["output"]
        return output


def load_record(directory: Path, ident: str) -> dict[str, Any]:
    path = directory / (ident + ".json")
    if path.exists():
        content = path.read_text()
    else:
        with gzip.open(path.with_suffix(".json.gz"), "rt", encoding="utf-8") as stream:
            content = stream.read()
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("invalid_recording")
    return value


async def replay(directory: Path, cases: list[dict[str, Any]]) -> list[Result]:
    prompt = Path("prompts/agent.txt").read_text()
    results = []
    for case in cases:
        record = load_record(directory, case["id"])
        if record["prompt_sha256"] != hashlib.sha256(prompt.encode()).hexdigest():
            raise AssertionError("stale_prompt_recording")
        if (
            record["tools_sha256"]
            != hashlib.sha256(Path("prompts/tools.json").read_bytes()).hexdigest()
        ):
            raise AssertionError("stale_tool_recording")
        model, tools = ReplayModel(record), ReplayTools(record)
        samples = iter(record["clock_samples"]) if "clock_samples" in record else None

        def clock() -> float:
            # Legacy recordings contain no timing evidence. Their replay clock
            # is fixed; newer captures preserve every observed clock reading.
            return float(next(samples)) if samples is not None else 0.0

        result = await run(
            Query(question=case["input"], lang=case["lang"]),
            model,
            tools,
            prompt,
            clock=clock,
        )
        if samples is not None and next(samples, None) is not None:
            raise AssertionError("unused_clock_recording")
        if model.count != len(record["model"]) or tools.count != len(record["tools"]):
            raise AssertionError("unused_recording")
        actual = json.loads(json.dumps(asdict(result), default=str))
        if actual != record["result"]:
            raise AssertionError("recorded_result_changed")
        results.append(result)
    return results


def select_cases(suite: str, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if suite == "web":
        cases = [case for case in cases if case.get("statut") == "valide"]
        if not cases:
            raise ValueError("no_validated_web_cases")
    return cases


def main() -> None:
    suite = sys.argv[1]
    if suite not in ("tools", "web"):
        raise ValueError("invalid_suite")
    name = "e4_tool_calling" if suite == "tools" else "e6_web"
    cases = select_cases(
        suite, yaml.safe_load(Path(f"evals/golden/{name}.yaml").read_text())
    )
    mode = os.environ.get("ATLAS_M3_EVAL_MODE", "live")
    if mode == "live":
        with patch.object(sys, "argv", ["record_agent.py", *[c["id"] for c in cases]]):
            record_main()
        directory = Path("BRAIN/agent-recordings")
    elif mode == "replay":
        directory = Path("tests/cassettes/agent")
    else:
        raise ValueError("invalid_test_mode")
    results = asyncio.run(replay(directory, cases))
    output = agent.report(suite, cases, results)
    output["mode"] = mode
    path = Path(f"BRAIN/eval/{suite}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, default=str, indent=2))
    print(json.dumps({k: v for k, v in output.items() if k != "cases"}), flush=True)
    if suite == "tools" and output["success_rate"] < 0.90:
        raise SystemExit(1)
    if suite == "web" and not any(r["passed"] for r in output["cases"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
