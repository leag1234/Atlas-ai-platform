"""Entry point of a picture job, run by worker.py in its own child process.

The child limits itself first (address space and CPU time), then reads one
JSON request on stdin, {"handler": name, "input": ...}, runs the named
handler and writes one JSON reply on stdout: {"ok": true, "result": ...} or
{"ok": false, "error": code}. It gets no secret: worker.py passes a minimal
environment.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import resource
import sys
from collections.abc import Callable
from typing import Any, cast

MAX_INPUT_BYTES = 8_000_000

# Handlers a job may name: "module:function" under the picture package.
HANDLERS = {
    "render": "services.orchestrator.pictures.job:render_job",
    "draft": "services.orchestrator.pictures.chain:process_draft",
    "sheet": "services.orchestrator.pictures.chain:contact_sheet",
}
TEST_PREFIX = "tests."


def _limit(name: str, kind: int, scale: int) -> None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return
    value = int(float(raw) * scale)
    resource.setrlimit(kind, (value, value))


def render_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Sanitise one SVG and render it: {"svg", "size"} -> {"svg", "png"}."""
    from services.orchestrator.pictures import render
    from services.orchestrator.pictures.sanitise import sanitise

    svg = sanitise(str(payload["svg"]))
    size = int(payload.get("size", 1024))
    if not 16 <= size <= 2048:
        raise ValueError("invalid_size")
    data = render.png(svg, size, background=payload.get("background") or None)
    return {"svg": svg, "png": base64.b64encode(data).decode("ascii")}


def _handler(name: str) -> Callable[[dict[str, Any]], Any]:
    target = HANDLERS.get(name)
    if target is None and os.environ.get("ATLAS_PICTURE_TEST_HANDLERS") == "1":
        if name.startswith(TEST_PREFIX):
            target = name
    if target is None:
        raise ValueError("unknown_handler")
    module, function = target.split(":", 1)
    return cast(
        Callable[[dict[str, Any]], Any],
        getattr(importlib.import_module(module), function),
    )


def main() -> int:
    _limit("ATLAS_PICTURE_JOB_MEMORY_MB", resource.RLIMIT_AS, 1024 * 1024)
    _limit("ATLAS_PICTURE_JOB_CPU_SECONDS", resource.RLIMIT_CPU, 1)
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        sys.stdout.write(json.dumps({"ok": False, "error": "input_too_large"}))
        return 0
    try:
        request = json.loads(raw)
        handler = _handler(str(request["handler"]))
        result = handler(request.get("input") or {})
    except MemoryError:
        sys.stdout.write(json.dumps({"ok": False, "error": "memory_limit"}))
        return 0
    except Exception as error:  # noqa: BLE001 - reported as a code, never a trace
        code = str(error) if str(error).isidentifier() else type(error).__name__
        sys.stdout.write(json.dumps({"ok": False, "error": code[:60]}))
        return 0
    sys.stdout.write(json.dumps({"ok": True, "result": result}, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
