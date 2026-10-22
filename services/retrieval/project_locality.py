"""REQ-PROJ-01/02/06/07 P0 locality and unavailable-capability boundary.

This module deliberately contains no device, skill, scheduler, or model dispatch.
It keeps their P0 refusals and the organization-ceiling check at one boundary so a
future implementation cannot accidentally treat metadata as authority.
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
    """Return the fixed P0 capability report; unavailable features stay explicit."""
    return {
        "chat": True,
        "work": work,
        "scheduled_tasks": False,
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
    return intersection(dict(candidate), dict(ceiling)) == candidate
