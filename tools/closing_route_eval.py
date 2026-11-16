"""Live check: a polite closing after an app is answered in the chat.

Not a unit test (it calls a real model through the gateway), kept out of CI by
its place, like tools/writing_route_eval.py. The deterministic part of the
same cases runs in tests/test_closing_intent.py.

    ATLAS_GATEWAY_URL=http://127.0.0.1:<gateway port> \\
    PYTHONPATH="services/model-gateway:services/edge-bff:." \\
    python tools/closing_route_eval.py [--closings 5] [--requests 3] \\
        [--privacy strict] [--out results.json]

Each case of tests/fixtures/polite_closings.json is sent after a conversation
in which an app was just delivered: the model router once (two calls in
flight at most), then the code rules that settle its decision, and the lexical
gates alone (the path taken when the router times out or fails). A closing
must end as a chat answer; a counter-example (a change, a rerun, a yes) must
not be turned into one by the code rules.
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

CASES = ROOT / "tests" / "fixtures" / "polite_closings.json"
APP_LINK = "/api/atlas/artifacts/" + "d" * 32 + "/versions/1/open"
HISTORY = [
    {"role": "user", "content": "fais-moi un petit jeu de memory avec des animaux"},
    {
        "role": "assistant",
        "content": f"Voici le jeu de memory : 8 paires d'animaux. [Ouvrir]({APP_LINK})",
    },
]
# Kinds that keep the turn a text answer in the chat.
TEXT = frozenset({"answer", "research", "other"})


def cases(closings: int | None, requests: int | None) -> list[dict[str, Any]]:
    rows = list(json.loads(CASES.read_text(encoding="utf-8"))["cases"])
    kept = [r for r in rows if r["expect"] == "closing"][:closings]
    kept += [r for r in rows if r["expect"] == "request"][:requests]
    return kept


async def evaluate(rows: list[dict[str, Any]], parallel: int) -> list[dict[str, Any]]:
    from router_eval import lexical_kind

    from services.orchestrator.request_router import _settle, classify

    gate = asyncio.Semaphore(parallel)

    async def one(case: dict[str, Any]) -> dict[str, Any]:
        text = case["text"]
        async with gate:
            routed, ms, cost, reason = await classify(text, HISTORY)
        settled, rule = _settle(text, routed)
        lexical = lexical_kind(text, HISTORY, case.get("lang", "fr"))
        final = settled.kind if settled is not None else lexical
        if case["expect"] == "closing":
            ok = final in TEXT
        else:
            # The code rules never turn a request into a chat answer.
            ok = rule != "closing" and (
                settled is None or routed is None or settled.kind == routed.kind
            )
        return {
            "text": text,
            "lang": case.get("lang"),
            "expect": case["expect"],
            "router": routed.kind if routed else None,
            "follow_up": routed.follow_up if routed else None,
            "settled": settled.kind if settled else None,
            "rule": rule or None,
            "lexical": lexical,
            "final": final,
            "ok": ok,
            "ms": round(ms),
            "cost": str(cost),
            "fallback": reason or None,
        }

    return list(await asyncio.gather(*(one(case) for case in rows)))


def report(rows: list[dict[str, Any]]) -> None:
    for expect in ("closing", "request"):
        group = [r for r in rows if r["expect"] == expect]
        if not group:
            continue
        print(f"\n== {expect}: {sum(r['ok'] for r in group)}/{len(group)} correct")
        print("  router kinds:", dict(Counter(str(r["router"]) for r in group)))
        print("  final kinds: ", dict(Counter(str(r["final"]) for r in group)))
        print("  settled by the closing rule:", sum(r["rule"] == "closing" for r in group))
    print(
        "fallbacks:",
        dict(Counter(r["fallback"] for r in rows if r["fallback"])) or "none",
    )
    latencies = sorted(r["ms"] for r in rows)
    print(
        f"latency ms: median {statistics.median(latencies):.0f}, max {latencies[-1]}; "
        f"cost {sum(Decimal(r['cost']) for r in rows):.4f} EUR"
    )
    for r in rows:
        if not r["ok"]:
            print(
                f"  MISS {r['expect']} -> router {r['router']} / settled {r['settled']}"
                f" / lexical {r['lexical']}: {r['text'][:80]}"
            )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--closings", type=int)
    parser.add_argument("--requests", type=int)
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
            rows = await evaluate(
                cases(args.closings, args.requests), min(max(args.parallel, 1), 2)
            )
    finally:
        privacy.reset(token)
    print(f"{len(rows)} cases after an app turn, privacy {args.privacy}")
    report(rows)
    if args.out:
        args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    if not all(r["ok"] for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
