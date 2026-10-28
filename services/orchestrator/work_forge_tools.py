"""Work forge tool: freeze scratch bytes, wait inline, execute that approval only."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import tarfile
import time
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from services.orchestrator.project_forge import canonical, snapshot_files
from services.retrieval.project_identity import ProjectError


class ForgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["branch", "commit", "pull_request", "publish_change"]
    base_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    branch: str | None = None
    message: str | None = None
    head: str | None = None
    target: str | None = None
    title: str | None = None
    body: str | None = None

    def action(self) -> dict[str, Any]:
        fields = {
            "branch": {"branch"},
            "commit": {"branch", "message"},
            "pull_request": {"head", "target", "title", "body"},
            "publish_change": {"branch", "target", "message", "title", "body"},
        }[self.kind]
        if self.model_fields_set != fields | {"kind", "base_commit"} or any(
            getattr(self, f) is None for f in fields
        ):
            raise ValueError("Provide exactly the fields for the forge action")
        params = {f: getattr(self, f) for f in fields}
        result: dict[str, Any] = dict(kind=self.kind, base_commit=self.base_commit)
        if self.kind == "branch":
            params = dict(name=self.branch)
        elif self.kind == "commit":
            result["target_branch"] = params.pop("branch")
        result["parameters"] = params
        result["change_digest"] = hashlib.sha256(canonical(result)).hexdigest()
        return result


def declaration() -> dict[str, object]:
    return dict(
        type="function",
        function=dict(
            name="forge_action",
            description="After tests pass, use this tool instead of shell git commit/push/remote commands: the scratch workspace has no remote or credentials. Publish a verified change with one inline approval using kind=publish_change: base_commit, branch (new branch), target, message, title, body. Freezes the entire repository scratch snapshot and publishes branch, commit and pull request together. Separate actions are also available. kind=branch: base_commit, branch. kind=commit: base_commit, branch, message; freezes the entire repository scratch snapshot before approval. kind=pull_request: base_commit (target SHA), head, target, title, body. Omit irrelevant fields. Broker performs no remote write until approval; report only the returned state. The repository and base SHA are in code_workspace.",
            parameters=ForgeRequest.model_json_schema(),
        ),
    )


def exported_snapshot(raw: bytes, folder: str, limit: int) -> bytes:
    files: list[dict[str, str]] = []
    size = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if not path.parts or path.parts[0] != folder or member.isdir():
                continue
            if (
                not member.isfile()
                or path.is_absolute()
                or ".." in path.parts
                or ".git" in path.parts
            ):
                raise ValueError("unsafe_repository_export")
            size += member.size
            if size > limit or len(files) >= 20000:
                raise ValueError("repository_export_limit")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("invalid_repository_export")
            files.append(
                dict(
                    path=str(PurePosixPath(*path.parts[1:])),
                    mode="100755" if member.mode & 0o111 else "100644",
                    content=base64.b64encode(stream.read()).decode(),
                )
            )
    result = canonical(dict(files=sorted(files, key=lambda f: f["path"])))
    snapshot_files(result, hashlib.sha256(result).hexdigest())
    return result


async def execute(tools: Any, arguments: str, timeout: float) -> dict[str, Any]:
    args = ForgeRequest.model_validate_json(arguments)
    if args.kind == "publish_change":
        refusal = tools.verification.refusal()
        if refusal is not None:
            return dict(refusal)
    execution = tools.execution
    repository_id = execution.session.get("repository_id")
    if not repository_id:
        raise ProjectError(
            409, "feature_not_available", "No project repository selected"
        )
    status, listing = await execution.broker.public("GET", "repositories")
    if status != 200:
        raise ProjectError(status, "dependency_unavailable")
    repo = next((r for r in listing["items"] if r["id"] == repository_id), None)
    if repo is None:
        raise ProjectError(404, "not_found")
    action = args.action()
    if args.kind == "publish_change":
        # Scratch git is a synthetic import, not the remote repository history.
        # Bind the full snapshot to its stored remote source. Forge still checks
        # that the target has not moved before both approval and execution.
        action["base_commit"] = repo["base_commit"]
    data = dict(execution.fence(), operation="issue", action=action)
    if args.kind in {"commit", "publish_change"}:
        export = await tools.workspace.invoke(
            "export",
            dict(baseline=tools.workspace.metadata()["baseline"]),
            min(timeout, 120),
        )
        if export.get("error"):
            raise ValueError("repository_export_failed")
        payload = exported_snapshot(
            base64.b64decode(export["archive"], validate=True),
            repo["folder_id"],
            execution.session["effective_limits"]["expanded_bytes"],
        )
        digest = hashlib.sha256(payload).hexdigest()
        action.update(
            change_digest=digest, snapshot=dict(id=str(uuid4()), sha256=digest)
        )
        data["snapshot_content"] = base64.b64encode(payload).decode()
    pending = await execution.broker.call("forge", data)
    approval = pending["approval"]
    await tools.emit(
        "approval",
        tool="forge_action",
        label="Approve repository " + args.kind,
        status="running",
        output=dict(approval=approval),
    )
    deadline = time.monotonic() + min(timeout, 120)
    operation = dict(execution.fence(), operation_id=pending["operation_id"])
    while time.monotonic() < deadline:
        await execution.check()
        current = await execution.broker.call(
            "forge", dict(operation, operation="status")
        )
        decision = current["approval"]["decision"]
        if decision != "pending":
            break
        await asyncio.sleep(0.25)
    else:
        return dict(
            error="approval_expired",
            state="not_sent",
            approval=dict(approval, decision="expired"),
        )
    if decision != "allowed":
        return dict(
            error="approval_" + decision, state="not_sent", approval=current["approval"]
        )
    await execution.broker.call("forge", dict(operation, operation="execute"))
    while time.monotonic() < deadline:
        current = await execution.broker.call(
            "forge", dict(operation, operation="status")
        )
        if current["state"] not in {"pending", "running"}:
            return dict(
                operation_id=current["operation_id"],
                state=current["state"],
                commit=current["commit"],
                approval=current["approval"],
                pr_url=current.get("pr_url"),
                failed_step=current.get("failed_step"),
                completed_steps=current.get("completed_steps", []),
                **(
                    {"error": "forge_" + current["state"]}
                    if current["state"] != "succeeded"
                    else {}
                ),
            )
        await asyncio.sleep(0.25)
    return dict(
        operation_id=pending["operation_id"],
        state="unknown",
        error="forge_outcome_unknown",
        approval=current["approval"],
        failed_step=current.get("failed_step"),
        completed_steps=current.get("completed_steps", []),
    )
