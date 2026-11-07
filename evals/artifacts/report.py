#!/usr/bin/env python3
"""Aggregate artefact benchmark results into a Markdown report."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


from evals.artifacts.run import STEP_NAMES


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
    return ordered[max(0, index)]


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_profile: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_profile[str(result["profile"])].append(result)
    profiles: dict[str, Any] = {}
    for profile, runs in sorted(by_profile.items()):
        steps = {
            name: sum(
                any(step["name"] == name and step["success"] for step in run["steps"])
                for run in runs
            )
            for name in STEP_NAMES
        }
        previews = [
            step["duration_seconds"]
            for run in runs
            for step in run["steps"]
            if step["name"] == "first_preview"
        ]
        changes = [
            step["duration_seconds"]
            for run in runs
            for step in run["steps"]
            if step["name"] == "change"
        ]
        profiles[profile] = {
            "runs": len(runs),
            "step_success": steps,
            "full_success": sum(bool(run["success"]) for run in runs),
            "first_preview_p50_seconds": percentile(previews, 0.50),
            "first_preview_p90_seconds": percentile(previews, 0.90),
            "first_preview_max_seconds": max(previews) if previews else None,
            "change_turnaround_p50_seconds": percentile(changes, 0.50),
            "questions_instead_of_artifacts": sum(
                not any(step["name"] == "create" and step["success"] for step in run["steps"])
                and "?" in str(run["steps"][0]["answer"]).strip()[-1:]
                for run in runs
                if run["steps"]
            ),
            "console_errors": sum(int(run["console_errors"]) for run in runs),
            "language_matches": sum(bool(run["language_match"]) for run in runs),
            "keyboard_operability": sum(bool(run["keyboard_operability"]) for run in runs),
            "touch_controls": sum(bool(run["touch_control"]) for run in runs),
            "cost_eur": sum(
                float(step["usage"]["cost_eur"])
                for run in runs
                for step in run["steps"]
            ),
            "prompt_tokens": sum(
                int(step["usage"]["prompt_tokens"])
                for run in runs
                for step in run["steps"]
            ),
            "completion_tokens": sum(
                int(step["usage"]["completion_tokens"])
                for run in runs
                for step in run["steps"]
            ),
            "reasoning_tokens": sum(
                int(step["usage"]["reasoning_tokens"])
                for run in runs
                for step in run["steps"]
            ),
        }
    return {"profiles": profiles}


def format_seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def markdown(aggregate_data: dict[str, Any]) -> str:
    lines = ["# Artefact continuation benchmark", ""]
    lines.extend(
        [
            "| Profile | Runs | Create | Interaction | Change | Version | Export | Full | Preview p50 | Preview p90 | Preview max | Change p50 | Cost EUR |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for profile, values in aggregate_data["profiles"].items():
        steps = values["step_success"]
        lines.append(
            f"| {profile} | {values['runs']} | {steps['create']} | "
            f"{steps['scripted_interaction']} | {steps['change']} | {steps['version_check']} | "
            f"{steps['export']} | {values['full_success']} | "
            f"{format_seconds(values['first_preview_p50_seconds'])} | "
            f"{format_seconds(values['first_preview_p90_seconds'])} | "
            f"{format_seconds(values['first_preview_max_seconds'])} | "
            f"{format_seconds(values['change_turnaround_p50_seconds'])} | "
            f"{values['cost_eur']:.4f} |"
        )
    lines.extend(["", "## Quality signals", ""])
    for profile, values in aggregate_data["profiles"].items():
        lines.append(
            f"- {profile}: language {values['language_matches']}/{values['runs']}, "
            f"keyboard {values['keyboard_operability']}, touch {values['touch_controls']}, "
            f"console errors {values['console_errors']}, questions {values['questions_instead_of_artifacts']}."
        )
    lines.extend(
        [
            "",
            "## Tokens",
            "",
            "| Profile | Input | Output | Reasoning |",
            "|---|---:|---:|---:|",
        ]
    )
    for profile, values in aggregate_data["profiles"].items():
        lines.append(
            f"| {profile} | {values['prompt_tokens']} | {values['completion_tokens']} | {values['reasoning_tokens']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results: list[dict[str, Any]] = []
    for path in args.results:
        value = json.loads(path.read_text())
        if not isinstance(value, list):
            raise ValueError(f"{path} does not contain a result array")
        results.extend(value)
    text = markdown(aggregate(results))
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
