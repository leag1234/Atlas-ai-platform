"""Live, bounded serverless micro-benchmark; never measures replay speed."""

import json
import gzip
import subprocess
import multiprocessing
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

from eval_provider import EvalProvider


def probe(_: int) -> dict[str, Any]:
    return EvalProvider().complete(
        "system",
        [{"role": "user", "content": Path("tests/journeys/bench.txt").read_text()}],
    )


def main() -> None:
    subprocess.run(["make", "test-budgets"], check=True)
    started = monotonic()
    baseline = probe(0)
    waves = []
    with multiprocessing.get_context("spawn").Pool(8) as pool:
        for _ in range(2):
            waves.append(pool.map(probe, range(8)))
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "live serverless via model-gateway; two waves of eight, not long-term load",
        "concurrency": 8,
        "budget_tests": True,
        "wall_seconds": monotonic() - started,
        "baseline": baseline,
        "waves": waves,
    }
    path = Path("BRAIN/eval/m6-bench.json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    Path("reports/bench.json.gz").write_bytes(gzip.compress(path.read_bytes(), mtime=0))
    print(
        json.dumps(
            {
                "requests": 17,
                "cost": sum(
                    r["telemetry"]["cost"] for r in [baseline] + sum(waves, [])
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
