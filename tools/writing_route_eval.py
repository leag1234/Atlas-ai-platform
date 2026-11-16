"""Live check: plain writing requests stay a text answer in the chat.

Not a unit test (it calls a real model through the gateway), kept out of CI by
its place, like tools/router_eval.py. The deterministic part of the same cases
runs in tests/test_writing_intent.py.

    ATLAS_GATEWAY_URL=http://127.0.0.1:<gateway port> \\
    PYTHONPATH="services/model-gateway:services/edge-bff:." \\
    python tools/writing_route_eval.py [--privacy strict] [--out results.json]

Each case of tests/fixtures/plain_writing_routes.json runs the model router
once (two calls in flight at most), then the code rules that settle its
decision, and the lexical gates alone (the path taken when the router times
out or fails). Every outcome is reduced to where the turn goes: "text" (a chat
answer), "app" (the app panel) or "file" (Work). Prints the confusion matrix
of each path, the misses, latency and cost.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

CASES = ROOT / "tests" / "fixtures" / "plain_writing_routes.json"
EXPECTED = ("answer", "app", "file")
PLACES = ("text", "app", "file")

# Where each router kind sends a turn in the chat pipeline.
_PLACE = {
    "answer": "text",
    "research": "text",
    "other": "text",
    "app": "app",
    "drawing": "app",
    "file": "file",
    "run": "file",
    "remote": "file",
}
# Where each lexical outcome of tools/router_eval.py sends a turn.
_LEXICAL_PLACE = {
    "app": "app",
    "drawing": "app",
    "file": "file",
    "run": "file",
    "remote": "file",
    "work_follow_up": "file",
}


def place(kind: str | None) -> str:
    return _PLACE.get(kind or "", "text")


def lexical_place(kind: str) -> str:
    return _LEXICAL_PLACE.get(kind, "text")


def want(expect: str) -> str:
    return {"answer": "text"}.get(expect, expect)


def cases() -> list[dict[str, Any]]:
    return list(json.loads(CASES.read_text(encoding="utf-8"))["cases"])


async def evaluate(rows: list[dict[str, Any]], parallel: int) -> list[dict[str, Any]]:
    from router_eval import lexical_kind

    from services.orchestrator import request_router
    from services.orchestrator.request_router import classify

    settle = getattr(request_router, "settle", None)
    gate = asyncio.Semaphore(parallel)

    async def one(case: dict[str, Any]) -> dict[str, Any]:
        text = case["text"]
        async with gate:
            routed, ms, cost, reason = await classify(text, [])
        settled = settle(text, routed) if settle and routed is not None else routed
        lexical = lexical_kind(text, [], case.get("lang", "fr"))
        final = place(settled.kind) if settled is not None else lexical_place(lexical)
        return {
            "text": text,
            "lang": case.get("lang"),
            "expect": case["expect"],
            "format": case.get("format"),
            "router": routed.kind if routed else None,
            "router_format": routed.file_format if routed else None,
            "settled": settled.kind if settled else None,
            "lexical": lexical,
            "router_place": place(routed.kind) if routed else None,
            "lexical_place": lexical_place(lexical),
            "final_place": final,
            "ms": round(ms),
            "cost": str(cost),
            "fallback": reason,
        }

    return list(await asyncio.gather(*(one(case) for case in rows)))


def matrix(rows: list[dict[str, Any]], key: str) -> str:
    lines = [f"  {'expected':9}" + "".join(f"{p:>6}" for p in (*PLACES, "none"))]
    for expect in EXPECTED:
        group = [r for r in rows if r["expect"] == expect]
        if not group:
            continue
        counts = Counter(r[key] or "none" for r in group)
        lines.append(
            f"  {expect:9}" + "".join(f"{counts[p]:>6}" for p in (*PLACES, "none"))
        )
    good = sum(r[key] == want(r["expect"]) for r in rows)
    lines.append(f"  correct {good}/{len(rows)}")
    return "\n".join(lines)


def report(rows: list[dict[str, Any]]) -> None:
    for title, key in (
        ("router alone", "router_place"),
        ("lexical gates alone (router timeout or failure)", "lexical_place"),
        ("pipeline (router, then code rules; lexical when no decision)", "final_place"),
    ):
        print(f"\n== {title}")
        print(matrix(rows, key))
    files = [r for r in rows if r["format"] and r["router"] == "file"]
    if files:
        exact = sum(r["router_format"] == r["format"] for r in files)
        print(f"\nfile_format exact (router said file): {exact}/{len(files)}")
    print(
        "fallbacks:",
        dict(Counter(r["fallback"] for r in rows if r["fallback"])) or "none",
    )
    latencies = sorted(r["ms"] for r in rows)
    print(
        f"latency ms: median {statistics.median(latencies):.0f}, max {latencies[-1]}; "
        f"cost {sum(Decimal(r['cost']) for r in rows):.4f} EUR"
    )
    print("misses (expected -> router / settled / lexical / pipeline):")
    for r in rows:
        if (
            want(r["expect"]) != r["final_place"]
            or want(r["expect"]) != r["lexical_place"]
        ):
            print(
                f"  {r['expect']} -> {r['router']} / {r['settled']} / {r['lexical']} / "
                f"{r['final_place']}: {r['text'][:80]}"
            )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path)
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--privacy", choices=("standard", "strict"), default="standard")
    args = parser.parse_args()
    os.environ.setdefault("ATLAS_MODEL_ROUTER_TIMEOUT", "10")
    from packages import privacy

    token = privacy.elevate(args.privacy)
    try:
        with patch(
            "services.orchestrator.image_tool.images_enabled", return_value=False
        ):
            rows = await evaluate(cases(), min(max(args.parallel, 1), 2))
    finally:
        privacy.reset(token)
    print(f"{len(rows)} cases, privacy {args.privacy}")
    report(rows)
    if args.out:
        args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
