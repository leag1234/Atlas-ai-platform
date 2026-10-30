"""Overlap isolated M5 evaluation with replay gates; retain every gate result."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess


def selected() -> list[int]:
    """Milestones of this CI shard: ATLAS_CI_MILESTONES="7-12,14" (default 5-26)."""
    value = os.environ.get("ATLAS_CI_MILESTONES") or "5-26"
    milestones: list[int] = []
    for part in value.split(","):
        first, _, last = part.strip().partition("-")
        milestones.extend(range(int(first), int(last or first) + 1))
    return milestones


def run(execute: Callable[[int], None], milestones: list[int] | None = None) -> None:
    chosen = selected() if milestones is None else milestones
    # M5 owns an ephemeral database/port and writes only BRAIN/eval outputs.
    # M6 consumes those outputs and changes reports, so it must run last and
    # always in the same shard as M5.
    if (5 in chosen) != (6 in chosen):
        raise ValueError("M5 and M6 must run in the same shard")
    with ThreadPoolExecutor(max_workers=1) as pool:
        evaluation = pool.submit(execute, 5) if 5 in chosen else None
        for milestone in range(7, 27):
            if milestone in chosen:
                execute(milestone)
        if evaluation is not None:
            evaluation.result()
    if 6 in chosen:
        execute(6)


def verify(milestone: int) -> None:
    environment = dict(os.environ)
    if milestone in (5, 6, 7, 8, 12):
        environment[f"ATLAS_M{milestone}_MODE"] = "replay"
    if milestone in (8, 12):
        environment["SCW_GENERATIVE_API_KEY"] = "test-only"
    subprocess.run(["make", f"verify-m{milestone}"], env=environment, check=True)


if __name__ == "__main__":
    run(verify)
