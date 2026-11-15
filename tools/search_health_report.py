"""Daily web search health from the serving journal (metadata only).

Reads journal lines on standard input, for example
``journalctl -u atlas-staging --since "24 hours ago" -o cat``, and prints a
Markdown section: searches, outcomes, cache hit rate, per-engine answers
(asked calls only) and demotions, paid-provider fallbacks. The journal
lines carry no query text, and neither does this report.
"""

import argparse
import collections
import json
import re
import statistics
import sys
from collections.abc import Iterable

LINE = re.compile(
    r"\b(searxng_outcome|searxng_cache|searxng_engine_demoted|searxng_health) (\{.*\})\s*$"
)
SKIPPED = {"cooldown", "quarantined"}


def summarize(lines: Iterable[str]) -> dict[str, object]:
    outcomes: collections.Counter[str] = collections.Counter()
    cache: collections.Counter[str] = collections.Counter()
    engines: dict[str, collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    demoted: collections.Counter[str] = collections.Counter()
    chains: collections.Counter[str] = collections.Counter()
    waited: list[float] = []
    for line in lines:
        if '"event": "search_chain"' in line:
            try:
                chain = json.loads(line[line.index("{") :])
            except ValueError:
                continue
            attempts = chain.get("attempts") or []
            served = [
                a.get("provider")
                for a in attempts
                if isinstance(a, dict) and a.get("outcome") in {"ok", "partial_results"}
            ]
            chains[str(served[-1]) if served else "none"] += 1
            continue
        match = LINE.search(line)
        if not match:
            continue
        try:
            data = json.loads(match[2])
        except ValueError:
            continue
        if match[1] == "searxng_outcome":
            outcomes[str(data.get("outcome"))] += 1
            cache["miss"] += 1
            waited.append(float(data.get("waited_seconds") or 0))
            for row in data.get("engines") or []:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("engine"))
                if row.get("reason") in SKIPPED:
                    engines[name]["skipped"] += 1
                else:
                    engines[name]["asked"] += 1
                    engines[name][str(row.get("outcome"))] += 1
        elif match[1] == "searxng_cache":
            cache[str(data.get("result"))] += 1
        elif match[1] == "searxng_engine_demoted":
            demoted[str(data.get("engine"))] += 1
    lookups = sum(cache.values())
    return {
        "searches": sum(outcomes.values()),
        "outcomes": dict(outcomes),
        "lookups": lookups,
        "cache": dict(cache),
        "hit_rate": (cache["hit"] + cache["shared"]) / lookups if lookups else None,
        "engines": {name: dict(counts) for name, counts in sorted(engines.items())},
        "demoted": dict(demoted),
        "fallbacks": dict(chains),
        "waited_median": statistics.median(waited) if waited else None,
        "waited_max": max(waited) if waited else None,
    }


def rate(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "-"


def markdown(summary: dict[str, object], instance: str) -> str:
    outcomes = summary["outcomes"]
    assert isinstance(outcomes, dict)
    searches = int(str(summary["searches"]))
    out = [f"- Search health ({instance}, metadata only):"]
    if not searches and not summary["lookups"]:
        out.append("  - no web search in the period")
        return "\n".join(out)
    out.append(
        f"  - {searches} upstream searches: "
        + ", ".join(
            f"{name} {count} ({rate(count, searches)})"
            for name, count in sorted(outcomes.items(), key=lambda item: -item[1])
        )
    )
    cache = summary["cache"]
    assert isinstance(cache, dict)
    hit_rate = summary["hit_rate"]
    out.append(
        f"  - cache: {summary['lookups']} lookups, {cache.get('hit', 0)} hits, "
        f"{cache.get('shared', 0)} shared in flight, hit rate "
        + (f"{hit_rate:.0%}" if isinstance(hit_rate, float) else "-")
    )
    engines = summary["engines"]
    assert isinstance(engines, dict)
    for name, counts in engines.items():
        asked = int(counts.get("asked", 0))
        out.append(
            f"  - {name}: asked {asked}, ok {rate(counts.get('ok', 0), asked)}, "
            f"blocked {rate(counts.get('upstream_blocked', 0), asked)}, "
            f"empty {rate(counts.get('empty_results', 0), asked)}, "
            f"timeout {rate(counts.get('upstream_timeout', 0), asked)}, "
            f"skipped {counts.get('skipped', 0)}"
        )
    if summary["demoted"]:
        out.append(f"  - demotions: {summary['demoted']}")
    if summary["fallbacks"]:
        out.append(f"  - provider chains (served by): {summary['fallbacks']}")
    if summary["waited_median"] is not None:
        out.append(
            f"  - pacing wait per search: median {summary['waited_median']:.1f} s, "
            f"max {summary['waited_max']:.1f} s"
        )
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--instance", default="staging")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    summary = summarize(sys.stdin)
    print(json.dumps(summary) if args.json else markdown(summary, args.instance))


if __name__ == "__main__":
    main()
