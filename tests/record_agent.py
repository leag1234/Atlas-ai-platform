"""Explicit live recording utility. Fault injection exists only in tests."""

import argparse
import asyncio
import hashlib
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from threading import Thread
from typing import Any

import yaml
from gateway_cpu import CPUModels
from http_gateway import serve
from test_storage import StorageTests

from services.orchestrator.cache import Cache
from services.orchestrator.loop import Call, Message, Query, run
from services.orchestrator.model import GatewayModel
from services.orchestrator.tools import Runtime
from services.retrieval.pipeline import ingest
from services.retrieval.search import Gateway
from services.retrieval.store import Store
from decimal import Decimal


class RecordingModel(GatewayModel):
    records: list[Message]

    async def complete(self, messages: list[Message], timeout: float) -> Any:
        result = await super().complete(messages, timeout)
        self.records.append(
            {
                "messages": json.loads(json.dumps(messages)),
                "response": asdict(result),
                "prompt_tokens": self.prompt_tokens,
            }
        )
        return result


class RecordingTools(Runtime):
    def __init__(self, case: dict[str, Any]) -> None:
        super().__init__(Cache(Path("BRAIN/web-cache.sqlite")), Decimal(0))
        self.case = case
        self.records: list[Message] = []
        self.counts: dict[str, int] = {}

    async def execute(self, call: Call, timeout: float) -> Message:
        self.counts[call.name] = self.counts.get(call.name, 0) + 1
        fault = self.case.get("fault_injection", {})
        if fault.get("tool") == call.name and fault.get("call") in (
            "*",
            self.counts[call.name],
        ):
            result: Message = {"error": fault["error"]}
        else:
            result = await super().execute(call, timeout)
        self.records.append({"call": asdict(call), "output": result})
        return result


async def record(case: dict[str, Any], url: str) -> str:
    base = await GatewayModel.connect(url)
    model = RecordingModel(url, base.configuration)
    model.records = []
    tools = RecordingTools(case)
    prompt = Path("prompts/agent.txt").read_text()
    clock_samples: list[float] = []

    def clock() -> float:
        value = time.monotonic()
        clock_samples.append(value)
        return value

    result = await run(
        Query(question=case["input"], lang=case["lang"]),
        model,
        tools,
        prompt,
        clock=clock,
    )
    output = {
        "id": case["id"],
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "configuration": base.configuration.model_dump(mode="json"),
        "tools_sha256": hashlib.sha256(
            Path("prompts/tools.json").read_bytes()
        ).hexdigest(),
        "model": model.records,
        "tools": tools.records,
        "result": asdict(result),
        "clock_samples": clock_samples,
    }
    path = Path("BRAIN/agent-recordings") / (case["id"] + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, default=str, indent=2))
    print(
        json.dumps(
            {
                "id": case["id"],
                "state": result.state,
                "reason": result.reason,
                "tools": [e["tool"] for e in result.trace],
                "cost": str(result.cost),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    return result.reason


async def record_cases(cases: list[dict[str, Any]], url: str) -> None:
    failures = 0
    for case in cases:
        reason = await record(case, url)
        if reason == "provider_error":
            failures += 1
            if failures >= 3:
                raise RuntimeError("provider_error_threshold")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ids", nargs="+")
    args = parser.parse_args()
    cases = [
        c
        for name in ("e4_tool_calling", "e6_web")
        for c in yaml.safe_load(Path(f"evals/golden/{name}.yaml").read_text())
        if c["id"] in args.ids
    ]
    try:
        StorageTests.setUpClass()
        store = Store(StorageTests.dsn)
        store.initialize()
        with serve(CPUModels(), 0) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                os.environ["ATLAS_GATEWAY_URL"] = url
                os.environ["ATLAS_RETRIEVAL_DSN"] = StorageTests.dsn
                ingest(Path("corpus"), store, Gateway(url))
                asyncio.run(record_cases(cases, url))
            finally:
                server.shutdown()
                thread.join()
    finally:
        StorageTests.doClassCleanups()


if __name__ == "__main__":
    main()
