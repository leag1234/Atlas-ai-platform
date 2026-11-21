"""SVG files named for an app, turned into PNG pictures in an isolated browser.

An SVG file is a document: it can carry script, event handlers, references to
other addresses and CSS that fetches. The app embeds pictures, not documents,
so each SVG the person names is drawn once, as an image, in headless Chromium
(services/orchestrator/svg_raster_worker.py) and only the PNG pixels enter the
app. The isolation is the render check's (services/orchestrator/app_render.py,
docs/27-app-render-check.md): one throwaway container from the
atlas-app-render image, ``--network none``, read-only, non-root, memory and
process capped, killed at the deadline; ``ATLAS_APP_RENDER`` selects the
runtime ("docker" on hosted instances, "local" for development and tests).
Inside, the SVG is drawn in image mode (no script runs, nothing loads) on a
page whose policy allows data: images only, and every request is aborted.

Each file that cannot be drawn safely is a named failure, never a silent drop:
``svg_invalid`` (not an SVG document, or a declaration the parser refuses),
``svg_too_complex`` (more than MAX_ELEMENTS elements), ``svg_blank`` (nothing
is drawn once references to other documents are taken out),
``svg_render_failed``, ``svg_no_renderer`` (the isolated browser is off or
cannot run here) and ``svg_timeout``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from services.orchestrator import app_render

log = logging.getLogger(__name__)

WORKER = Path(__file__).with_name("svg_raster_worker.py")
MIME = "image/svg+xml"
# Longest side of the PNG: small drawings are scaled up to MIN_SIDE (an SVG
# is sharp at any size), large ones down to MAX_SIDE.
MAX_SIDE = 800
MIN_SIDE = 256
MAX_ELEMENTS = 40_000
ITEM_MS = 5000
# The whole conversion, browser start included (about 2.1 s in a container
# on the build VM for five files).
SECONDS = 20.0
MINIMUM_SECONDS = 4.0
# One output line carries a whole PNG in base64.
LINE_LIMIT = 8 * 1024 * 1024
FAILURES = (
    "svg_invalid",
    "svg_too_complex",
    "svg_blank",
    "svg_render_failed",
    "svg_no_renderer",
    "svg_timeout",
)

_ROOT = re.compile(rb"<svg[\s>/]", re.IGNORECASE)
_HTML = re.compile(rb"<html[\s>]", re.IGNORECASE)


@dataclass(frozen=True)
class Raster:
    """One converted file: the PNG and its size, or the reason it is not."""

    png: bytes = b""
    width: int = 0
    height: int = 0
    # References to other documents taken out of the drawing.
    left_out: int = 0
    error: str = ""


def is_svg(data: bytes) -> bool:
    """Whether the bytes look like an SVG document (the drawing decides)."""
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if not head.startswith(b"<") or _HTML.search(head):
        return False
    return _ROOT.search(head) is not None


def settings() -> dict[str, int]:
    return {
        "max_side": MAX_SIDE,
        "min_side": MIN_SIDE,
        "max_elements": MAX_ELEMENTS,
        "item_ms": ITEM_MS,
    }


async def rasterise(files: list[bytes], seconds: float = SECONDS) -> list[Raster]:
    """Each SVG as a PNG, or the named reason it could not be converted."""
    if not files:
        return []
    if app_render.mode() == "off":
        return [Raster(error="svg_no_renderer") for _ in files]
    seconds = min(seconds, SECONDS)
    if seconds < MINIMUM_SECONDS:
        return [Raster(error="svg_timeout") for _ in files]
    started = time.monotonic()
    gate = app_render._gate()
    try:
        async with asyncio.timeout(seconds):
            await gate.acquire()
    except TimeoutError:
        return [Raster(error="svg_timeout") for _ in files]
    try:
        left = seconds - (time.monotonic() - started)
        found, launched, finished = await _run(files, left)
    except OSError as error:
        log.warning("svg_raster_unavailable error=%s", type(error).__name__)
        return [Raster(error="svg_no_renderer") for _ in files]
    finally:
        gate.release()
    if not launched:
        # No browser started: no image, no Docker, or a broken runtime.
        log.warning("svg_raster_unavailable files=%d", len(files))
        return [Raster(error="svg_no_renderer") for _ in files]
    results = [
        found.get(
            index, Raster(error="svg_timeout" if not finished else "svg_render_failed")
        )
        for index in range(len(files))
    ]
    log.info(
        "svg_raster files=%d failed=%s ms=%d",
        len(files),
        ",".join(result.error for result in results if result.error) or "-",
        (time.monotonic() - started) * 1000,
    )
    return results


def _result(message: dict[str, object]) -> Raster:
    error = str(message.get("error") or "")
    if error:
        return Raster(error=error if error in FAILURES else "svg_render_failed")
    try:
        png = base64.b64decode(str(message.get("png") or ""), validate=True)
    except ValueError:
        return Raster(error="svg_render_failed")
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return Raster(error="svg_render_failed")
    return Raster(
        png,
        _int(message.get("width")),
        _int(message.get("height")),
        _int(message.get("left_out")),
    )


def _int(value: object) -> int:
    try:
        return max(0, int(str(value)))
    except ValueError:
        return 0


async def _run(
    files: list[bytes], seconds: float
) -> tuple[dict[int, Raster], bool, bool]:
    """The converted files by index, whether the browser started and whether
    the worker finished."""
    name = "atlas-svg-raster-" + uuid.uuid4().hex[:12]
    command = app_render._command(
        name, WORKER.read_text(encoding="utf-8"), json.dumps(settings())
    )
    local = app_render.mode() == "local"
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=local,
        limit=LINE_LIMIT,
    )
    payload = json.dumps(
        {"items": [base64.b64encode(data).decode() for data in files]}
    ).encode()
    found: dict[int, Raster] = {}
    launched = finished = False

    async def talk() -> None:
        nonlocal launched, finished
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(payload)
        await process.stdin.drain()
        process.stdin.close()
        while line := await process.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            index = message.get("item")
            if message.get("stage") == "launched":
                launched = True
            elif isinstance(index, int) and 0 <= index < len(files):
                found[index] = _result(message)
            elif isinstance(message.get("done"), dict):
                finished = True
                blocked = message["done"].get("blocked") or []
                if blocked:
                    # Image mode loads nothing: a request here is a finding.
                    log.warning("svg_raster_blocked count=%d", len(blocked))
        await process.wait()

    try:
        await asyncio.wait_for(talk(), seconds)
    except (TimeoutError, BrokenPipeError, ConnectionResetError, ValueError):
        if local:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            await app_render._remove(name)
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
    return found, launched, finished
