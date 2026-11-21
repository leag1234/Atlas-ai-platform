"""Share each painter draft's child-process result across picture tests.

A draft job (pictures/chain.py:process_draft) renders and measures one SVG in
a child process and takes several seconds of CPU. The picture turn tests send
the same fixture drafts through it dozens of times. The child is a pure
function of its request: it receives only the handler name, the payload, its
limits and the variables of worker.PASSED, and runs with PYTHONHASHSEED=0.

memoise_drafts(case) makes the first occurrence of each distinct draft run in
a real child process, exactly as before, and gives later identical requests
the same result without starting a new child. Failures are never kept: a
draft that fails, times out or is cancelled fails again the next time.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import unittest
from typing import Any
from unittest.mock import patch

from services.orchestrator.pictures import cpu_turn, followup, turn, worker

MEMOISED = frozenset({"draft"})
_results: dict[str, Any] = {}
_original = worker.run_job


def _key(handler: str, payload: dict[str, Any], options: dict[str, Any]) -> str:
    environment = {name: os.environ.get(name) for name in worker.PASSED}
    request = [handler, payload, options, environment]
    encoded = json.dumps(request, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def run_job(handler: str, payload: dict[str, Any], **options: Any) -> Any:
    if handler not in MEMOISED:
        return await _original(handler, payload, **options)
    key = _key(handler, payload, options)
    if key in _results:
        # Still a suspension point, as the child process would be.
        await asyncio.sleep(0)
        return copy.deepcopy(_results[key])
    result = await _original(handler, payload, **options)
    _results[key] = copy.deepcopy(result)
    return result


def memoise_drafts(case: unittest.TestCase) -> None:
    """Patch every module that runs picture jobs for this test."""
    for module in (turn, followup, cpu_turn):
        patcher = patch.object(module, "run_job", run_job)
        patcher.start()
        case.addCleanup(patcher.stop)
