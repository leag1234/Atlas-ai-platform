"""One part of the unit test modules, for a CI shard: python3 tests/unit_part.py K/N.

Modules are assigned to N parts by measured duration (tests/unit_weights.json, relative seconds
measured on 2026-11-02), longest first, each to the currently lightest part. A module without a
measured weight counts as the median. Every module lands in exactly one part.
"""

import json
import statistics
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def modules() -> list[str]:
    return sorted(path.stem for path in TESTS.glob("test_*.py"))


def parts(count: int) -> list[list[str]]:
    weights: dict[str, float] = json.loads((TESTS / "unit_weights.json").read_text())
    names = modules()
    median = statistics.median(weights.values()) if weights else 1.0
    load = [0.0] * count
    chosen: list[list[str]] = [[] for _ in range(count)]
    for name in sorted(names, key=lambda n: (-weights.get(n, median), n)):
        lightest = min(range(count), key=lambda i: (load[i], i))
        chosen[lightest].append(name)
        load[lightest] += weights.get(name, median)
    return [sorted(part) for part in chosen]


def main() -> int:
    index, _, count = sys.argv[1].partition("/")
    selected = parts(int(count))[int(index) - 1]
    print(f"Unit part {index}/{count}: {len(selected)} modules", flush=True)
    # Same import path as python3 -m unittest discover -s tests run from the repository:
    # the tests directory is the top level, and the repository root is importable.
    sys.path[:0] = [str(TESTS), str(TESTS.parent)]
    suite = unittest.defaultTestLoader.loadTestsFromNames(selected)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
