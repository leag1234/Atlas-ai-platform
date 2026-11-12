"""Wire the opencode Work engine into process_work (step 1d, behind a setting).

When ATLAS_WORK_ENGINE=opencode and the turn selected the code group, one Work
turn runs opencode in the session's own sandboxed container instead of Atlas's
loop. This module owns the turn: it starts the engine container, maps engine
events to Atlas Work tool events, enforces the turn's ceilings through the
driver, and returns the terminal bookkeeping values for process_work.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from functools import partial
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from services.orchestrator.work_opencode import OpencodeDriver, engine_selected
from services.orchestrator.work_opencode_container import start, stop

if TYPE_CHECKING:
    from services.orchestrator.chat_schema import ChatRequest
    from services.orchestrator.code_tools import WorkTools
    from services.orchestrator.code_workspace import Workspace
    from services.orchestrator.interactions import Interaction
    from services.orchestrator.loop import Limits

DEFAULT_MODEL = "atlas-code"
# How the engine's agent runs code, for the instruction of a requested run.
RUN_TOOL = "the bash tool"
DEFAULT_PRICES = "0.5,1.5"


@dataclass
class OpencodeTurnResult:
    text: str
    state: str
    reason: str
    cost_eur: float
    input_tokens: int
    output_tokens: int
    tool_calls: int


def engine_turn_selected(selection: Any, repository: bool = False) -> bool:
    """The opencode engine runs only for code turns when the setting is on.

    A session linked to a forge repository keeps Atlas's loop: forge
    publication (branch, commit, pull request) is an Atlas tool the engine
    is not offered yet, and that flow already works on the loop.
    """
    return (
        engine_selected()
        and not repository
        and selection is not None
        and getattr(selection, "group", None) == "code"
    )


def token_prices() -> tuple[Decimal, Decimal]:
    """EUR per million input/output tokens from ATLAS_WORK_ENGINE_PRICES."""
    raw = os.environ.get("ATLAS_WORK_ENGINE_PRICES", DEFAULT_PRICES)
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 2:
        parts = DEFAULT_PRICES.split(",")
    return Decimal(parts[0]), Decimal(parts[1])


# The workspace prefix says nothing to the user; the file name does.
_WORKSPACE = "/workspace/"


def _label(tool: str, detail: Any, args: Any = None) -> str:
    """A short step label: the file for file tools, the command for bash.

    Never the file content: write and edit inputs carry whole files.
    """
    shown = ""
    if isinstance(args, dict):
        path = args.get("filePath") or args.get("path")
        if isinstance(args.get("command"), str):
            shown = args["command"]
        elif isinstance(path, str):
            shown = path.removeprefix(_WORKSPACE)
        elif isinstance(args.get("pattern"), str):
            shown = args["pattern"]
    elif detail and tool not in {"write", "edit"}:
        shown = str(detail)
    text = shown.replace("\n", " ").strip()
    label = tool if not text else tool + ": " + text
    return label if len(label) <= 120 else label[:119] + "…"


CACHE_PARTS = ("__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/")


def file_patches(patch: str) -> list[tuple[str, str]]:
    """Split a git patch into (path, section) pairs, without build caches.

    Work events carry at most 16000 characters of diff, like the loop's
    per-edit diffs; one section per file keeps each change whole, and test
    runs leave compiled caches that are not changes.
    """
    sections: list[tuple[str, str]] = []
    for chunk in re.split(r"(?m)^(?=diff --git )", patch):
        if not chunk.startswith("diff --git "):
            continue
        header = chunk.split("\n", 1)[0]
        match = re.match(r"diff --git a/(\S+) b/", header)
        path = match.group(1) if match else header[11:]
        if any(part in path + "/" for part in CACHE_PARTS) or path.endswith(".pyc"):
            continue
        sections.append((path, chunk))
    return sections


async def emit_changes(tools: WorkTools, workspace: Workspace) -> None:
    """Emit the turn's changes as diffs, one event per file, like the loop.

    The engine edits files with its own tools, so no per-edit diff exists. The
    workspace computes the patch against its turn baseline; each file's
    section reaches the Work stream and the UI as a completed write_file event.
    """
    baseline = str(workspace.metadata().get("baseline", ""))
    if not baseline:
        return
    exported = await workspace.invoke("export", {"baseline": baseline}, 60)
    patch = "" if exported.get("error") else str(exported.get("patch") or "")
    for path, section in file_patches(patch):
        await tools.emit(
            "tool",
            tool="write_file",
            status="completed",
            label="Changes: " + path.split("/", 1)[-1][:100],
            output={"diff": section},
        )


async def run_opencode_turn(
    request: ChatRequest,
    item: Interaction,
    tools: WorkTools,
    workspace: Workspace,
    limits: Limits,
    research: bool,
) -> OpencodeTurnResult:
    """Run one Work turn on the opencode engine and map its outcome."""
    begun = time.monotonic()
    model_id = os.environ.get("ATLAS_WORK_ENGINE_MODEL", DEFAULT_MODEL)
    prices = token_prices()
    engine = await start(workspace, model_id)
    answer = ""
    seen: set[tuple[Any, ...]] = set()
    tool_calls = 0
    # A requested run holds the answer until the shell ran something.
    guard = getattr(tools, "run_guard", None)
    announced = ""

    async def emit(mapped: dict[str, Any]) -> None:
        nonlocal answer, tool_calls, announced
        kind = mapped.get("kind")
        if kind == "text":
            answer = str(mapped.get("text") or answer)
            return
        if kind != "tool":
            return
        tool = str(mapped.get("tool") or "tool")
        status = str(mapped.get("status") or "")
        key = (tool, str(mapped.get("input") or ""))
        if status == "pending":
            # The engine is still writing this call (a whole file can take a
            # minute): the chat shows the step now. Not persisted, no content.
            sink = getattr(tools, "sink", None)
            if sink is not None and announced != tool:
                announced = tool
                await sink({"work_step": {"tool": tool}})
            return
        announced = ""
        if status == "running":
            if key in seen:
                return
            seen.add(key)
            await tools.emit(
                "tool",
                tool=tool,
                status="running",
                label=_label(tool, mapped.get("input"), mapped.get("args")),
                output={"input": mapped.get("input")},
            )
        elif status in {"completed", "error"}:
            terminal_key = (key[0], key[1], "terminal")
            if terminal_key in seen:
                return
            seen.add(terminal_key)
            tool_calls += 1
            if guard is not None:
                guard.record((tool,))
            await tools.emit(
                "tool",
                tool=tool,
                status="completed" if status == "completed" else "failed",
                label=_label(tool, mapped.get("input"), mapped.get("args")),
                output={
                    "input": mapped.get("input"),
                    "output": mapped.get("output"),
                },
            )

    async def approve(_permission: dict[str, Any]) -> bool:
        # Commands run inside the session's own sandboxed container, exactly
        # like Atlas's run_command today.
        return True

    try:
        driver = OpencodeDriver(
            engine.url,
            engine.password,
            emit,
            approve=approve,
            cost_limit=float(limits.cost),
            # 30 s are kept to export the changes and save the files. The
            # engine start is counted, and the limit stays positive: 0 would
            # mean no limit at all.
            time_limit=max(1.0, limits.wall_clock - 30 - (time.monotonic() - begun)),
            research=research,
            model_id=model_id,
            token_prices=prices,
            follow_up=None if guard is None else partial(guard.next_step, RUN_TOOL),
        )
        # opencode's own prompt is English: without this line short French
        # messages were answered in English (owner test 2026-11-10).
        outcome = await driver.run(
            "Answer in the language of the user's message.\n\n" + request.user_text
        )
    finally:
        await stop(engine.name)
    await emit_changes(tools, workspace)

    state, reason = {
        "complete": ("done", ""),
        "aborted_budget": ("failed", "cost_limit"),
        "aborted_time": ("failed", "time_limit"),
        "error": ("failed", "engine_error"),
    }.get(str(outcome.get("outcome")), ("failed", "engine_error"))
    if guard is not None and state == "done":
        answer, unexecuted = guard.answer(answer, getattr(request, "lang", "en"))
        if unexecuted:
            await tools.emit(
                "honesty",
                label="Run not executed",
                status="completed",
                output={"text": answer},
            )
    return OpencodeTurnResult(
        text=answer,
        state=state,
        reason=reason,
        cost_eur=float(outcome.get("cost", 0.0)),
        input_tokens=int(outcome.get("input_tokens", 0)),
        output_tokens=int(outcome.get("output_tokens", 0)),
        tool_calls=tool_calls,
    )
