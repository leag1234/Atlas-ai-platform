"""Render check of a side-panel app: Atlas opens it before saying it is done.

The static checks (packages/artifact_html.py) parse the file; they cannot see
a page that throws when it loads, stays blank, or needs what the preview
blocks. This check opens the file in headless Chromium with the side view's
headers (artifact_routes.PREVIEW_CSP, the storage shim), waits for it to
settle, presses up to three visible buttons and reports, as problems:

- an uncaught error or rejected promise, at load or after a press;
- a Content Security Policy violation (a request the preview blocks);
- a blank page: no visible text, control, drawing or painted box;
- buttons on the page and no event handler anywhere (nothing can react);
- a page that does not finish loading or stops responding.

Runtime (docs/27-app-render-check.md): one throwaway container per check from
the atlas-app-render image (infra/docker/Dockerfile.app-render), without
network, read-only, as a non-root user, memory and process capped, killed at
CHECK_SECONDS. ATLAS_APP_RENDER selects it: "docker" on hosted instances,
"local" (the host's Playwright, development and tests), "off" (the default:
static checks only). When the runtime cannot run, the check says so
("unavailable") and the turn keeps its static checks: a missing browser never
fails an app.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

WORKER = Path(__file__).with_name("app_render_worker.py")
DEFAULT_IMAGE = "atlas-app-render:local"
# Whole check, browser start included (measured on the build VM: 2.2 to 2.8 s
# for a container, 1.3 to 1.8 s on the host).
CHECK_SECONDS = 15.0
# A check that cannot have this much time is not started.
MINIMUM_SECONDS = 6.0
LOAD_MS = 6000
SETTLE_MS = 800
CLICKS = 3
MEMORY = "512m"
PIDS = "256"
MAX_PROBLEMS = 6
MAX_DETAIL = 160

Status = Literal["ok", "problems", "unavailable", "skipped"]

# Problems a repair can fix in the file, in the order they are reported.
CODES = (
    "not_loaded",
    "not_responding",
    "page_error",
    "control_error",
    "csp_violation",
    "blocked_request",
    "blank_page",
    "dead_controls",
)
NETWORK_CODES = frozenset({"csp_violation", "blocked_request"})


@dataclass(frozen=True)
class RenderProblem:
    code: str
    detail: str = ""


@dataclass
class RenderReport:
    status: Status
    problems: list[RenderProblem] = field(default_factory=list)
    ms: float = 0.0
    reason: str = ""

    @property
    def codes(self) -> list[str]:
        return sorted({problem.code for problem in self.problems}, key=CODES.index)


def mode() -> str:
    value = os.environ.get("ATLAS_APP_RENDER", "off").strip().lower()
    return value if value in {"off", "docker", "local"} else "off"


def image() -> str:
    return os.environ.get("ATLAS_APP_RENDER_IMAGE", "").strip() or DEFAULT_IMAGE


_semaphore: asyncio.Semaphore | None = None


def _gate() -> asyncio.Semaphore:
    """At most ATLAS_APP_RENDER_CONCURRENCY browsers per process (default 2)."""
    global _semaphore
    if _semaphore is None:
        try:
            size = int(os.environ.get("ATLAS_APP_RENDER_CONCURRENCY", "2"))
        except ValueError:
            size = 2
        _semaphore = asyncio.Semaphore(max(1, min(size, 8)))
    return _semaphore


def settings() -> dict[str, object]:
    from services.orchestrator.artifact_routes import PREVIEW_CSP, PREVIEW_HEADERS

    return {
        # The side view's policy; frame-ancestors does not apply to a page
        # opened at the top level.
        "csp": PREVIEW_CSP + "'none'",
        "permissions": PREVIEW_HEADERS["Permissions-Policy"],
        "load_ms": LOAD_MS,
        "settle_ms": SETTLE_MS,
        "clicks": CLICKS,
    }


def served(document: str) -> bytes:
    """The bytes the side view serves for this file (with its storage shim)."""
    from services.orchestrator.artifact_routes import preview_body

    return preview_body(document.encode("utf-8"))


def _command(name: str, source: str, config: str) -> list[str]:
    if mode() == "local":
        return [sys.executable, "-I", "-c", source, config]
    return [
        "docker",
        "run",
        "--rm",
        "-i",
        "--pull",
        "never",
        "--name",
        name,
        "--label",
        "atlas.purpose=app-render",
        "--network",
        "none",
        "--memory",
        MEMORY,
        "--memory-swap",
        MEMORY,
        "--pids-limit",
        PIDS,
        "--cpus",
        "1",
        "--read-only",
        "--tmpfs",
        "/tmp:size=64m",
        "--tmpfs",
        "/home/render:size=16m",
        "--shm-size",
        "64m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        image(),
        "python",
        "-I",
        "-c",
        source,
        config,
    ]


async def _remove(name: str) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), 10)
    except (OSError, TimeoutError):
        log.warning("app_render_cleanup_failed name=%s", name)


async def _run(
    document: bytes, seconds: float
) -> tuple[list[str], dict[str, Any] | None]:
    """The stages seen and the worker's result (None when it gave none)."""
    name = "atlas-app-render-" + uuid.uuid4().hex[:12]
    command = _command(name, WORKER.read_text(encoding="utf-8"), json.dumps(settings()))
    local = mode() == "local"
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=local,
    )
    stages: list[str] = []
    result: dict[str, Any] | None = None

    async def talk() -> None:
        nonlocal result
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(document)
        await process.stdin.drain()
        process.stdin.close()
        while line := await process.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and isinstance(message.get("stage"), str):
                stages.append(message["stage"])
            elif isinstance(message, dict) and isinstance(message.get("result"), dict):
                result = message["result"]
        await process.wait()

    try:
        await asyncio.wait_for(talk(), seconds)
    except (TimeoutError, BrokenPipeError, ConnectionResetError):
        if local:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            await _remove(name)
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
    return stages, result


async def check(document: str, seconds: float = CHECK_SECONDS) -> RenderReport:
    """Open the app as the side view would and report what does not work."""
    if mode() == "off":
        return RenderReport("skipped", reason="off")
    seconds = min(seconds, CHECK_SECONDS)
    if seconds < MINIMUM_SECONDS:
        return RenderReport("skipped", reason="deadline")
    started = time.monotonic()
    body = served(document)
    try:
        async with asyncio.timeout(seconds):
            await _gate().acquire()
    except TimeoutError:
        return RenderReport("unavailable", reason="busy", ms=_ms(started))
    try:
        left = seconds - (time.monotonic() - started)
        stages, result = await _run(body, left)
    except OSError as error:
        log.warning("app_render_unavailable error=%s", type(error).__name__)
        return RenderReport("unavailable", reason="runtime", ms=_ms(started))
    finally:
        _gate().release()
    if result is None:
        if "loaded" in stages:
            # The page loaded, then never let the check finish: an endless loop.
            report = RenderReport(
                "problems", [RenderProblem("not_responding")], _ms(started)
            )
        else:
            log.warning("app_render_unavailable stages=%s", ",".join(stages) or "-")
            return RenderReport("unavailable", reason="runtime", ms=_ms(started))
    else:
        found = problems(result)
        report = RenderReport("problems" if found else "ok", found, _ms(started))
    log.info(
        "app_render status=%s codes=%s ms=%d",
        report.status,
        ",".join(report.codes) or "-",
        report.ms,
    )
    return report


def _ms(started: float) -> float:
    return (time.monotonic() - started) * 1000


def _text(value: object) -> str:
    return " ".join(str(value or "").split())[:MAX_DETAIL]


def problems(result: dict[str, Any]) -> list[RenderProblem]:
    """The problems of one worker result, most serious first, deduplicated."""
    found: list[RenderProblem] = []
    for error in result.get("errors") or []:
        if not isinstance(error, dict):
            continue
        message = _text(error.get("message"))
        if message.startswith("not_loaded"):
            found.append(RenderProblem("not_loaded"))
        elif error.get("phase") == "click":
            control = _text(error.get("control"))[:40]
            found.append(RenderProblem("control_error", f"{control}␟{message}"))
        else:
            found.append(RenderProblem("page_error", message))
    for violation in result.get("violations") or []:
        if isinstance(violation, dict):
            found.append(
                RenderProblem(
                    "csp_violation",
                    f"{_text(violation.get('directive'))}␟"
                    f"{_text(violation.get('uri'))}",
                )
            )
    if not any(problem.code == "csp_violation" for problem in found):
        for text in result.get("refused") or []:
            found.append(RenderProblem("csp_violation", "␟" + _text(text)))
    for url in result.get("blocked") or []:
        found.append(RenderProblem("blocked_request", _text(url)))
    if result.get("loaded") and not result.get("visible"):
        found.append(RenderProblem("blank_page"))
    if result.get("loaded") and int(result.get("buttons") or 0) > 0:
        if not result.get("handlers"):
            found.append(RenderProblem("dead_controls"))
    unique: list[RenderProblem] = []
    for problem in found:
        if problem not in unique:
            unique.append(problem)
    unique.sort(key=lambda problem: CODES.index(problem.code))
    return unique[:MAX_PROBLEMS]


# What the model reads to repair the file.
REPAIR_LINES = {
    "not_loaded": "the page did not finish loading within 6 seconds (an endless loop "
    "or blocking code when it loads)",
    "not_responding": "the page stopped responding after it loaded (an endless loop)",
    "page_error": "uncaught error when the page loads: {0}",
    "control_error": 'pressing the button "{0}" throws: {1}',
    "csp_violation": "blocked by the preview's security policy ({0}): {1}",
    "blocked_request": "the page tried to open another address: {0}",
    "blank_page": "the page shows nothing: no visible text, control or drawing "
    "after it loads",
    "dead_controls": "the page shows buttons but no event handler is attached "
    "anywhere, so nothing reacts",
}


def repair_lines(report: RenderReport) -> str:
    lines = []
    for problem in report.problems:
        parts = problem.detail.split("␟") if problem.detail else []
        lines.append("- " + REPAIR_LINES[problem.code].format(*parts, "", ""))
    return "\n".join(lines)
