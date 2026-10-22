"""Overlap isolated M5 evaluation with replay gates; retain every gate result."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess


def run(execute: Callable[[int], None]) -> None:
    # M5 owns an ephemeral database/port and writes only BRAIN/eval outputs.
    # M6 consumes those outputs and changes reports, so it must run last.
    with ThreadPoolExecutor(max_workers=1) as pool:
        evaluation = pool.submit(execute, 5)
        for milestone in range(7, 27):
            execute(milestone)
        evaluation.result()
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
