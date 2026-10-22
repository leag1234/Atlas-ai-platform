"""Real demo recordings and exact CI replay, confined to the test harness."""

import asyncio
import gzip
import json
import logging
import os
import re
import sys
from pathlib import Path
from threading import Thread
from time import monotonic
from typing import Any

import yaml
from agent_gate_eval import load_record, replay
from gateway_cpu import CPUModels
from http_gateway import serve
from record_agent import record
from test_cascade import CascadeTests
from test_storage import StorageTests

from evals.agent import cited_pages
from services.orchestrator.loop import Result
from services.retrieval.pipeline import ingest
from services.retrieval.search import Gateway
from services.retrieval.store import Store


def validate(ident: str, result: Result, fallback: bool) -> None:
    valid = result.state == "done" and bool(result.text.strip())
    if ident == "DEMO-RAG":
        ids = {
            p["chunk_id"]
            for event in result.trace
            if event["tool"] == "rag_search" and isinstance(event["output"], dict)
            for p in event["output"].get("data", {}).get("passages", [])
        }
        cited = set(re.findall(r"[a-f0-9]{64}", result.text))
        valid = valid and bool(cited) and cited <= ids
    elif ident == "DEMO-WEB":
        valid = valid and len(cited_pages(result)) >= 2
    else:
        valid = valid and fallback
    if not valid:
        raise ValueError("incomplete_demo_" + ident)


class Routes(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.fallback = False

    def emit(self, record: logging.LogRecord) -> None:
        self.fallback |= bool(json.loads(record.getMessage()).get("fallback"))


async def live(cases: list[dict[str, Any]], url: str) -> None:
    logger = logging.getLogger("agent_provider")
    previous_level = logger.level
    routes = Routes()
    logger.addHandler(routes)
    logger.setLevel(logging.INFO)
    try:
        for case in cases:
            routes.fallback = False
            started = monotonic()
            await record(case, url)
            path = Path("BRAIN/agent-recordings") / (case["id"] + ".json")
            data = json.loads(path.read_text())
            data["latency"] = monotonic() - started
            data["fallback"] = routes.fallback
            validate(case["id"], Result(**data["result"]), routes.fallback)
            destination = Path("tests/cassettes/demo") / (case["id"] + ".json.gz")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(
                gzip.compress(json.dumps(data, ensure_ascii=False).encode(), mtime=0)
            )
    finally:
        logger.removeHandler(routes)
        logger.setLevel(previous_level)


def archive_measurements() -> None:
    measurements = []
    for case in yaml.safe_load(Path("tests/journeys/demo.yaml").read_text()):
        data = load_record(Path("tests/cassettes/demo"), case["id"])
        measurements.append(
            {
                "id": case["id"],
                "latency": data["latency"],
                "cost": float(data["result"]["cost"]),
                "fallback": data["fallback"],
                "source": "live CPU retrieval / web / serverless; caches may be warm",
            }
        )
    Path("reports/demo.json.gz").write_bytes(
        gzip.compress(json.dumps(measurements).encode(), mtime=0)
    )


def main() -> None:
    cases = yaml.safe_load(Path("tests/journeys/demo.yaml").read_text())
    if sys.argv[1:]:
        cases = [c for c in cases if c["id"] in sys.argv[1:]]
        if {c["id"] for c in cases} != set(sys.argv[1:]):
            raise ValueError("unknown_demo_case")
    if os.environ.get("ATLAS_M6_MODE", "record") == "replay":
        directory = Path("tests/cassettes/demo")
        results = asyncio.run(replay(directory, cases))
        for case, result in zip(cases, results, strict=True):
            validate(case["id"], result, load_record(directory, case["id"])["fallback"])
        checked = CascadeTests("test_closed_local_endpoint_escalates").run()
        if checked is None or not checked.wasSuccessful():
            raise ValueError("fallback_demo_failed")
    else:
        os.environ["LOCAL_API_BASE"] = "http://127.0.0.1:1/v1"
        try:
            StorageTests.setUpClass()
            store = Store(StorageTests.dsn)
            store.initialize()
            with serve(CPUModels(), 0) as server:
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f"http://127.0.0.1:{server.server_port}"
                    os.environ.update(
                        ATLAS_GATEWAY_URL=url, ATLAS_RETRIEVAL_DSN=StorageTests.dsn
                    )
                    ingest(Path("corpus"), store, Gateway(url))
                    asyncio.run(live(cases, url))
                finally:
                    server.shutdown()
                    thread.join()
        finally:
            StorageTests.doClassCleanups()
        archive_measurements()
    print(json.dumps({"scenarios": [c["id"] for c in cases], "status": "done"}))


if __name__ == "__main__":
    main()
