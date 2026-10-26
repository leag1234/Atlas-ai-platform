"""Execution-fenced adapter from Work to the repository action broker."""

import base64
import hashlib
from typing import Any
from uuid import uuid4

from services.orchestrator.project_forge import ForgeError, ForgeUnknown, snapshot_files
from services.retrieval.project_forge import RepositoryActions
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_store import Scope


def execute_forge(scope: Scope, data: dict[str, Any]) -> dict[str, Any]:
    broker = RepositoryActions(scope)
    fence = {k: data[k] for k in ("session_id", "request_id", "generation")}
    session, _ = broker.work.fence(fence)
    repository_id = session["repository_id"]
    if not repository_id:
        raise ProjectError(409, "feature_not_available", "No repository selected")
    try:
        if data["operation"] == "issue":
            if set(data) != set(fence) | {"operation", "action"} | (
                {"snapshot_content"} if "snapshot_content" in data else set()
            ):
                raise ProjectError(422, "invalid_request")
            action = dict(data["action"])
            if "approval_id" in action:
                raise ProjectError(422, "invalid_request")
            if action["kind"] in {"commit", "publish_change"}:
                repository = broker.repository(repository_id)
                selected = {
                    (v["file_id"], v["version_id"])
                    for v in session["base_file_versions"]
                }
                required = {
                    (f["id"], f["version_id"])
                    for f in broker.files.listing("file")
                    if f["folder_id"] == repository["folder_id"]
                }
                if not required.issubset(selected):
                    raise ProjectError(
                        422,
                        "invalid_request",
                        "Select every repository file before publishing a complete snapshot",
                    )
                content = base64.b64decode(
                    data.get("snapshot_content", ""), validate=True
                )
                digest = hashlib.sha256(content).hexdigest()
                if (
                    action["change_digest"] != digest
                    or action["snapshot"]["sha256"] != digest
                ):
                    raise ProjectError(409, "revision_conflict")
                snapshot_files(content, digest)
                folder = broker.files.get(
                    "folder", scope.project_data["deliverables_folder_id"]
                )
                file = broker.files.create_file(
                    folder["id"],
                    "repository-snapshot-" + str(uuid4()) + ".json",
                    content,
                    folder["revision"],
                )
                action["snapshot"] = dict(id=file["version_id"], sha256=digest)
            elif "snapshot_content" in data:
                raise ProjectError(422, "invalid_request")
            return broker.submit(repository_id, action, fence)[1]
        if set(data) != set(fence) | {"operation", "operation_id"}:
            raise ProjectError(422, "invalid_request")
        operation = broker.operation(data["operation_id"])
        if (
            operation.get("fence") != fence
            or operation["repository"]["id"] != repository_id
        ):
            raise ProjectError(404, "not_found")
        approval, _ = broker.current(operation)
        if data["operation"] == "execute":
            return broker.submit(
                repository_id, dict(operation["action"], approval_id=approval["id"])
            )[1]
        return dict(
            operation_id=operation["id"],
            state=operation["state"],
            approval=approval,
            commit=operation["commit"],
            pr_url=operation.get("pr_url"),
            failed_step=operation.get("failed_step"),
            completed_steps=operation.get("completed_steps", []),
        )
    except ForgeUnknown:
        raise ProjectError(503, "dependency_unavailable") from None
    except (ForgeError, ValueError):
        raise ProjectError(409, "revision_conflict") from None
