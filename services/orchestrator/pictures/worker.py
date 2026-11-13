"""Run picture jobs in child processes, never on the event loop.

Rendering and measuring a draft is CPU work on untrusted SVG (native renderer,
numpy masks). Each job runs in its own child process (job.py) that limits its
address space and CPU time, gets no secret in its environment and is killed
when its wall-clock limit passes. At most MAX_JOBS jobs run at once in this
process; the others wait their turn.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import weakref
from pathlib import Path
from typing import Any

MAX_JOBS = 2
DEFAULT_SECONDS = 90.0
DEFAULT_MEMORY_MB = 2048
MAX_OUTPUT_BYTES = 32_000_000
ROOT = Path(__file__).resolve().parents[3]
# Variables a child may receive; nothing else from the parent's environment.
PASSED = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "ATLAS_PICTURE_FONT_DIR",
    "ATLAS_PICTURE_TEST_HANDLERS",
)

_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


class PictureJobError(RuntimeError):
    """A job that did not finish: its code is the message."""


def _slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slot = _slots.get(loop)
    if slot is None:
        slot = _slots[loop] = asyncio.Semaphore(MAX_JOBS)
    return slot


def _number(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def child_environment(seconds: float, memory_mb: float) -> dict[str, str]:
    environment = {name: os.environ[name] for name in PASSED if name in os.environ}
    environment.update(
        {
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "HOME": "/nonexistent",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "ATLAS_PICTURE_JOB_MEMORY_MB": str(int(memory_mb)),
            "ATLAS_PICTURE_JOB_CPU_SECONDS": str(int(seconds) + 5),
        }
    )
    environment.setdefault("PATH", "/usr/bin:/bin")
    return environment


async def run_job(
    handler: str,
    payload: dict[str, Any],
    *,
    seconds: float | None = None,
    memory_mb: float | None = None,
) -> Any:
    """The handler's result, or PictureJobError(code) when the job failed,
    passed its limits or could not start."""
    limit = (
        seconds
        if seconds is not None
        else _number("ATLAS_PICTURE_JOB_SECONDS", DEFAULT_SECONDS)
    )
    memory = (
        memory_mb
        if memory_mb is not None
        else _number("ATLAS_PICTURE_JOB_MEMORY_MB", DEFAULT_MEMORY_MB)
    )
    request = json.dumps({"handler": handler, "input": payload}).encode("utf-8")
    async with _slot():
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "services.orchestrator.pictures.job",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(ROOT),
                env=child_environment(limit, memory),
            )
        except OSError:
            raise PictureJobError("picture_job_unavailable") from None
        try:
            output, _ = await asyncio.wait_for(process.communicate(request), limit)
        except TimeoutError:
            # Only this child is stopped: it is ours, known by its handle.
            process.kill()
            await process.wait()
            raise PictureJobError("picture_job_timeout") from None
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
    if process.returncode != 0:
        # Killed by its own CPU limit or by the memory allocator.
        raise PictureJobError("picture_job_killed")
    if len(output) > MAX_OUTPUT_BYTES:
        raise PictureJobError("picture_job_output_too_large")
    try:
        reply = json.loads(output)
    except ValueError:
        raise PictureJobError("picture_job_invalid_reply") from None
    if not isinstance(reply, dict):
        raise PictureJobError("picture_job_invalid_reply")
    if not reply.get("ok"):
        raise PictureJobError(str(reply.get("error") or "picture_job_failed")[:60])
    return reply.get("result")
