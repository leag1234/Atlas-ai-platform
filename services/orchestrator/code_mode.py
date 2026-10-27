"""Owner-only Code mode through the existing gateway and agent loop."""

import asyncio
import base64
import json
from decimal import Decimal
import os
from pathlib import Path
import re
import time
import threading
from typing import Any
from collections.abc import Awaitable, Callable

import yaml

from packages.ports import atlas_ports
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.code_tools import CodeTools, WorkTools, declarations
from services.orchestrator.code_verification import annotate, explicit_tests
from services.orchestrator.code_workspace import Audit, Workspace, enabled, redact
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import (
    CodeResult,
    Limits,
    Message,
    Model,
    Query,
    Turn,
    ResultReferences,
    run,
)
from services.orchestrator.model import GatewayError, GatewayModel
from services.orchestrator.stream_client import sink_context
from services.orchestrator.terminal_client import TerminalClient


def code_profile() -> str:
    routing = yaml.safe_load(Path("services/model-gateway/routing.yaml").read_text())
    return next(
        str(name) for name, role in routing["public_profiles"].items() if role == "code"
    )


def settings() -> Limits:
    return Limits(
        profile=code_profile(),
        tokens=None,
        code_mode=True,
        tool_calls=int(os.environ.get("ATLAS_CODE_MAX_STEPS", "40")),
        wall_clock=float(os.environ.get("ATLAS_CODE_WALL_SECONDS", "1200")),
        cost=Decimal(os.environ.get("ATLAS_CODE_COST_EUR", "3")),
        model_calls=int(os.environ.get("ATLAS_CODE_MAX_MODEL_CALLS", "41")),
    )


class CodeModel(GatewayModel):
    """Keep gateway per-call ceilings, while the loop owns the larger turn budget."""

    work_tools: Callable[[], list[dict[str, object]]] | None = None
    work_cost: Decimal | None = None
    work_per_call: Decimal = Decimal("0.10")
    work_unknown: bool = False
    work_check: Callable[[], Awaitable[None]] | None = None

    @property
    def allowance(self) -> Decimal:
        return min(
            self.work_per_call,
            max(
                Decimal(0),
                (self.work_cost if self.work_cost is not None else settings().cost)
                - self.spent,
            ),
        )

    def available_tools(self, messages: list[Message]) -> list[dict[str, object]]:
        return self.work_tools() if self.work_tools is not None else self.tools

    async def complete(self, messages: list[Message], timeout: float) -> Turn:
        if self.work_check is not None:
            await self.work_check()
        reserved = self.allowance
        before = self.spent
        try:
            return await super().complete(messages, timeout)
        except BaseException:
            # Failed/canceled provider usage is unknown: retain the reservation.
            self.spent = max(self.spent, before + reserved)
            self.work_unknown = True
            raise


def configure_work_model(model: CodeModel, request: ChatRequest) -> None:
    """One request configuration for public Work and its measurement adapter."""
    model.configure_quality(request.model, request.max_tokens)


CLAIM = re.compile(
    r"(?:\btests?\b.{0,45}\b(?:pass(?:ed|es|ing)?|successful|green|passano|passati|r[eé]ussi\w*|passent|vert\w*|bestanden|erfolgreich|gr[uü]n\w*|superat\w*|riuscit\w*)\b"
    r"|\b(?:pruebas?|tests?)\b.{0,45}\b(?:pasan|pasaron|superad\w*|exitos\w*|correctamente)\b"
    r"|\b(?:tous|all|alle|tutti|todas)\b.{0,20}\btests?\b.{0,25}\b(?:ok|passed|pass|bestanden|superati)\b)",
    re.I,
)
CORRECTION = {
    "en": "Test success is unverified: no successful test command was recorded in this turn.",
    "fr": "La réussite des tests n'est pas vérifiée : aucune commande de test réussie n'a été enregistrée pendant ce tour.",
    "de": "Der Testerfolg ist nicht bestätigt: In diesem Durchgang wurde kein erfolgreicher Testbefehl aufgezeichnet.",
    "es": "El éxito de las pruebas no está verificado: no se registró ningún comando de prueba exitoso en este turno.",
    "it": "Il successo dei test non è verificato: in questo turno non è stato registrato alcun comando di test riuscito.",
}


def claims_success(text: str) -> bool:
    for match in CLAIM.finditer(text):
        context = text[max(0, match.start() - 12) : match.end()]
        if not re.search(
            r"\b(?:not|never|no|pas|aucun\w*|nicht|kein\w*|non|nessun\w*)\b",
            context,
            re.I,
        ):
            return True
    return False


def honest(text: str, evidence: list[dict[str, Any]], lang: str) -> tuple[str, bool]:
    if evidence or not claims_success(text):
        return text, False
    # Replace the whole claiming sentence; never leave a second contradictory claim.
    sentences = re.split(r"(?<=[.!?])\s+|\n", text)
    corrected = [
        CORRECTION.get(lang, CORRECTION["en"]) if claims_success(sentence) else sentence
        for sentence in sentences
    ]
    return "\n".join(corrected), True


async def execute_turn(
    request: ChatRequest,
    model: Model,
    tools: CodeTools,
    limits: Limits,
    workspace_context: dict[str, Any] | None = None,
    references: ResultReferences | None = None,
) -> CodeResult:
    history: list[Message] = [
        {"role": m.role, "content": redact(m.text)} for m in request.messages[:-1]
    ]
    from packages import context_usage

    registered = context_usage.parts.get() or {}
    for original, message in zip(request.messages[:-1], history):
        if original.text in registered:
            context_usage.register(
                str(message["content"]), redact(registered[original.text])
            )
    if workspace_context is not None:
        history.append(
            {
                "role": "user",
                "content": json.dumps(redact({"code_workspace": workspace_context})),
            }
        )
    if workspace_context is not None:
        from packages.context_usage import register

        register(str(history[-1]["content"]), {"session_state": workspace_context})
    if isinstance(tools, WorkTools):
        tools.verification.explicit = explicit_tests(request.user_text)
    result = await run(
        Query(question=redact(request.user_text), lang=request.lang),
        model,
        tools,
        Path("prompts/chat.txt").read_text()
        + Path("prompts/chat-agent.txt").read_text(),
        limits,
        history=history,
        references=references,
    )
    assert isinstance(result, CodeResult)
    if isinstance(tools, WorkTools):
        result.verification = tools.verification.status()
        result.text, replaced = annotate(redact(result.text), result.verification)
    else:
        result.text, replaced = honest(
            redact(result.text), tools.test_evidence, request.lang
        )
    if replaced:
        await tools.emit(
            "honesty",
            label="Test claim corrected",
            status="completed",
            output={
                "text": result.text,
                "verification": result.verification.model_dump(),
            },
        )
    if result.limit_stop:
        await tools.emit(
            "limit",
            label="Turn stopped",
            status="completed",
            output={**result.limit_stop, "detail": result.text},
        )
    return result


active: set[str] = set()
active_lock = threading.Lock()


async def process_code(request: ChatRequest, item: Interaction) -> None:
    if not enabled() or request.code_session_id is None:
        raise ValueError("code_mode_disabled")
    session = request.code_session_id
    item.task_type = "code"
    with active_lock:
        if session in active:
            item.reponse, item.state = (
                "This Code session already has an active turn; wait for it to finish.",
                "done",
            )
            return
        active.add(session)
    audit = Audit(session, item.request_id)
    workspace = Workspace(session)
    tools = CodeTools(workspace, audit, sink_context.get())
    limits = settings()
    started = time.monotonic()
    model: GatewayModel | None = None
    try:
        async with asyncio.timeout(limits.wall_clock):
            await workspace.prepare()
            metadata = workspace.metadata()
            if request.documents:
                if metadata.get("baseline") or len(request.documents) != 1:
                    raise ValueError(
                        "Import requires exactly one archive into an empty session"
                    )
                imported = await workspace.invoke(
                    "import",
                    {"archive": request.documents[0].file_data.split(",")[-1]},
                    min(120, limits.wall_clock),
                )
                if imported.get("error"):
                    raise ValueError(str(imported["error"]))
                metadata = {"baseline": imported["baseline"], "plan": []}
                workspace.save(metadata)
                await tools.emit(
                    "import",
                    label="Archive imported",
                    status="completed",
                    output=imported,
                )
            if metadata.get("plan"):
                await tools.emit(
                    "plan",
                    label="Current plan",
                    status="completed",
                    output={"items": metadata["plan"]},
                )
            if not metadata.get("baseline"):
                raise ValueError(
                    "Upload one zip or tar archive to initialize this Code session"
                )
            listing = await workspace.invoke("list_dir", {"path": "."}, 10)
            workspace_context = {
                "root": "/workspace",
                "archive_state": "already_extracted",
                "paths": "relative or absolute under /workspace",
                "listing": listing,
            }
            await tools.emit(
                "workspace",
                label="Current workspace",
                status="completed",
                output=workspace_context,
            )
            model = await CodeModel.connect(
                os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway")),
                local_enabled=False,
            )
            model.configure_quality(code_profile(), request.max_tokens)
            model.tools = declarations()
            # Hold all model prose until the honesty guard has evidence.
            model.sink = None
            remaining = max(0, limits.wall_clock - (time.monotonic() - started))
            from dataclasses import replace

            result = await execute_turn(
                request,
                model,
                tools,
                replace(limits, wall_clock=remaining),
                workspace_context,
            )
            item.reponse = result.text
            item.tool_urls.extend(getattr(result, "tool_urls", set()))
            item.verification = result.verification.model_dump()
            item.cout_eur = float(result.cost)
            item.tokens = {"in": model.input_tokens, "out": model.output_tokens}
            if result.reason:
                item.erreurs.append(result.reason)
            exported = await workspace.invoke(
                "export",
                {"baseline": metadata["baseline"]},
                min(60, max(0.1, limits.wall_clock - (time.monotonic() - started))),
            )
            if exported.get("error"):
                item.reponse += "\nExport failed: " + str(redact(exported["error"]))
            else:
                terminal = TerminalClient(item.request_id)
                await terminal.initialize()
                for filename, content in (
                    ("workspace.tar.gz", base64.b64decode(exported["archive"])),
                    ("changes.patch", exported["patch"].encode()),
                ):
                    path = await terminal.upload_generated(
                        filename, content or b"\n", 30, max_bytes=220 * 1024 * 1024
                    )
                    url = "/api/v1/terminals/atlas-files/files/serve/" + path.lstrip(
                        "/"
                    )
                    item.files.append(url)
                    item.reponse += f"\n[{filename}]({url})"
                await tools.emit(
                    "export",
                    label="Workspace exported",
                    status="completed",
                    output={"files": item.files},
                )
            await tools.emit(
                "recap",
                label="Code turn complete",
                status="completed",
                output={
                    "text": item.reponse,
                    "steps": result.tool_calls,
                    "model_calls": result.model_calls,
                    "cost_eur": item.cout_eur,
                    "test_evidence": tools.test_evidence,
                    "question": "What would you like to do next?",
                    "actions": ["Review the changes", "Run the tests"],
                },
            )
    except TimeoutError:
        elapsed = time.monotonic() - started
        item.reponse = f"Code turn stopped: elapsed {elapsed:.3f} seconds; limit {limits.wall_clock:.3f} seconds."
        await tools.emit(
            "limit",
            label="Wall time limit",
            status="completed",
            output={
                "kind": "wall_clock",
                "measured": elapsed,
                "limit": limits.wall_clock,
                "unit": "seconds",
            },
        )
    except GatewayError as exc:
        item.reponse = "Code model request stopped: " + str(
            redact(exc.detail or exc.code)
        )
        await tools.emit(
            "error",
            label="Code model stopped",
            status="failed",
            output={"code": exc.code, "message": item.reponse},
        )
    except (ValueError, RuntimeError, OSError) as exc:
        item.reponse = "Code operation failed: " + str(redact(str(exc)))
        await tools.emit(
            "error",
            label="Code operation failed",
            status="failed",
            output={"message": item.reponse},
        )
    except asyncio.CancelledError:
        audit.record("cancelled", elapsed_seconds=time.monotonic() - started)
        raise
    finally:
        if model is not None:
            item.cout_eur = max(item.cout_eur, float(model.spent))
            item.provider_model = model.provider_model
            item.reasoning_retried = model.reasoning_retried
        with active_lock:
            active.discard(session)
    # Limit stops are successful protocol replies with explicit measured refusal.
    item.state = "done"


class WorkExecution:
    """Per-turn client state; the retrieval lease is the only execution lock."""

    def __init__(self, broker: Any, session: dict[str, Any], request_id: str) -> None:
        self.broker, self.session, self.request_id = broker, session, request_id

    def fence(self) -> dict[str, Any]:
        return dict(
            session_id=self.session["id"],
            request_id=self.request_id,
            generation=self.session["lease"]["generation"],
        )

    async def turn(self, operation: str, **changes: Any) -> dict[str, Any]:
        data = dict(
            self.fence(),
            operation=operation,
            usage=None,
            terminal_state=None,
            cleanup_confirmed=False,
        )
        data.update(changes)
        result = await self.broker.call("turn", data)
        return dict(result)

    async def check(self) -> None:
        from services.retrieval.project_identity import ProjectError

        result = await self.turn("check")
        if result["cleanup_required"]:
            raise ProjectError(409, "session_busy", "Work cancellation requested")

    def workspace(self) -> Workspace:
        return Workspace(
            self.session["id"],
            org=self.broker.org,
            project=self.broker.project,
            workspace=self.session["workspace_id"],
            generation=self.session["lease"]["generation"],
            authorize=self.check,
            limits=self.session["effective_limits"],
        )

    async def snapshot(self, workspace: Workspace) -> None:
        import hashlib
        import io
        import tarfile
        from pathlib import PurePosixPath
        from services.retrieval.project_identity import ProjectError

        bases = self.session["base_file_versions"]
        buffer = io.BytesIO()
        remaining = self.session["effective_limits"]["expanded_bytes"]
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for base in bases:
                await self.check()
                status, file = await self.broker.public(
                    "GET", "files/" + base["file_id"]
                )
                if status != 200 or file.get("version_id") != base["version_id"]:
                    raise ProjectError(409, "revision_conflict")
                content = await self.broker.content(
                    base["file_id"], base["version_id"], remaining
                )
                if (
                    len(content) != file["size_bytes"]
                    or hashlib.sha256(content).hexdigest() != file["sha256"]
                ):
                    raise ProjectError(503, "dependency_unavailable")
                path = PurePosixPath(file["relative_path"])
                if path.is_absolute() or ".." in path.parts:
                    raise ProjectError(422, "invalid_request")
                remaining -= len(content)
                entry = tarfile.TarInfo(str(file["folder_id"]) + "/" + str(path))
                entry.size = len(content)
                entry.mode = 0o600
                archive.addfile(entry, io.BytesIO(content))
        imported = await workspace.invoke(
            "import", dict(archive=base64.b64encode(buffer.getvalue()).decode()), 120
        )
        if imported.get("error"):
            raise ValueError(str(imported["error"]))
        workspace.save(dict(baseline=imported["baseline"], plan=[]))


async def cleanup_work(
    broker: Any, session_id: str, expected_request: str | None = None
) -> None:
    """Explicit cancel/restart recovery: fence, remove workers/scratch, then release."""
    from uuid import uuid4
    from services.retrieval.project_identity import ProjectError

    status, session = await broker.public("GET", "work-sessions/" + session_id)
    if status != 200:
        raise ProjectError(status, str(session.get("error", "not_found")))
    if session["lease"] is None:
        return
    if (
        expected_request is not None
        and session["lease"]["request_id"] != expected_request
    ):
        return
    request_id = str(uuid4())
    result = await broker.call(
        "turn",
        dict(
            session_id=session_id,
            request_id=request_id,
            generation=session["lease"]["generation"],
            operation="recover",
            usage=None,
            terminal_state=None,
            cleanup_confirmed=False,
        ),
    )
    execution = WorkExecution(broker, result["session"], request_id)
    await execution.workspace().cleanup()
    await execution.turn("release", terminal_state="cancelled", cleanup_confirmed=True)


async def process_work(
    request: ChatRequest,
    item: Interaction,
    *,
    subject: str,
    org_id: str,
    project_id: str,
    session_id: str,
    collect_workspace: Callable[[Workspace], Awaitable[None]] | None = None,
) -> None:
    """Task-G handoff after verified identity and scoped conversation/context selection.

    No caller headers or database handles enter this interface. The broker refreshes
    current authority for each operation; disconnect cancellation follows the same
    cleanup boundary as an explicit cancel.
    """
    from uuid import UUID
    from dataclasses import replace
    from services.orchestrator.code_api import ExecutionBroker
    from services.orchestrator.code_tools import WorkTools
    from services.retrieval.project_identity import ProjectError

    from services.orchestrator.project_transcript import work_dispatch

    dispatch = work_dispatch.get()
    broker = ExecutionBroker(subject, str(UUID(org_id)), str(UUID(project_id)))
    status, session = await broker.public(
        "GET", "work-sessions/" + str(UUID(session_id))
    )
    if status != 200:
        raise ProjectError(status, str(session.get("error", "not_found")))
    if session["conversation_id"] != request.conversation_id:
        raise ProjectError(404, "not_found")
    limits = settings()
    acquired = await broker.call(
        "acquire",
        dict(
            session_id=session_id,
            request_id=str(UUID(item.request_id.removeprefix("chatcmpl-"))),
            expected_revision=session["revision"],
            storage_bytes=session["effective_limits"]["expanded_bytes"],
        ),
    )
    execution = WorkExecution(
        broker,
        acquired["session"],
        str(UUID(item.request_id.removeprefix("chatcmpl-"))),
    )
    if dispatch is not None:
        dispatch.session_id = session_id
        dispatch.generation = execution.session["lease"]["generation"]
    workspace = execution.workspace()
    tools = WorkTools(workspace, execution, sink_context.get())
    ceiling = execution.session["effective_limits"]
    limits = replace(
        limits,
        tool_calls=min(limits.tool_calls, ceiling["tool_calls"]),
        model_calls=min(limits.model_calls or 0, ceiling["model_calls"]),
        wall_clock=min(limits.wall_clock, ceiling["wall_seconds"]),
        cost=min(limits.cost, Decimal(str(ceiling["cost_eur"]))),
    )
    started = time.monotonic()
    model: CodeModel | None = None
    result: CodeResult | None = None
    nearby_usage: dict[str, Any] | None = None
    terminal = "failed"
    owner = asyncio.current_task()
    assert owner is not None

    async def heartbeat() -> None:
        try:
            while True:
                await asyncio.sleep(5)
                await execution.turn("renew")
        except (ProjectError, OSError, ValueError):
            owner.cancel()

    renewal = asyncio.create_task(heartbeat())
    try:
        async with asyncio.timeout(limits.wall_clock):
            await workspace.prepare()
            await execution.snapshot(workspace)
            from services.orchestrator.location import location_dependent

            if location_dependent(request.user_text) and (
                request.location is None
                or (request.location.position is None and not request.location.place)
            ):
                from services.orchestrator.nearby_chat import process_nearby

                await execution.check()
                count = await process_nearby(
                    request,
                    item,
                    max_calls=min(8, limits.tool_calls),
                    cost_limit=limits.cost,
                    check=execution.check,
                )
                terminal = "completed" if item.state == "done" else "failed"
                nearby_usage = dict(
                    tool_calls=count,
                    model_calls=0,
                    elapsed_seconds=time.monotonic() - started,
                    cost_eur=item.cout_eur,
                    reserved_eur=0,
                    unknown_eur=0,
                    stored_bytes=0,
                    input_tokens=0,
                    output_tokens=0,
                    tokens_estimated=False,
                )
                await tools.emit(
                    "recap",
                    label="Nearby places",
                    status=terminal,
                    output=dict(
                        text=item.reponse,
                        steps=count,
                        cost_eur=item.cout_eur,
                        file_version_ids=[],
                    ),
                )
                return
            await tools.emit(
                "workspace", label="Server workspace", status="completed", output={}
            )
            connected = await CodeModel.connect(
                os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway")),
                local_enabled=False,
            )
            if not isinstance(connected, CodeModel):
                raise RuntimeError("Invalid Work model client")
            model = connected
            configure_work_model(model, request)
            model.dispatch_hook = dispatch.publish if dispatch is not None else None
            model.session_context = (
                dispatch.session_context if dispatch is not None else None
            )
            model.work_check = execution.check
            model.work_cost = limits.cost
            model.work_per_call = Decimal(str(ceiling["per_call_eur"]))
            from services.orchestrator.work_tool_selection import (
                WorkToolSelection,
                select_group,
            )

            selected_files = []
            for version in execution.session["base_file_versions"]:
                status, file = await broker.public("GET", "files/" + version["file_id"])
                if status != 200:
                    raise ProjectError(status, str(file.get("error", "not_found")))
                selected_files.append(file)
            tools.selection = WorkToolSelection(
                select_group(
                    request.user_text,
                    selected_files,
                    bool(execution.session.get("repository_id")),
                )
            )
            if os.environ.get("ATLAS_REMOTE_RUNNERS") == "1":
                targets = await broker.call(
                    "remote", dict(execution.fence(), operation="list", arguments={})
                )
                if targets.get("items"):
                    tools.selection.enable_remote()
            repository = None
            if execution.session.get("repository_id"):
                status, repositories = await broker.public("GET", "repositories")
                if status != 200:
                    raise ProjectError(status, "dependency_unavailable")
                repository = next(
                    (
                        r
                        for r in repositories["items"]
                        if r["id"] == execution.session["repository_id"]
                    ),
                    None,
                )
            if request.location and location_dependent(request.user_text):
                from services.orchestrator.nearby_chat import NearbyTools
                from services.orchestrator.chat_pipeline import ChatTools

                tools.nearby = NearbyTools(
                    request.location,
                    ChatTools(item),
                    language=request.lang,
                    max_calls=min(8, limits.tool_calls),
                    cost_limit=limits.cost,
                    check=execution.check,
                )
            references = None
            if dispatch is not None:
                from services.orchestrator.result_references import (
                    ResultReferences as ScopedReferences,
                )

                references = ScopedReferences(
                    dispatch.broker,
                    str(request.conversation_id),
                    execution.request_id,
                    dispatch.generation,
                )
                tools.remote_references = references
                tools.selection.reopen_enabled = True
                tools.selection.memory_enabled = True
                from services.orchestrator.project_memory_tools import MemoryTools

                tools.memory = MemoryTools(
                    dispatch.broker, dispatch.state, dispatch.generation
                )
            model.work_tools = tools.selection.declarations
            model.sink = None
            result = await execute_turn(
                request,
                model,
                tools,
                limits,
                dict(
                    root="/workspace",
                    repository=repository,
                    listing=await workspace.invoke("list_dir", {"path": "."}, 10),
                    files=[
                        dict(
                            file_id=file["id"],
                            version=file["version_id"],
                            path=file["folder_id"] + "/" + file["relative_path"],
                            mime=file["mime"],
                        )
                        for file in selected_files
                    ],
                    environment=execution.session["environment"],
                    plan=execution.session.get("plan", []),
                ),
                **({"references": references} if references is not None else {}),
            )
            if collect_workspace is not None:
                await collect_workspace(workspace)
            item.reponse = result.text
            item.tool_urls.extend(getattr(result, "tool_urls", set()))
            item.verification = result.verification.model_dump()
            if tools.nearby is not None and tools.nearby.result:
                from services.orchestrator.nearby_chat import render
                from services.orchestrator.location import LEDGER_LABEL

                item.reponse = render(tools.nearby.result, request.lang)
                item.location_used = LEDGER_LABEL
                item.nearby_source = tools.nearby.result["source"]
                item.retrieved_urls.extend(
                    p["source"] for p in tools.nearby.result["places"]
                )
            from services.orchestrator.file_results import project_deliverable_links

            for receipt in tools.deliverables:
                links = project_deliverable_links(receipt, broker.org, broker.project)
                if links not in item.reponse:
                    item.reponse += "\n\n" + links
            item.cout_eur = float(model.spent)
            item.tokens = {"in": model.input_tokens, "out": model.output_tokens}
            reason = result.reason or next(iter(tools.validation_failures.values()), "")
            terminal = (
                "completed" if result.state == "done" and not reason else "failed"
            )
            item.state = "done" if terminal == "completed" else "failed"
            if terminal == "failed":
                reason = reason or result.state
                item.erreurs.append(reason)
                item.reponse += "\n\nWork failed: " + reason
            await tools.emit(
                "recap",
                label="Work turn complete"
                if terminal == "completed"
                else "Work failed: " + reason,
                status=terminal,
                output=dict(
                    text=item.reponse,
                    steps=result.tool_calls,
                    verification=result.verification.model_dump(),
                    cost_eur=item.cout_eur,
                    file_version_ids=[r["file_version_id"] for r in tools.deliverables],
                ),
            )
    except asyncio.CancelledError:
        terminal = "cancelled"
        raise
    finally:
        item.verification = tools.verification.status().model_dump()
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        if model is not None:
            item.cout_eur = max(item.cout_eur, float(model.spent))
        # A newer generation owns cleanup if recovery raced this turn. Never let
        # an old finally block destroy a newer workspace or release its slot.
        cleanup_authorized = True
        try:
            await execution.turn("check")
        except ProjectError as exc:
            if exc.status in {403, 404, 409}:
                cleanup_authorized = False
                try:
                    await cleanup_work(broker, session_id, execution.request_id)
                except ProjectError as recovery_error:
                    if recovery_error.status not in {403, 404, 409}:
                        raise
                    import logging

                    logging.getLogger(__name__).warning(
                        "work_cleanup_requires_authorized_recovery status=%s",
                        recovery_error.status,
                    )
            else:
                raise
        if cleanup_authorized:
            if tools.nearby is not None:
                tools.nearby.position = None
            if request.location:
                request.location.position = None
                request.location.place = None
                request.location = None

            async def finish() -> None:
                await workspace.cleanup()
                usage = nearby_usage
                if result is not None and model is not None and not model.work_unknown:
                    usage = dict(
                        tool_calls=result.tool_calls,
                        model_calls=result.model_calls,
                        elapsed_seconds=time.monotonic() - started,
                        cost_eur=float(model.spent),
                        reserved_eur=0,
                        unknown_eur=0,
                        stored_bytes=0,
                        input_tokens=model.input_tokens,
                        output_tokens=model.output_tokens,
                        tokens_estimated=False,
                        **(
                            {"context": dispatch.context}
                            if dispatch is not None and dispatch.context is not None
                            else {}
                        ),
                    )
                await execution.turn(
                    "release",
                    terminal_state=terminal,
                    cleanup_confirmed=True,
                    usage=usage,
                )

            cleanup_task = asyncio.create_task(finish())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
                raise
