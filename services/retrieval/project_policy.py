"""Closed task-B inputs, monotone policy and measured quota failures."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from uuid import UUID
import re

from services.retrieval.project_identity import ProjectError

Positive = Annotated[int, Field(ge=1)]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Name = Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WorkLimits(Closed):
    tool_calls: int = Field(ge=1, le=40)
    model_calls: int = Field(ge=1, le=41)
    wall_seconds: int = Field(ge=1, le=1200)
    cost_eur: float = Field(ge=0, le=3, allow_inf_nan=False)
    per_call_eur: float = Field(ge=0, le=0.1, allow_inf_nan=False)
    expanded_bytes: int = Field(ge=1, le=209715200)
    archive_entries: int = Field(ge=1, le=20000)
    text_tool_bytes: int = Field(ge=1, le=262144)
    command_output_bytes: int = Field(ge=1, le=32768)
    command_seconds: int = Field(ge=1, le=120)
    command_memory_bytes: int = Field(ge=1, le=536870912)


class Quotas(Closed):
    active_members: Positive
    active_projects: Positive
    stored_bytes: Positive
    versioned_bytes: Positive
    derived_bytes: Positive
    temporary_bytes: Positive
    upload_count: Positive
    concurrent_jobs: Positive
    upload_bytes: int = Field(ge=1, le=16777216)
    daily_model_eur: Nonnegative
    monthly_model_eur: Nonnegative
    per_turn: WorkLimits


class Retention(Closed):
    content_expiry_days: Positive | None = None
    deletion_grace_days: int = Field(default=30, ge=0)
    temporary_after_terminal_hours: int = Field(default=24, ge=0)
    audit_days: Positive = 90
    backup_expiry_days: int = Field(default=30, ge=0, le=30)
    superseded_versions_days: Positive | None = None
    legal_hold: bool = False


class Policy(Closed):
    retention: Retention
    quotas: Quotas
    image_safety_floor: Literal["strict", "standard"]
    connector_allowlist: list[str]
    skill_allowlist: list[str]
    local_egress: Literal[False] = False

    @field_validator("connector_allowlist", "skill_allowlist")
    @classmethod
    def unique_entries(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Duplicate policy entry")
        return value

    @field_validator("connector_allowlist")
    @classmethod
    def connector_ids(cls, value: list[str]) -> list[str]:
        for item in value:
            UUID(item)
        return value

    @field_validator("skill_allowlist")
    @classmethod
    def skill_digests(cls, value: list[str]) -> list[str]:
        if any(re.fullmatch(r"[a-f0-9]{64}", item) is None for item in value):
            raise ValueError("Invalid skill digest")
        return value


class PolicyPatch(Closed):
    retention: Retention | None = None
    quotas: Quotas | None = None
    image_safety_floor: Literal["strict", "standard"] | None = None
    connector_allowlist: list[str] | None = None
    skill_allowlist: list[str] | None = None


class MemberCreate(Closed):
    identity_subject: Annotated[str, Field(min_length=1)]
    role: Literal["admin", "member"]


class MemberPatch(Closed):
    role: Literal["admin", "member"] | None = None
    status: Literal["active", "revoked"] | None = None


class ProjectCreate(Closed):
    name: Name
    instructions: str = Field(default="", max_length=4000)
    default_action: Literal["chat", "work"] = "chat"


class ProjectPatch(Closed):
    name: Name | None = None
    default_action: Literal["chat", "work"] | None = None
    state: Literal["active", "archived"] | None = None


class GrantPut(Closed):
    role: Literal["owner", "editor", "viewer"]


def intersect(*policies: dict[str, Any]) -> dict[str, Any]:
    """Permissions intersect and numerical ceilings take their minimum."""
    result = Policy.model_validate(policies[0]).model_dump()
    for raw in policies[1:]:
        policy = Policy.model_validate(raw).model_dump()
        for key, value in result["quotas"].items():
            other = policy["quotas"][key]
            result["quotas"][key] = (
                {k: min(v, other[k]) for k, v in value.items()}
                if isinstance(value, dict)
                else min(value, other)
            )
        for key, value in result["retention"].items():
            other = policy["retention"][key]
            if key == "legal_hold":
                result["retention"][key] = value or other
            else:
                result["retention"][key] = (
                    other
                    if value is None
                    else value
                    if other is None
                    else min(value, other)
                )
        for key in ("connector_allowlist", "skill_allowlist"):
            result[key] = sorted(set(result[key]) & set(policy[key]))
        if policy["image_safety_floor"] == "strict":
            result["image_safety_floor"] = "strict"
    return result


def quota(resource: str, measured: float, limit: float) -> None:
    if measured <= limit:
        return
    unit = (
        "EUR"
        if resource.endswith("eur")
        else "bytes"
        if resource.endswith("bytes")
        else {
            "active_members": "members",
            "active_projects": "projects",
            "upload_count": "files",
            "concurrent_jobs": "jobs",
        }[resource]
    )
    raise ProjectError(
        429,
        "budget_exceeded" if unit == "EUR" else "quota_exceeded",
        f"{resource}: measured {measured:g} {unit}; limit {limit:g} {unit}",
        measurements=[{"measured": measured, "limit": limit, "unit": unit}],
    )
