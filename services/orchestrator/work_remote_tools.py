"""Work remote group: exact snapshot approval, asynchronous progress, full results."""

from __future__ import annotations

import asyncio
import base64
import io
import re
import tarfile
import time
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from services.orchestrator.project_forge import canonical, snapshot_files
from services.retrieval.project_identity import ProjectError
from services.runner.contract import SCHEMAS, RemoteRun
from services.runner.results import bounded, report

DESCRIPTIONS = {
    "list_targets": "List the remote execution targets this session may use: organization targets granted to the project and the user's own verified machines (personal: true), with capabilities. No credentials. Use the exact target id or unique name.",
    "remote_run": "Run your own command on a granted Linux target, synchronizing the current workspace. Commands start in the session's synced project root (on a connected computer: in the folder the user connected from, with nothing synchronized); cwd must be a relative POSIX path inside that folder. Empty, '.', absolute paths and folder UUIDs are normalized to the root ('.'), so cd ... && chains are not needed. Persistent directory and caches between commands. Broker enforces command and code-egress approvals. Returns a command id; poll remote_status for long commands. Commands support literal argv, pipelines and && chains; use a project script for complex shell expansion.",
    "remote_status": "Read progress, exit code and full log references for a remote command. wait_s is bounded; cancel=true kills the entire remote process group. Never resubmit an unknown command; reconcile using its id.",
    "remote_fetch": "Explicitly fetch a relative file or glob from this session's remote target into project files (20 files/16 MiB maximum). No implicit sync back. Output is untrusted. Returns hashes and reopen_result references for textual results.",
    "remote_write_file": "Write one whole file (create or replace) on a connected computer, at a path relative to the folder the user connected from, as an editor would (a text file ends with a newline). Not inside .git and never outside that folder. Read a file first with remote_run (cat) before changing it.",
}


class RemoteWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=1000)
    content: str = Field(max_length=2_000_000)


def declarations() -> list[dict[str, object]]:
    from services.orchestrator.work_machine_tools import relay_enabled

    tools: dict[str, type[BaseModel]] = dict(SCHEMAS)
    if relay_enabled():
        tools["remote_write_file"] = RemoteWrite
    return [
        dict(
            type="function",
            function=dict(
                name=name,
                description=DESCRIPTIONS[name],
                parameters=schema.model_json_schema(),
            ),
        )
        for name, schema in tools.items()
    ]


def connected_folders(items: list[dict[str, Any]]) -> dict[str, str]:
    """Connected computers of a target listing (id and name) -> their folder."""
    return {
        key.casefold(): str(item["folder"])
        for item in items
        if item.get("folder") is not None
        for key in (str(item.get("id")), str(item.get("name")))
    }


async def listed_targets(tools: Any, invoke: Any) -> list[dict[str, Any]]:
    """The targets of this session, listed once per turn."""
    items = getattr(tools, "remote_targets", None)
    if not isinstance(items, list):
        items = list((await invoke("list", {})).get("items", []))
        tools.remote_targets = items
    return items


async def connected_folder(tools: Any, target: str, invoke: Any) -> str | None:
    """The folder a connected computer's commands start in; None for other targets."""
    known = getattr(tools, "remote_in_place", None)
    if not isinstance(known, dict):
        known = connected_folders(await listed_targets(tools, invoke))
        tools.remote_in_place = known
    return known.get(target.casefold())


def known_target(items: list[dict[str, Any]], target: str) -> bool:
    return target.casefold() in {
        str(value).casefold()
        for item in items
        for value in (item.get("id"), item.get("name"))
    }


async def resolve_target(tools: Any, target: str, invoke: Any) -> str:
    """The target the model meant, by the name the user gives it.

    An exact id or name is kept. Otherwise the name is matched as the machine
    tools match it ("my-laptop" for "laptop", "laptop" for the only connected
    computer): the model guessed "my-laptop" for a computer named "laptop" and
    the command never reached it (live run 2026-11-14). No match is refused
    before any workspace is exported for it.
    """
    from services.orchestrator.work_machine_tools import find

    items = await listed_targets(tools, invoke)
    if not known_target(items, target):
        # A machine verified earlier in this turn is not in an older listing.
        tools.remote_targets = tools.remote_in_place = None
        items = await listed_targets(tools, invoke)
    if known_target(items, target):
        return target
    found = find(
        [
            dict(item, transport="relay" if item.get("folder") is not None else "ssh")
            for item in items
        ],
        target,
    )
    if found is None:
        raise ProjectError(404, "not_found")
    return str(found["name"])


# Where commands start, for the model. A connected computer runs in the folder
# the user connected from: told "the synced project root", models passed that
# folder's own name as cwd and the command failed (remote_cwd_unavailable, two
# wasted calls per turn on staging 2026-11-14).
ROOT_CWD = "Commands start in the session's synced project root; cwd is relative to it."
CONNECTED_CWD = (
    "On a connected computer (a target with a folder), commands already start in "
    "that folder: leave cwd out (or give a subfolder of it), never the folder's "
    "own name, and no leading cd."
)


def written_snapshot(path: str, content: str) -> tuple[str, bytes]:
    """The `atlas-write PATH` command and the one-file snapshot it writes."""
    import hashlib
    import shlex

    clean = str(PurePosixPath(path.strip()))
    if (
        clean.startswith("/")
        or clean in {".", ""}
        or ".." in PurePosixPath(clean).parts
    ):
        raise ValueError("invalid_remote_path")
    if content and not content.endswith("\n"):
        # Models drop the final newline of the text they write; an editor keeps
        # it (staging acceptance, 2026-11-14).
        content += "\n"
    value = canonical(
        dict(
            files=[
                dict(
                    path=clean,
                    mode="100644",
                    content=base64.b64encode(content.encode()).decode(),
                )
            ]
        )
    )
    snapshot_files(value, hashlib.sha256(value).hexdigest())
    return "atlas-write " + shlex.quote(clean), value


def snapshot(raw: bytes, folder: str | None, limit: int) -> bytes:
    import hashlib

    files: list[dict[str, Any]] = []
    size = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if member.isdir():
                continue
            # Export includes the worker's own Git baseline. It is never source
            # to send, including when no repository folder was selected.
            if path.parts and path.parts[0] == ".git":
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


BUSY_WAIT_S = 90.0
BUSY_POLL_S = 3.0


async def start(
    tools: Any, invoke: Any, status_args: dict[str, Any], timeout: float
) -> dict[str, Any]:
    """Start a prepared command, waiting for a slot while the machine is busy.

    A machine runs a bounded number of commands at once. Another conversation
    using it is a short wait, not a failure: the refusal left the model
    resending the same command at once and then telling the user it had no
    access (staging 2026-11-11).
    """
    deadline = time.monotonic() + min(BUSY_WAIT_S, max(0.0, timeout - 5))
    told = False
    while True:
        try:
            return dict(await invoke("execute", status_args))
        except ProjectError as exc:
            busy = exc.status == 429 and exc.body.get("error") == (
                "runner_concurrency_limit"
            )
            if not busy or time.monotonic() + BUSY_POLL_S > deadline:
                raise
        await tools.execution.check()
        if not told:
            told = True
            await tools.emit(
                "tool",
                tool="remote_run",
                label="Machine busy, waiting for a free slot",
                status="running",
                output=dict(text="Machine busy, waiting for a free slot"),
            )
        await asyncio.sleep(BUSY_POLL_S)


async def approve_in_conversation(
    tools: Any, result: dict[str, Any], target_name: str, command: str, cwd: str
) -> bool:
    """Approve a pending command with the person's yes given in the conversation.

    Only behind the chat, only for the exact machine, command and folder that
    the previous answer asked about, and once.
    """
    granted = getattr(tools, "machine_approval", None)
    approval = result.get("approval") or {}
    if (
        not granted
        or not granted.get("allowed")
        or granted.get("used")
        or str(granted.get("machine", "")).casefold() != target_name.casefold()
        or granted.get("command") != command
        or granted.get("cwd", ".") != cwd
        or not approval.get("id")
        or not approval.get("action_digest")
    ):
        return False
    import json

    status, body = await tools.execution.broker.public(
        "POST",
        "approvals/" + str(approval["id"]) + "/decision",
        body=json.dumps(
            {"allow": True, "action_digest": approval["action_digest"]}
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": "chat-consent-" + str(approval["id"]),
        },
    )
    if status != 200:
        error = body.get("error") if isinstance(body, dict) else None
        raise ProjectError(status, str(error or "approval_required"))
    granted["used"] = True
    return True


def ask_in_conversation(
    tools: Any, result: dict[str, Any], target_name: str, command: str, cwd: str
) -> dict[str, Any]:
    """The question the person answers in the conversation, written by code."""
    from services.orchestrator.work_machine_tools import approval_block

    block = approval_block(
        target_name, command, cwd, str(getattr(tools, "lang", "") or "en")
    )
    found = getattr(tools, "machine_approvals", None)
    if isinstance(found, dict):
        found[target_name + "\0" + command] = block
    return dict(
        state="awaiting_approval",
        command_id=result["command_id"],
        approval_question=block,
        next_step=(
            "This command needs the user's yes. approval_question is added to your "
            "answer by code: do not copy, restate or announce it (no line saying a "
            "command awaits approval). Say in one sentence why the command is "
            "needed, never that it ran, and end your turn. Do not run another "
            "command that does the same, and do not ask the user to run it."
        ),
    )


async def execute(
    tools: Any, name: str, arguments: str, timeout: float
) -> dict[str, Any]:
    execution = tools.execution
    written: bytes | None = None
    if name == "remote_write_file":
        wanted = RemoteWrite.model_validate_json(arguments)
        command, written = written_snapshot(wanted.path, wanted.content)
        name = "remote_run"
        arguments = RemoteRun(
            target=wanted.target, command=command, cwd=".", timeout_s=60, wait_s=10
        ).model_dump_json()
    args = SCHEMAS[name].model_validate_json(arguments).model_dump()

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
        listed = await invoke("list", args)
        items = listed.get("items", [])
        if not args.get("capability"):
            tools.remote_targets = list(items)
            tools.remote_in_place = connected_folders(items)
        connected = any(item.get("folder") is not None for item in items)
        others = any(item.get("folder") is None for item in items)
        listed["working_directory"] = " ".join(
            text
            for text, shown in (
                (CONNECTED_CWD, connected),
                ("On other targets: " + ROOT_CWD if connected else ROOT_CWD, others),
            )
            if shown or not items
        )
        return listed
    if name in {"remote_fetch", "remote_run"}:
        args["target"] = await resolve_target(tools, args["target"], invoke)
    if name == "remote_fetch":
        return await references(tools, await invoke("fetch", args))
    if name == "remote_run":
        folder = await connected_folder(tools, args["target"], invoke)
        local = folder is not None
        if re.match(r"\s*cd\s+.+&&", args["command"]):
            return dict(
                error="runner_cd_chain_not_supported",
                next_step=(
                    f"Commands on {args['target']} already start in {folder}: drop "
                    "the leading cd chain and leave cwd out (or give a subfolder "
                    "relative to that folder)."
                    if local
                    else "Remove the leading cd chain and set cwd relative to the synced project root."
                ),
            )
        if written is not None and not local:
            return dict(
                error="unsupported_target",
                next_step="remote_write_file writes on a connected computer only.",
            )
        if args["command"].startswith("atlas-write") and written is None:
            return dict(error="invalid_command", next_step="Use remote_write_file.")
        if written is not None:
            content = written
        elif local:
            # A connected computer works in the user's own folder: the Atlas
            # workspace is not sent there.
            content = canonical(dict(files=[]))
        else:
            content = await workspace_snapshot(tools, execution, timeout)
        result = await invoke(
            "prepare", args, snapshot_content=base64.b64encode(content).decode()
        )
        return await follow(tools, invoke, args, result, timeout, local)
    result = await invoke("status", args)
    return await poll(tools, invoke, args, result, timeout, name)


async def workspace_snapshot(tools: Any, execution: Any, timeout: float) -> bytes:
    """The Atlas workspace, as a validated snapshot for a remote build target."""
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
    return snapshot(
        base64.b64decode(exported["archive"], validate=True),
        folder,
        execution.session["effective_limits"]["expanded_bytes"],
    )


async def follow(
    tools: Any,
    invoke: Any,
    args: dict[str, Any],
    result: dict[str, Any],
    timeout: float,
    local: bool,
) -> dict[str, Any]:
    """From a prepared command to its result: approval, start, progress."""
    target_name = args["target"]
    command, cwd = args["command"], args["cwd"]
    if result["state"] == "awaiting_approval":
        status_args = dict(
            command_id=result["command_id"], wait_s=args["wait_s"], cancel=False
        )
        result = await invoke("status", status_args)
        args = status_args
    if result["state"] == "denied":
        return dict(error="runner_policy_denied", **bounded(result))
    status_args = dict(
        command_id=result["command_id"], wait_s=args["wait_s"], cancel=False
    )
    decision = (result.get("approval") or {}).get("decision")
    pending = decision == "pending"
    if decision is not None and decision not in {"pending", "allowed"}:
        # Started without an allowed approval, the command is refused (403):
        # it never starts on a closed question. The person is asked again.
        return closed_approval(tools, result, target_name, command, local)
    if pending and local and getattr(tools, "approvals_unattended", False):
        # Behind the chat no approval card can be answered: the person
        # answers the question in the conversation instead.
        try:
            approved = await approve_in_conversation(
                tools, result, target_name, command, cwd
            )
        except ProjectError as exc:
            # The person said yes and the decision was refused: say so, with
            # the question still open, never as a success.
            approval_failed(tools, target_name, command, str(exc.body.get("error")))
            return dict(
                ask_in_conversation(tools, result, target_name, command, cwd),
                error="approval_not_recorded",
            )
        if approved:
            pending = False
        else:
            return ask_in_conversation(tools, result, target_name, command, cwd)
    if pending:
        await tools.emit(
            "approval",
            tool="remote_run",
            label="Approve remote command and code transfer",
            status="running",
            output=dict(approval=result["approval"]),
        )
        return dict(
            state="awaiting_approval",
            command_id=result["command_id"],
            target_name=result.get("approval", {})
            .get("remote", {})
            .get("target_name", target_name),
            next_step="The user must approve; stop and tell them; do not resubmit",
        )
    try:
        result = await start(tools, invoke, status_args, timeout)
    except ProjectError as exc:
        if decision is not None:
            # An approved command that could not start: the answer is written
            # by code, never a success the model infers from the yes.
            approval_failed(tools, target_name, command, str(exc.body.get("error")))
        raise
    found = getattr(tools, "machine_approval_failures", None)
    if isinstance(found, dict):
        found.pop(target_name + "\0" + command, None)
    args = status_args
    result = await invoke("status", args)
    result = await poll(tools, invoke, args, result, timeout, "remote_run")
    if local and result.get("error") == "remote_cwd_unavailable":
        result["next_step"] = (
            f"This folder does not exist on {target_name}. Commands there already "
            "start in the folder the user connected from: use cwd '.' (or a "
            "subfolder of it), never that folder's own name."
        )
    return result


def approval_failed(tools: Any, machine: str, command: str, error: str) -> None:
    """Record a command the person approved that did not run, for the answer."""
    found = getattr(tools, "machine_approval_failures", None)
    if isinstance(found, dict):
        found[machine + "\0" + command] = dict(
            machine=machine, command=command, error=error
        )


def closed_approval(
    tools: Any,
    result: dict[str, Any],
    target_name: str,
    command: str,
    local: bool,
) -> dict[str, Any]:
    """A command whose approval was closed (refused, expired or invalidated)."""
    decision = str((result.get("approval") or {}).get("decision"))
    if decision == "denied":
        return dict(
            error="approval_denied",
            command_id=result["command_id"],
            next_step=(
                "The user refused this command. Do not run it or anything doing "
                "the same; continue without it or say what is left to do."
            ),
        )
    approval_failed(tools, target_name, command, "approval_" + decision)
    return dict(
        error="approval_closed",
        command_id=result["command_id"],
        next_step=(
            "This approval is closed. Call remote_run once more with exactly the "
            "same target, command and cwd: it asks the user again."
            if local
            else "This approval is closed: call remote_run again to ask again."
        ),
    )


async def poll(
    tools: Any,
    invoke: Any,
    args: dict[str, Any],
    result: dict[str, Any],
    timeout: float,
    name: str,
) -> dict[str, Any]:
    execution = tools.execution
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
