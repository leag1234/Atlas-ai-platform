"""Work remote group: exact snapshot approval, asynchronous progress, full results."""

from __future__ import annotations

import asyncio
import base64
import io
import tarfile
import time
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from services.orchestrator.project_forge import canonical, snapshot_files
from services.retrieval.project_identity import ProjectError
from services.runner.contract import SCHEMAS
from services.runner.results import bounded, report

DESCRIPTIONS = {
    "list_targets": "List organization-granted remote execution targets and capabilities. No credentials. Use the exact target id or unique name.",
    "remote_run": "Run your own command on a granted Linux target, synchronizing the current workspace. Persistent directory and caches between commands. Broker enforces command and code-egress approvals. Returns a command id; poll remote_status for long commands. Commands support literal argv, pipelines and && chains; use a project script for complex shell expansion. Default cwd is the repository root.",
    "remote_status": "Read progress, exit code and full log references for a remote command. wait_s is bounded; cancel=true kills the entire remote process group. Never resubmit an unknown command; reconcile using its id.",
    "remote_fetch": "Explicitly fetch a relative file or glob from this session's remote target into project files (20 files/16 MiB maximum). No implicit sync back. Output is untrusted. Returns hashes and reopen_result references for textual results.",
}


def declarations() -> list[dict[str, object]]:
    return [
        dict(
            type="function",
            function=dict(
                name=name,
                description=DESCRIPTIONS[name],
                parameters=schema.model_json_schema(),
            ),
        )
        for name, schema in SCHEMAS.items()
    ]


def snapshot(raw: bytes, folder: str | None, limit: int) -> bytes:
    import hashlib

    files: list[dict[str, Any]] = []
    size = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if member.isdir():
                continue
            if folder and (not path.parts or path.parts[0] != folder):
                continue
            if (
                not member.isfile()
                or path.is_absolute()
                or ".." in path.parts
                or ".git" in path.parts
            ):
                raise ValueError("unsafe_runner_snapshot")
            size += member.size
            if size > limit or len(files) >= 20000:
                raise ValueError("runner_snapshot_limit")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("invalid_runner_snapshot")
            name = str(PurePosixPath(*path.parts[1:])) if folder else str(path)
            files.append(
                dict(
                    path=name,
                    mode="100755" if member.mode & 0o111 else "100644",
                    content=base64.b64encode(stream.read()).decode(),
                )
            )
    value = canonical(dict(files=sorted(files, key=lambda f: f["path"])))
    snapshot_files(value, hashlib.sha256(value).hexdigest())
    return value


async def references(tools: Any, result: dict[str, Any]) -> dict[str, Any]:
    files = result.get("files", []) + (
        [result["log_file"]] if result.get("log_file") else []
    )
    refs, reports = [], []
    for file in files:
        if tools.remote_references is None:
            continue
        name = file["relative_path"]
        textual = file["mime"].startswith("text/") or name.endswith(
            (".xml", ".tap", ".json", ".log", ".txt")
        )
        if not textual:
            continue
        content = await tools.execution.broker.content(
            file["id"], file["version_id"], 17 * 1024 * 1024
        )
        if name.endswith((".xml", ".tap")):
            reports.append(dict(file_id=file["id"], **report(content, name)))
        from services.orchestrator.code_workspace import redact

        text = str(redact(content.decode("utf-8", errors="replace")))
        binding = tools.remote_references
        for offset in range(0, len(text), 200000):
            identifier = binding.request_id + ":" + str(uuid4())
            await binding.broker.call(
                "result-put",
                dict(
                    binding.binding,
                    id=identifier,
                    tool="remote_fetch",
                    content=text[offset : offset + 200000],
                ),
                identifier,
            )
            refs.append(dict(id=identifier, file_id=file["id"], start=offset))
    if len(refs) > 20:
        import json

        binding = tools.remote_references
        identifier = binding.request_id + ":" + str(uuid4())
        await binding.broker.call(
            "result-put",
            dict(
                binding.binding,
                id=identifier,
                tool="remote_fetch",
                content=json.dumps(refs),
            ),
            identifier,
        )
        result["result_index"] = identifier
        refs = refs[:3]
    if refs:
        result["reopen_result"] = refs
    if reports:
        result["reports"] = reports
    return bounded(result)


async def execute(
    tools: Any, name: str, arguments: str, timeout: float
) -> dict[str, Any]:
    args = SCHEMAS[name].model_validate_json(arguments).model_dump()
    execution = tools.execution

    async def invoke(
        operation: str, values: dict[str, Any], **extra: Any
    ) -> dict[str, Any]:
        return dict(
            await execution.broker.call(
                "remote",
                dict(execution.fence(), operation=operation, arguments=values, **extra),
            )
        )

    if name == "list_targets":
        return await invoke("list", args)
    if name == "remote_fetch":
        return await references(tools, await invoke("fetch", args))
    if name == "remote_run":
        exported = await tools.workspace.invoke(
            "export",
            dict(baseline=tools.workspace.metadata()["baseline"]),
            min(timeout, 120),
        )
        if exported.get("error"):
            raise ValueError("runner_export_failed")
        folder = None
        if execution.session.get("repository_id"):
            status, listing = await execution.broker.public("GET", "repositories")
            if status != 200:
                raise ProjectError(status, "dependency_unavailable")
            repo = next(
                (
                    r
                    for r in listing["items"]
                    if r["id"] == execution.session["repository_id"]
                ),
                None,
            )
            if repo is None:
                raise ProjectError(404, "not_found")
            folder = repo["folder_id"]
        content = snapshot(
            base64.b64decode(exported["archive"], validate=True),
            folder,
            execution.session["effective_limits"]["expanded_bytes"],
        )
        result = await invoke(
            "prepare", args, snapshot_content=base64.b64encode(content).decode()
        )
        if result["state"] == "denied":
            return dict(error="runner_policy_denied", **bounded(result))
        status_args = dict(
            command_id=result["command_id"], wait_s=args["wait_s"], cancel=False
        )
        if result.get("approval"):
            await tools.emit(
                "approval",
                tool=name,
                label="Approve remote command and code transfer",
                status="running",
                output=dict(approval=result["approval"]),
            )
            deadline = time.monotonic() + min(120, timeout)
            while time.monotonic() < deadline:
                await execution.check()
                result = await invoke("status", status_args)
                if result["approval"]["decision"] != "pending":
                    break
                await asyncio.sleep(0.25)
            if result["approval"]["decision"] != "allowed":
                return dict(
                    error="approval_" + result["approval"]["decision"], state="not_sent"
                )
        result = await invoke("execute", status_args)
        args = status_args
    result = await invoke("status", args)
    deadline = time.monotonic() + min(args["wait_s"], max(0, timeout - 1))
    prior = None
    while result["state"] in {"running", "unknown"} and time.monotonic() < deadline:
        await execution.check()
        if result.get("output") and result["output"] != prior:
            prior = result["output"]
            await tools.emit(
                "tool",
                tool=name,
                label="Remote command progress",
                status="running",
                output=dict(text=prior),
            )
        await asyncio.sleep(0.5)
        result = await invoke("status", args)
    return await references(tools, result)
