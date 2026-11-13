"""Measure each finished draft and order the four candidates.

Ordering only: every finished draft is shown. The key is the number of failed
judge items plus the number of failed code checks, then the judge's quality
tie-break (round 12), then the draft's own position.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from services.orchestrator.pictures.checks import Checks, code_flags
from services.orchestrator.pictures.judge import judge

FAILED_MEASURE = {"J": 99, "Q": 0, "P": 4}


def _hidden(node: ET.Element) -> bool:
    style = node.get("style") or ""
    return (
        node.get("display") == "none"
        or re.search(r"display\s*:\s*none", style) is not None
        or node.get("visibility") == "hidden"
    )


def strip_hidden(svg: str) -> tuple[str, list[str]]:
    """Remove what the painter hid on purpose (display none, visibility hidden):
    (svg without it, removed elements as XML)."""
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return svg, []
    removed: list[str] = []

    def walk(node: ET.Element) -> None:
        for child in list(node):
            if isinstance(child.tag, str) and _hidden(child):
                removed.append(ET.tostring(child, encoding="unicode"))
                node.remove(child)
            else:
                walk(child)

    walk(root)
    return (ET.tostring(root, encoding="unicode"), removed) if removed else (svg, [])


def measure(
    svg: str, ck: dict[str, Any], living: dict[str, bool] | None
) -> dict[str, Any]:
    """Judge items failed (J), code checks failed (P, flags) and quality (Q) of
    one draft, after the body and animal kits ran on it."""
    try:
        visible, _ = strip_hidden(svg)
        _, summary = judge(visible, ck)
        checks = Checks(visible, ck, living=living)
        result = checks.run()
        result["readability"]["_chars"] = len(checks.instances(("character",)))
        flags = code_flags(result)
        return {
            "J": len(summary["failed"]),
            "Q": summary["quality"],
            "flags": flags,
            "P": sum(flags.values()),
            "judge_items": summary["items"],
            "judge_ok": summary["ok"],
            "named_groups": summary.get("named_groups"),
            "shapes": summary.get("shapes"),
            "failed": [item["key"] for item in summary["failed"]],
        }
    except Exception as error:  # noqa: BLE001 - a draft that cannot be measured
        code = type(error).__name__
        return {**FAILED_MEASURE, "flags": {}, "measure_error": code[:60]}


def rank(measures: list[dict[str, Any]]) -> list[int]:
    """Indices of the drafts, best first: fewest failed judge items and code
    checks, then the higher quality, then the earlier draft."""
    return sorted(
        range(len(measures)),
        key=lambda k: (
            measures[k]["J"] + measures[k]["P"],
            -float(measures[k]["Q"] or 0),
            k,
        ),
    )
