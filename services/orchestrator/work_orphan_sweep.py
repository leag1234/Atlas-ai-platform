"""Startup sweep of orphaned Work containers (label atlas.work=<scope>).

A restart of the orchestrator during a Work turn can leave an opencode engine
container (atlas-opencode-*), an egress proxy (atlas-work-egress-*) or a
worker container (atlas-code-*) running with no session behind it. At service
start the orchestrator removes such containers of its own instance only: the
name carries this instance's port-base suffix, and a container older than the
maximum turn wall clock plus a margin cannot belong to a running turn. A Work
session that is not running anymore leaves exactly those aged containers
behind, so the age check covers it without a database read at startup.
Containers of another port base are never touched.
"""

import asyncio
from datetime import datetime, timezone
import logging
import os

from packages.ports import atlas_ports

# Container name prefixes owned by the Work runtime (see code_workspace.py and
# work_opencode_container.py).
CONTAINER_PREFIXES = ("atlas-opencode-", "atlas-work-egress-", "atlas-code-")


def max_turn_seconds() -> float:
    """Maximum wall clock of one Work turn (code_mode settings default)."""
    try:
        return max(1.0, float(os.environ.get("ATLAS_CODE_WALL_SECONDS", "1200")))
    except ValueError:
        return 1200.0


def sweep_margin_seconds() -> float:
    """Extra age margin on top of the turn wall clock."""
    try:
        return max(0.0, float(os.environ.get("ATLAS_WORK_SWEEP_MARGIN_SECONDS", "600")))
    except ValueError:
        return 600.0


def max_age_seconds() -> float:
    return max_turn_seconds() + sweep_margin_seconds()


def _suffix() -> str:
    base = atlas_ports().base
    return "" if base == 0 else "-" + str(base)


def is_own_container(name: str) -> bool:
    """True when the name belongs to this instance's port base only."""
    suffix = _suffix()
    for prefix in CONTAINER_PREFIXES:
        if not name.startswith(prefix):
            continue
        middle = name[len(prefix) : len(name) - len(suffix)]
        if name.endswith(suffix) and "-" not in middle:
            return True
    return False


async def _docker(*args: str) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode or 0, stdout, stderr


def _created_seconds(value: str, now: float) -> float:
    stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return now - stamp.timestamp()


async def sweep(now: float | None = None) -> list[str]:
    """Remove this instance's orphaned Work containers; return their names.

    Never raises: a failed sweep must not block service start.
    """
    logger = logging.getLogger(__name__)
    moment = now if now is not None else datetime.now(timezone.utc).timestamp()
    try:
        code, stdout, _ = await _docker(
            "docker", "ps", "-aq", "--filter", "label=atlas.work"
        )
        if code:
            logger.warning("work_orphan_sweep_list_failed code=%s", code)
            return []
        ids = stdout.decode().split()
        if not ids:
            return []
        code, stdout, _ = await _docker(
            "docker",
            "inspect",
            "--format",
            "{{.Name}}\t{{.Created}}",
            *ids,
        )
        if code:
            logger.warning("work_orphan_sweep_inspect_failed code=%s", code)
            return []
        limit = max_age_seconds()
        orphans: list[str] = []
        for line in stdout.decode().splitlines():
            name, _, created = line.partition("\t")
            name = name.lstrip("/")
            if not name or not created or not is_own_container(name):
                continue
            try:
                age = _created_seconds(created, moment)
            except ValueError:
                continue
            if age > limit:
                orphans.append(name)
        if orphans:
            code, _, _ = await _docker("docker", "rm", "-f", *orphans)
            if code:
                logger.warning("work_orphan_sweep_remove_failed code=%s", code)
                return []
            logger.info("work_orphan_sweep_removed names=%s", ",".join(orphans))
        return orphans
    except OSError as exc:
        logger.warning("work_orphan_sweep_failed kind=%s", type(exc).__name__)
        return []


async def sweep_at_startup() -> list[str]:
    """Entry point called once when the orchestrator service starts."""
    return await sweep()
