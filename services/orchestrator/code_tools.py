"""Code tool declarations, validated arguments, and single-use approval gate."""

import asyncio
import json
import os
import re
import time
import threading
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from services.orchestrator.work_tool_selection import WorkToolSelection
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.orchestrator.code_workspace import Audit, Workspace, redact
from services.orchestrator.loop import Call, Message, Reservation
from services.orchestrator.stream_client import Sink
from decimal import Decimal


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Directory(Arguments):
    path: str = Field(default=".", max_length=1000)


class Read(Arguments):
    path: str = Field(max_length=1000)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1)


class Search(Directory):
    pattern: str = Field(min_length=1, max_length=200)


class Edit(Arguments):
    path: str = Field(max_length=1000)
    old_text: str = Field(min_length=1, max_length=262144)
    new_text: str = Field(max_length=262144)


class Write(Arguments):
    path: str = Field(max_length=1000)
    content: str = Field(max_length=262144)


class Command(Arguments):
    command: str = Field(min_length=1, max_length=15000)
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    memory_mb: int = Field(default=256, ge=32, le=512)


class PlanItem(Arguments):
    text: str = Field(min_length=1, max_length=500)
    status: Literal["pending", "in_progress", "completed"]


class Plan(Arguments):
    items: list[PlanItem] = Field(max_length=40)


class Approval(Arguments):
    action: str = Field(min_length=1, max_length=2000)


SCHEMAS: dict[str, type[Arguments]] = {
    "list_dir": Directory,
    "read_file": Read,
    "search": Search,
    "edit_file": Edit,
    "write_file": Write,
    "run_command": Command,
    "update_plan": Plan,
    "request_approval": Approval,
}
DESCRIPTIONS = {
    "list_dir": "List the already-extracted workspace directory, bounded to 500 entries. File paths may be relative or under /workspace; no unpacking is needed.",
    "read_file": "Read an already-extracted workspace UTF-8 file with 1-based inclusive line ranges; 256 KiB cap. Paths are relative or under /workspace.",
    "search": "Search literal text recursively, returning bounded file:line matches.",
    "edit_file": "Replace old_text exactly once; fails if absent or ambiguous; returns unified diff.",
    "write_file": "Write a UTF-8 workspace file, at most 256 KiB; returns unified diff.",
    "run_command": "Run a shell command in the workspace with no network or credentials. Return exit code and bounded output. Run tests to verify your changes.",
    "update_plan": "Replace the pinned task list with pending, in_progress, or completed items.",
    "request_approval": "Pause until the owner allows or denies the named action, or the approval expires. Approval does not grant network access.",
}


def declarations(*, scoped: bool = False) -> list[dict[str, object]]:
    from services.orchestrator import project_document_tools

    result: list[dict[str, object]] = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": (
                    DESCRIPTIONS[name]
                    + " For delete_file supply file_id, base_version, revision."
                    " For overwrite also supply folder_id, path, sha256, size_bytes."
                    " For delete_memory supply only memory_ids, memory_revision."
                    " Always supply effect_kind and maximum_bytes; omit all irrelevant fields."
                    if scoped and name == "request_approval"
                    else DESCRIPTIONS[name]
                ),
                "parameters": (
                    ModelApproval if scoped and name == "request_approval" else schema
                ).model_json_schema(),
            },
        }
        for name, schema in SCHEMAS.items()
    ]
    if scoped:
        from services.orchestrator.work_forge_tools import declaration

        result.append(declaration())
    return result + (project_document_tools.declarations() if scoped else [])


def test_command(command: str) -> bool:
    """Recognize common test runners at command positions, not echoed claims."""
    return (
        re.search(
            r"(?:^|[;&|]\s*)(?:python(?:3)?(?:\.\d+)?\s+(?:-m\s+(?:pytest|unittest)\b|[^\s]*test[^\s]*\.py\b)"
            r"|(?:pytest|py.test|ctest|jest|vitest)\b|(?:npm|pnpm|yarn|cargo|go|make)\s+test\b)",
            command.strip(),
            re.I,
        )
        is not None
    )


class ApprovalGate:
    def __init__(self) -> None:
        self.pending: dict[str, asyncio.Future[bool]] = {}
        self.lock = threading.Lock()

    def answer(self, key: str, allow: bool) -> bool:
        with self.lock:
            future = self.pending.pop(key, None)
        if future is None or future.done():
            return False

        def deliver() -> None:
            if not future.done():
                future.set_result(allow)

        try:
            future.get_loop().call_soon_threadsafe(deliver)
        except RuntimeError:
            return False
        return True

    async def wait(self, action: str, timeout: float, tools: "CodeTools") -> str:
        key = uuid4().hex
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        with self.lock:
            self.pending[key] = future
        try:
            await tools.emit(
                "approval",
                tool="request_approval",
                label=action,
                status="running",
                output={
                    "approval_id": key,
                    "action": action,
                    "timeout_seconds": timeout,
                },
            )
            try:
                allowed = await asyncio.wait_for(future, timeout)
                decision = "allowed" if allowed else "denied"
            except TimeoutError:
                decision = "timeout"
            await tools.emit(
                "approval",
                tool="request_approval",
                label=action,
                status="completed",
                output={"approval_id": key, "decision": decision},
            )
            return decision
        finally:
            with self.lock:
                self.pending.pop(key, None)
            if not future.done():
                future.cancel()


gate = ApprovalGate()


class CodeTools:
    def __init__(
        self, workspace: Workspace, audit: Audit, sink: Sink | None = None
    ) -> None:
        self.workspace, self.audit, self.sink = workspace, audit, sink
        self.test_evidence: list[dict[str, Any]] = []

    async def emit(self, kind: str, **data: Any) -> None:
        event = self.audit.record(kind, **data)
        if self.sink:
            await self.sink({"code": event})

    def estimate(self, call: Call) -> Reservation:
        return Reservation(0, Decimal(0))

    async def execute(self, call: Call, timeout: float) -> Message:
        started = time.monotonic()
        await self.emit(
            "tool",
            tool=call.name,
            label=call.name,
            status="running",
            arguments=call.arguments,
            output={},
        )
        try:
            schema = SCHEMAS.get(call.name)
            if schema is None:
                raise ValueError("unknown_code_tool")
            args = schema.model_validate(json.loads(call.arguments)).model_dump()
            if call.name == "update_plan":
                state = self.workspace.metadata()
                state["plan"] = args["items"]
                self.workspace.save(state)
                result = args
                await self.emit(
                    "plan",
                    tool=call.name,
                    label="Plan updated",
                    status="completed",
                    output=result,
                )
            elif call.name == "request_approval":
                seconds = min(
                    timeout, float(os.environ.get("ATLAS_CODE_APPROVAL_SECONDS", "120"))
                )
                result = {"decision": await gate.wait(args["action"], seconds, self)}
            else:
                if call.name == "run_command":
                    args["timeout_seconds"] = min(
                        args["timeout_seconds"], max(0.01, timeout - 2)
                    )
                result = await self.workspace.invoke(call.name, args, timeout)
            result = redact(result)
            if (
                call.name == "run_command"
                and result.get("exit_code") == 0
                and not result.get("timed_out")
            ):
                if test_command(args["command"]):
                    self.test_evidence.append(
                        {"command": redact(args["command"]), **result}
                    )
            if "diff" in result:
                await self.emit(
                    "diff",
                    tool=call.name,
                    label=args["path"],
                    status="completed",
                    output={"diff": result["diff"]},
                )
        except TimeoutError:
            result = {
                "error": "code_tool_timeout",
                "elapsed_seconds": time.monotonic() - started,
                "limit_seconds": timeout,
            }
        except (ValueError, OSError, RuntimeError) as exc:
            result = {"error": redact(str(exc))}
        await self.emit(
            "tool",
            tool=call.name,
            label=call.name,
            status="failed"
            if result.get("error")
            or result.get("timed_out")
            or result.get("exit_code", 0) != 0
            else "completed",
            output=result,
        )
        return dict(result)


class OverwriteEffect(Arguments):
    file_id: str
    folder_id: str
    path: str
    base_version: str
    revision: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)


class DeleteEffect(Arguments):
    file_id: str
    base_version: str
    revision: int = Field(ge=0)


class MemoryEffect(Arguments):
    memory_ids: list[str] = Field(max_length=100)
    memory_revision: int = Field(ge=0)


class ScopedApproval(Arguments):
    action_kind: Literal["overwrite", "delete"]
    effects: OverwriteEffect | DeleteEffect | MemoryEffect
    maximum_bytes: int = Field(ge=0)


class ModelApproval(Arguments):
    effect_kind: Literal["overwrite", "delete_file", "delete_memory"]
    maximum_bytes: int = Field(ge=0)
    file_id: str | None = None
    folder_id: str | None = None
    path: str | None = None
    base_version: str | None = None
    revision: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)
    memory_ids: list[str] | None = Field(default=None, max_length=100)
    memory_revision: int | None = Field(default=None, ge=0)

    def canonical(self) -> dict[str, Any]:
        variants: dict[
            str, type[OverwriteEffect] | type[DeleteEffect] | type[MemoryEffect]
        ] = {
            "overwrite": OverwriteEffect,
            "delete_file": DeleteEffect,
            "delete_memory": MemoryEffect,
        }
        schema = variants[self.effect_kind]
        fields = self.model_fields_set - {"effect_kind", "maximum_bytes"}
        if fields != schema.model_fields.keys():
            raise ValueError("Provide exactly the fields required by effect_kind")
        effects = schema.model_validate(self.model_dump(include=fields))
        return ScopedApproval(
            action_kind="overwrite" if self.effect_kind == "overwrite" else "delete",
            effects=effects,
            maximum_bytes=self.maximum_bytes,
        ).model_dump()


def approval_arguments(arguments: str) -> dict[str, Any]:
    # Preserve the strict internal interface while exposing only the flat form
    # to the model. Both routes still cross the same strict broker boundary.
    value = json.loads(arguments)
    if isinstance(value, dict) and "effect_kind" in value:
        return ModelApproval.model_validate_json(arguments).canonical()
    return ScopedApproval.model_validate_json(arguments).model_dump()


class WorkTools(CodeTools):
    """Adapt existing tool events once, before retrieval allocates durable sequences."""

    def __init__(
        self, workspace: Workspace, execution: Any, sink: Sink | None = None
    ) -> None:
        # Scoped Work never writes the legacy unscoped audit files.
        self.workspace, self.execution, self.sink = workspace, execution, sink
        self.test_evidence = []
        self.deliverables: list[dict[str, Any]] = []
        self.selection: WorkToolSelection | None = None
        self.validation_failures: dict[str, str] = {}
        self.nearby: Any = None

    async def emit(self, kind: str, **data: Any) -> None:
        output = redact(data.get("output", {}))
        if kind == "tool" and data.get("status") in {"completed", "failed"}:
            tool = str(data.get("tool", ""))
            if data["status"] == "failed":
                self.validation_failures[tool] = str(output.get("error", "tool_failed"))
            else:
                self.validation_failures.pop(tool, None)
        mapped: dict[str, Any] = {}
        for name in ("items", "approval", "file_version_ids", "steps", "cost_eur"):
            if name in output:
                mapped[name] = output[name]
        for name in ("diff", "text", "detail"):
            if isinstance(output.get(name), str) and output[name].strip():
                mapped[name] = output[name][:16000]
        if kind == "workspace":
            mapped["workspace"] = self.execution.session["environment"]
        if kind == "tool":
            mapped["tool_result"] = json.dumps(output, ensure_ascii=False)[:16000]
        if kind == "limit" and {"measured", "limit", "unit"} <= output.keys():
            mapped["measurement"] = {
                k: output[k] for k in ("measured", "limit", "unit")
            }
            if mapped["measurement"]["unit"] == "steps":
                mapped["measurement"]["unit"] = "calls"
        event = dict(
            org_id=self.execution.broker.org,
            project_id=self.execution.broker.project,
            version=1,
            seq=1,
            session_id=self.execution.session["id"],
            request_id=self.execution.request_id,
            turn_id=None,
            type=kind,
            status=data.get("status", "completed"),
            label=str(redact(data.get("label", kind)))[:500],
            tool=data.get("tool"),
            plan=[],
            approval_id=output.get("approval", {}).get("id"),
            job_id=None,
            text=None,
            error=None,
            output=mapped,
        )
        result = await self.execution.broker.call(
            "events", dict(self.execution.fence(), progress=[event], audit=[])
        )
        for persisted in result["events"]:
            if self.sink:
                await self.sink({"project": persisted})

    async def execute(self, call: Call, timeout: float) -> Message:
        await self.execution.check()
        from services.orchestrator import project_document_tools

        if self.selection is not None and call.name not in self.selection.names:
            return {
                "error": "tool_not_selected",
                "group": self.selection.group,
                "next_step": "Use switch_tool_group to select the required tools",
            }
        if call.name == "switch_tool_group" and self.selection is not None:
            try:
                result = self.selection.switch(call.arguments)
            except ValueError:
                result = {"error": "invalid_tool_group"}
            await self.emit(
                "tool",
                tool=call.name,
                label=call.name,
                status="failed" if result.get("error") else "completed",
                output=result,
            )
            return result
        if call.name in {"nearby_places", "geocode"}:
            if self.nearby is None:
                return {"error": "location_required"}
            result = await self.nearby.execute(call, timeout)
            await self.emit(
                "tool",
                tool=call.name,
                label=call.name,
                status="failed" if result.get("error") else "completed",
                output=result,
            )
            return dict(result)
        if call.name in project_document_tools.SCHEMAS:
            from services.retrieval.project_identity import ProjectError

            await self.emit(
                "tool", tool=call.name, label=call.name, status="running", output={}
            )
            try:
                async with asyncio.timeout(timeout):
                    result = await project_document_tools.execute(
                        self, call.name, call.arguments, timeout
                    )
            except ProjectError as exc:
                result = dict(exc.body)
            except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
                result = {"error": redact(str(exc)) or "document_tool_timeout"}
            await self.emit(
                "tool",
                tool=call.name,
                label=call.name,
                status="failed" if result.get("error") else "completed",
                output=result,
            )
            return dict(trust="untrusted", **result)
        if call.name == "forge_action":
            from services.orchestrator import work_forge_tools
            from services.retrieval.project_identity import ProjectError

            try:
                result = await work_forge_tools.execute(self, call.arguments, timeout)
            except (ValueError, ProjectError) as exc:
                result = dict(
                    error=exc.body["error"]
                    if isinstance(exc, ProjectError)
                    else "invalid_tool_arguments"
                )
            await self.emit(
                "tool",
                tool=call.name,
                label=call.name,
                status="failed" if result.get("error") else "completed",
                output=result,
            )
            return result
        if call.name == "request_approval":
            try:
                args = approval_arguments(call.arguments)
            except ValidationError as exc:
                # Validation is a recoverable tool result, as for CodeTools and
                # document tools. Never approve, coerce, or echo invalid inputs.
                result = {
                    "error": "invalid_tool_arguments",
                    "details": exc.errors(include_input=False, include_url=False),
                }
                await self.emit(
                    "tool",
                    tool=call.name,
                    label=call.name,
                    status="failed",
                    output=result,
                )
                return result
            except ValueError:
                result = {
                    "error": "invalid_tool_arguments",
                    "details": "Provide exactly the fields required by effect_kind",
                }
                await self.emit(
                    "tool",
                    tool=call.name,
                    label=call.name,
                    status="failed",
                    output=result,
                )
                return result
            self.validation_failures.pop(call.name, None)
            approval = await self.execution.broker.call(
                "approvals",
                dict(
                    self.execution.fence(),
                    **args,
                    approver_member_id=self.execution.session["lease"][
                        "holder_member_id"
                    ],
                    destination=dict(kind="server", ref=self.execution.broker.project),
                ),
            )
            await self.emit(
                "approval",
                tool=call.name,
                label="Approval required",
                status="running",
                output=dict(approval=approval),
            )
            # Canonical mutations consume the authoritative decision. A tool
            # response is never a capability or permission to bypass the broker.
            deadline = time.monotonic() + min(timeout, 120)
            decision = "expired"
            while time.monotonic() < deadline:
                await self.execution.check()
                status, session = await self.execution.broker.public(
                    "GET", "work-sessions/" + self.execution.session["id"]
                )
                if status != 200:
                    from services.retrieval.project_identity import ProjectError

                    raise ProjectError(status, str(session.get("error", "not_found")))
                current = next(
                    (
                        a
                        for a in session["pending_approvals"]
                        if a["id"] == approval["id"]
                    ),
                    None,
                )
                if current is None:
                    decision = "denied"
                    break
                if current["decision"] != "pending":
                    decision = current["decision"]
                    break
                await asyncio.sleep(min(0.25, max(0, deadline - time.monotonic())))
            await self.emit(
                "approval",
                tool=call.name,
                label="Approval " + decision,
                status="completed",
                output=dict(approval=approval),
            )
            return dict(
                approval_id=approval["id"],
                action_digest=approval["action_digest"],
                decision=decision,
            )
        if call.name == "run_command":
            args = Command.model_validate_json(call.arguments).model_dump()
            limits = self.execution.session["effective_limits"]
            if limits["command_memory_bytes"] < 32 * 1024 * 1024:
                from services.retrieval.project_identity import ProjectError

                raise ProjectError(
                    403,
                    "policy_denied",
                    "Command memory ceiling is below the worker minimum",
                )
            args["timeout_seconds"] = min(
                args["timeout_seconds"], limits["command_seconds"]
            )
            args["memory_mb"] = min(
                args["memory_mb"], limits["command_memory_bytes"] // (1024 * 1024)
            )
            from dataclasses import replace

            call = replace(call, arguments=json.dumps(args))
        return await super().execute(call, timeout)
