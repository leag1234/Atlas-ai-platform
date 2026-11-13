"""Picture job handlers used only by tests (ATLAS_PICTURE_TEST_HANDLERS=1)."""

from __future__ import annotations

import os
import time
from typing import Any


def sleep(payload: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    time.sleep(float(payload.get("seconds", 1.0)))
    return {"slept": time.monotonic() - started}


def spin(payload: dict[str, Any]) -> int:
    count = 0
    while True:
        count += 1


def allocate(payload: dict[str, Any]) -> int:
    block = bytearray(int(payload["megabytes"]) * 1024 * 1024)
    return len(block)


def environment(payload: dict[str, Any]) -> list[str]:
    return sorted(os.environ)
