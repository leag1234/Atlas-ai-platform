"""REQ-PROJ-01/02/06/07 locality and project-capability boundary.

Capability discovery grants no dispatch authority. Device and skill execution
remain unavailable; scheduled Work uses the scoped project authorization path.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from services.retrieval.project_identity import ProjectError


def reject_local_only_upload(locality: str) -> None:
    """Reject before upload bytes are parsed, buffered, or persisted."""
    if locality == "local_only":
        raise ProjectError(409, "local_only_upload_forbidden")


def reject_local_only_egress(locality: str) -> None:
    """Reject local-only context/model dispatch before it can leave the device."""
    if locality == "local_only":
        raise ProjectError(409, "local_only_egress_forbidden")


def p0_capabilities(work: bool) -> dict[str, bool]:
    """Report project capabilities; unavailable features stay explicit."""
    return {
        "chat": True,
        "work": work,
        "scheduled_tasks": True,
        "skills_execute": False,
        "local_reads": False,
        "sync": False,
    }


def p0_skill_references() -> dict[str, object]:
    """P0 reserves SKILL.md references but neither loads nor executes them."""
    return {"items": [], "next_cursor": None, "capabilities": {"execute": False}}


def policy_only_tightens(
    candidate: Mapping[str, Any],
    ceiling: Mapping[str, Any],
    intersection: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
) -> bool:
    """An organization policy is valid only when it is no weaker than its ceiling."""
    return intersection(dict(candidate), dict(ceiling)) == intersection(
        dict(candidate), dict(candidate)
    )
