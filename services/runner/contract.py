"""Strict additive boundaries for REQ-RUN-02/05/06."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from services.runner.policy import RunnerError, commands


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Target(Input):
    name: str = Field(min_length=1, max_length=120)
    os: Literal["linux", "macos"] = "linux"
    arch: str = Field(default="unknown", max_length=100)
    shell: Literal["posix"] = "posix"
    host: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(pattern=r"^[a-z_][a-z0-9_-]{0,63}$")
    work_root: str = Field(pattern=r"^/[A-Za-z0-9_/-]+$", max_length=500)
    credential_ref: str = Field(pattern=r"^[a-f0-9-]{36}$")
    credential_fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")
    host_key_fingerprints: list[str] = Field(min_length=1, max_length=8)
    jump_target_id: str | None = Field(default=None, pattern=r"^[a-f0-9-]{36}$")
    capabilities: list[str] = Field(default_factory=list, max_length=20)
    permission_mode: Literal["ask", "rules", "auto"] = "rules"
    allow_rules: list[str] = Field(default_factory=list, max_length=100)
    deny_rules: list[str] = Field(default_factory=list, max_length=100)
    sensitive_rules: list[str] = Field(default_factory=list, max_length=100)
    max_parallel_sessions: int = Field(default=2, ge=1, le=16)
    disk_quota_bytes: int = Field(default=1073741824, ge=1048576, le=10737418240)
    monthly_minutes: int = Field(default=300, ge=1, le=100000)
    same_locality: bool = False

    @field_validator("host_key_fingerprints")
    @classmethod
    def fingerprints(cls, values: list[str]) -> list[str]:
        import re

        if any(not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", v) for v in values):
            raise ValueError("invalid_fingerprint")
        return values

    @field_validator("work_root")
    @classmethod
    def root(cls, value: str) -> str:
        if (
            value == "/"
            or "//" in value
            or any(p in {".", ".."} for p in value.split("/"))
        ):
            raise ValueError("invalid_work_root")
        return value.rstrip("/")

    @field_validator("allow_rules", "deny_rules", "sensitive_rules")
    @classmethod
    def rules(cls, values: list[str]) -> list[str]:
        import shlex

        for value in values:
            if not value or len(value) > 1000 or not shlex.split(value):
                raise ValueError("invalid_rule")
        return values


class ListTargets(Input):
    capability: str | None = Field(default=None, max_length=100)


class RemoteRun(Input):
    target: str = Field(min_length=1, max_length=120)
    command: str = Field(min_length=1, max_length=15000)
    cwd: str = Field(default=".", max_length=1000)
    timeout_s: int = Field(default=600, ge=1, le=1800)
    wait_s: int = Field(default=5, ge=0, le=30)

    @field_validator("command")
    @classmethod
    def parsed(cls, value: str) -> str:
        try:
            commands(value)
        except RunnerError as exc:
            raise ValueError(str(exc)) from None
        return value

    @field_validator("cwd")
    @classmethod
    def relative(cls, value: str) -> str:
        from pathlib import PurePosixPath

        if (
            value.startswith("/")
            or ".." in PurePosixPath(value).parts
            or "\\" in value
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("invalid_cwd")
        return value


class RemoteStatus(Input):
    command_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    wait_s: int = Field(default=5, ge=0, le=30)
    cancel: bool = False


class RemoteFetch(Input):
    target: str = Field(min_length=1, max_length=120)
    path_or_glob: str = Field(min_length=1, max_length=1000)


SCHEMAS: dict[str, type[Input]] = {
    "list_targets": ListTargets,
    "remote_run": RemoteRun,
    "remote_status": RemoteStatus,
    "remote_fetch": RemoteFetch,
}


class Execution(Input):
    session_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    request_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    generation: int = Field(ge=1)
    operation: Literal["list", "prepare", "execute", "status", "fetch", "close"]
    arguments: dict[str, object] = Field(default_factory=dict)
    snapshot_content: str | None = Field(default=None, max_length=44739244)

    @model_validator(mode="after")
    def operation_fields(self) -> "Execution":
        from uuid import UUID

        UUID(self.session_id)
        UUID(self.request_id)
        if (self.operation == "prepare") != (self.snapshot_content is not None):
            raise ValueError("snapshot_required_only_for_prepare")
        variants: dict[str, type[Input]] = {
            "list": ListTargets,
            "prepare": RemoteRun,
            "execute": RemoteStatus,
            "status": RemoteStatus,
            "fetch": RemoteFetch,
        }
        schema = variants.get(self.operation)
        if schema is None:
            if self.arguments:
                raise ValueError("unexpected_arguments")
        else:
            schema.model_validate(self.arguments)
        return self
