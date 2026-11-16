"""Measure the answer language chosen for realistic messages (offline, no model).

    python tools/language_eval.py [--misses] [--json out.json]

Each case of tests/fixtures/language-detection.json runs once per interface
locale it lists (split "check": written after the tuning, never tuned on),
through the same two steps as a chat request: the
conversation language among the five interface languages
(packages.language.conversation_language with the UI locale), then the prose
language (packages.language.prose_language), which may be any language. A case
passes when the prose language is the expected one. Prints the accuracy per
kind of case, the interface-language accuracy for the five, the latency per
resolution and, with --misses, every miss. tests/test_language_detection.py
holds the accuracy floors.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.language import LANGUAGES, conversation_language, prose_language  # noqa: E402

CASES = ROOT / "tests" / "fixtures" / "language-detection.json"


@dataclass(frozen=True)
class Message:
    role: str
    text: str


def messages(case: dict[str, Any]) -> list[Message]:
    history = [str(text) for text in case.get("history", [])]
    turns = [
        Message("user" if index % 2 == 0 else "assistant", text)
        for index, text in enumerate(history)
    ]
    return [*turns, Message("user", str(case["text"]))]


def resolve(case: dict[str, Any], locale: str) -> tuple[str, str]:
    conversation = messages(case)
    interface = conversation_language(conversation, locale)
    return interface, prose_language(conversation, interface)


def measure(split: str | None = None) -> dict[str, Any]:
    """Accuracy over the cases of one split ("tune" or "check"), or all of them."""
    cases = [
        case
        for case in json.loads(CASES.read_text())["cases"]
        if split is None or case.get("split", "tune") == split
    ]
    totals: Counter[str] = Counter()
    passed: Counter[str] = Counter()
    interface_total = interface_passed = 0
    misses: list[dict[str, object]] = []
    seconds: list[float] = []
    for case in cases:
        for locale in case["locales"]:
            start = time.perf_counter()
            interface, prose = resolve(case, locale)
            seconds.append(time.perf_counter() - start)
            kind = str(case["kind"])
            totals[kind] += 1
            if prose == case["expected"]:
                passed[kind] += 1
            else:
                misses.append(
                    {
                        "kind": kind,
                        "text": case["text"],
                        "locale": locale,
                        "expected": case["expected"],
                        "got": prose,
                    }
                )
            if case["expected"] in LANGUAGES:
                interface_total += 1
                interface_passed += interface == case["expected"]
    ordered = sorted(seconds)
    return {
        "accuracy": sum(passed.values()) / sum(totals.values()),
        "runs": sum(totals.values()),
        "by_kind": {
            kind: {"runs": totals[kind], "accuracy": passed[kind] / totals[kind]}
            for kind in sorted(totals)
        },
        "interface_accuracy": interface_passed / interface_total,
        "latency_ms": {
            "mean": statistics.fmean(seconds) * 1000,
            "p95": ordered[int(len(ordered) * 0.95)] * 1000,
            "max": ordered[-1] * 1000,
        },
        "misses": misses,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--misses", action="store_true", help="print every miss")
    parser.add_argument("--json", type=Path, help="write the full result here")
    args = parser.parse_args()
    # The first detection loads the detector's profiles: not a per-message cost.
    resolve({"text": "warm up"}, "en")
    for split in ("tune", "check"):
        part = measure(split)
        print(f"{split} split: {part['accuracy']:.1%} over {part['runs']} runs")
    result = measure()
    print(
        f"prose language accuracy {result['accuracy']:.1%} over {result['runs']} runs"
    )
    for kind, row in result["by_kind"].items():
        print(f"  {kind:<8} {row['accuracy']:.1%} ({row['runs']} runs)")
    print(
        f"interface language accuracy (five languages) {result['interface_accuracy']:.1%}"
    )
    latency = result["latency_ms"]
    print(
        f"latency per resolution: mean {latency['mean']:.1f} ms, "
        f"p95 {latency['p95']:.1f} ms, max {latency['max']:.1f} ms"
    )
    if args.misses:
        for miss in result["misses"]:
            print(
                f"  miss [{miss['kind']}] ui={miss['locale']} expected={miss['expected']} "
                f"got={miss['got']}: {miss['text']}"
            )
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")


if __name__ == "__main__":
    main()
