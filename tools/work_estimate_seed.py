"""Seed of the Work duration history (work_estimates.py) from corpus runs.

The chat shows a plan with an estimate built from measured step durations.
Before any live measure exists, the history is seeded with the corpus runs of
the staging instance: each conversation record keeps the status lines the chat
showed, with their times, the total duration and the cost. This tool turns them
into duration and cost measures by step type, without any request or answer
content: only the profile, the path (app or Work), the step type, seconds,
EUR and success.

Run (from the repository root, read-only on the runs):
    python tools/work_estimate_seed.py RESULTS_DIR... > config/work-estimate-seed.json

Each RESULTS_DIR is a corpus run directory holding records.jsonl.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.orchestrator.app_turn import PROGRESS  # noqa: E402
from services.orchestrator.guest_activity import (  # noqa: E402
    ACTIVITIES,
    activity_label,
)
from services.orchestrator.work_estimates import ORDER  # noqa: E402
from services.orchestrator.work_progress import LABELS  # noqa: E402

LANGUAGES = ("en", "fr", "de", "es", "it")

# Status keys of work_progress.LABELS by step type; None: the time goes to the
# next typed step (the model prepares that step).
WORK_KEYS = {
    "preparing_workspace": "setup",
    "planning": None,
    "reading_files": "code",
    "writing_file": "code",
    "running_command": "test",
    "publishing": "deliver",
    "checking": "deliver",
    "preparing_document": "document",
    "writing_document": "document",
    "machine": None,
    "working": None,
}
ACTIVITY_KEYS = {
    "web_search": "research",
    "web_fetch": "research",
    "thinking": None,
    "answering": None,
}
# Index of the app progress labels (app_turn.PROGRESS): writing, writing with
# lines, checking, fixing.
APP_TYPES = ("code", "code", "test", "code")


def _normal(text: str) -> str:
    text = re.sub(r"\s+\d+\s*s$", "", text.strip())
    text = re.sub(r"\s*\(\d+(?:\.\d+)?s / \d+(?:\.\d+)?s\)$", "", text)
    return re.sub(r"\d+", "#", text)


def label_table() -> dict[str, tuple[str, str | None]]:
    """Normalized status text -> (path, step type or None)."""
    table: dict[str, tuple[str, str | None]] = {}
    for language in LANGUAGES:
        for key, step in WORK_KEYS.items():
            text = LABELS[language][key]
            if "{" in text:
                text = text.split("{")[0].rstrip()
            table[_normal(text)] = ("work", step)
        for key, step in ACTIVITY_KEYS.items():
            if key in ACTIVITIES:
                table[_normal(activity_label(language, key))] = ("", step)
        labels = PROGRESS["app"].get(language, PROGRESS["app"]["en"])
        for index, text in enumerate(labels):
            table[_normal(text.split("{")[0].rstrip())] = ("app", APP_TYPES[index])
    activities = json.loads((ROOT / "prompts/activity-labels.json").read_text())
    for language, labels in activities.items():
        for key, step in ACTIVITY_KEYS.items():
            if key in labels:
                table.setdefault(_normal(labels[key]), ("", step))
    return table


def classify(
    text: str, table: dict[str, tuple[str, str | None]]
) -> tuple[str, str | None] | None:
    normal = _normal(text)
    if normal in table:
        return table[normal]
    for known, value in table.items():
        # "Writing the app… # lines" and "Writing the document… # sections".
        if known and normal.startswith(known):
            return value
    return None


def spans(
    turn: dict, table: dict[str, tuple[str, str | None]]
) -> tuple[str, dict[str, float]] | None:
    """(path, seconds by step type) of one turn, forward-only like the chat plan."""
    statuses = [(float(t), str(text)) for t, text in turn.get("statuses") or []]
    total = float(turn.get("total") or 0)
    if not statuses or total <= 0:
        return None
    path = ""
    seconds: dict[str, float] = {}
    waiting = 0.0
    current = -1
    for index, (start, text) in enumerate(statuses):
        end = statuses[index + 1][0] if index + 1 < len(statuses) else total
        length = max(0.0, end - start)
        found = classify(text, table)
        if found is None:
            waiting += length
            continue
        kind, step = found
        path = path or kind
        if step is None:
            waiting += length
            continue
        rank = max(current, ORDER.index(step))
        current = rank
        effective = ORDER[rank]
        seconds[effective] = seconds.get(effective, 0.0) + waiting + length
        waiting = 0.0
    if not path or not any(step not in {"deliver", "setup"} for step in seconds):
        return None
    # The answer itself is the delivery.
    seconds["deliver"] = seconds.get("deliver", 0.0) + waiting
    if path == "work":
        # A file turn runs Atlas's loop; code to run starts the code engine,
        # whose start takes 20 to 40 s more (work_estimates.route).
        path = "work-file" if "document" in seconds else "work-run"
    return path, seconds


def records(directories: Iterable[Path]) -> Iterator[dict]:
    seen: set[str] = set()
    for directory in directories:
        source = directory / "records.jsonl"
        if not source.is_file():
            continue
        for line in source.read_text().splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            key = str(record.get("chat_id") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            yield record


def measures(directories: Iterable[Path]) -> list[list[object]]:
    table = label_table()
    rows: list[list[object]] = []
    for record in records(directories):
        profile = str(record.get("profile") or "")
        for turn in record.get("turns") or []:
            if turn.get("http") != 200:
                continue
            found = spans(turn, table)
            if found is None:
                continue
            path, seconds = found
            total = sum(seconds.values())
            cost = float(turn.get("cost_eur") or 0)
            success = not turn.get("error_events")
            for step, value in sorted(seconds.items()):
                share = cost * value / total if total else 0.0
                rows.append(
                    [path, step, profile, round(value, 1), round(share, 5), success]
                )
            # The whole turn, from the request to the answer.
            whole = float(turn.get("total") or total)
            rows.append(
                [path, "turn", profile, round(whole, 1), round(cost, 5), success]
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("runs", nargs="+", type=Path)
    args = parser.parse_args()
    rows = measures(args.runs)
    json.dump(
        {
            "source": "staging corpus runs (tools/corpus), status timings only",
            "created": datetime.now(UTC).strftime("%Y-%m-%d"),
            "columns": ["path", "type", "profile", "seconds", "cost_eur", "success"],
            "measures": rows,
        },
        sys.stdout,
        indent=0,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
