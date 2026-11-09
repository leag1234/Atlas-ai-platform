"""Save the new files a Work turn created into the project (option A, step A1).

At the end of a project Work turn, the files the turn created in the workspace
become project files without approval (docs/17-project-mode.md 4.5: new
deliverables within the requested task need no approval). Modified existing
project files are step A2 (overwrite approval) and are only listed in the
recap. Behind ATLAS_WORK_SAVE=1, default off.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import tarfile
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from packages.text_deliverables import CODE_BASENAMES, CODE_MIMES, TEXT_CHARACTER_LIMIT

MAX_SAVED_FILES = 50
SKIP_PARTS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def enabled() -> bool:
    return os.environ.get("ATLAS_WORK_SAVE") == "1"


def parse_patch(patch: str) -> tuple[list[str], list[str], set[str]]:
    """Split a git patch into (new paths, modified paths, binary new paths)."""
    new: list[str] = []
    modified: list[str] = []
    binary: set[str] = set()
    for chunk in re.split(r"(?m)^(?=diff --git )", patch):
        if not chunk.startswith("diff --git "):
            continue
        header = chunk.split("\n", 1)[0]
        match = re.match(r"diff --git a/(\S+) b/(\S+)", header)
        if not match:
            continue
        target = match.group(2)
        if "GIT binary patch" in chunk:
            binary.add(target)
            continue
        if "new file mode" in chunk:
            new.append(target)
        elif target != "/dev/null":
            modified.append(target)
    return new, modified, binary


def archive_files(raw: bytes) -> dict[str, bytes]:
    """Exact file bytes from the export archive, keyed by workspace path."""
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            if stream is None:
                continue
            files[member.name] = stream.read()
    return files


def allowed_type(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    if name in CODE_BASENAMES:
        return True
    return PurePosixPath(path).suffix.lower() in CODE_MIMES


def skip_reason(path: str, content: bytes) -> str | None:
    """Why a new file is not saved, or None when it is publishable."""
    parts = PurePosixPath(path).parts
    if any(part in SKIP_PARTS for part in parts):
        return "cache"
    if PurePosixPath(path).suffix.lower() in SKIP_SUFFIXES:
        return "cache"
    if not allowed_type(path):
        return "type"
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    if len(text) > TEXT_CHARACTER_LIMIT:
        return "size"
    if not text.strip():
        return "empty"
    return None


def base_workspace_paths(base: Any) -> set[str]:
    """Workspace paths of the session's base files (<folder_id>/<relative_path>)."""
    paths: set[str] = set()
    for entry in base or []:
        if not isinstance(entry, dict):
            continue
        folder_id, relative = entry.get("folder_id"), entry.get("relative_path")
        if folder_id and relative:
            paths.add(str(PurePosixPath(str(folder_id)) / str(relative)))
    return paths


async def save_new_files(
    execution: Any, workspace: Any, tools: Any, base: Any
) -> dict[str, Any]:
    """Publish the turn's new workspace files as project files, strategy created."""
    from services.orchestrator.terminal_client import ProjectPublisher, VerifiedDocument
    from services.retrieval.project_identity import ProjectError

    report: dict[str, Any] = {
        "saved": [],
        "skipped": [],
        "not_saved": [],
        "modified": [],
    }
    baseline = str(workspace.metadata().get("baseline", ""))
    if not baseline:
        return report
    exported = await workspace.invoke("export", {"baseline": baseline}, 60)
    if exported.get("error"):
        report["error"] = str(exported["error"])
        return report
    new_paths, modified, binary = parse_patch(str(exported.get("patch") or ""))
    report["modified"] = modified
    contents = archive_files(base64.b64decode(exported["archive"], validate=True))
    bases = base_workspace_paths(base)
    status, folders = await execution.broker.public("GET", "folders")
    if status != 200:
        report["error"] = "folders_unavailable"
        return report
    folders_by_id = {str(f["id"]): f for f in folders["items"]}
    source_version_ids = [
        str(v["version_id"]) for v in execution.session["base_file_versions"]
    ]
    publisher = ProjectPublisher(execution)
    for path in new_paths:
        parts = path.split("/", 1)
        if len(parts) != 2 or parts[0] not in folders_by_id:
            report["skipped"].append({"path": path, "reason": "unknown_folder"})
            continue
        folder_id, relative = parts
        if path in bases:
            continue
        if path in binary:
            report["skipped"].append({"path": path, "reason": "binary"})
            continue
        content = contents.get(path)
        if content is None:
            report["skipped"].append({"path": path, "reason": "missing"})
            continue
        reason = skip_reason(relative, content)
        if reason is not None:
            report["skipped"].append({"path": path, "reason": reason})
            continue
        if len(report["saved"]) >= MAX_SAVED_FILES:
            report["not_saved"].append(path)
            continue
        document = VerifiedDocument(
            content=content,
            preview=content,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        # Each publication moves the folder revision: read it fresh every time.
        status, listing = await execution.broker.public("GET", "folders")
        folder = (
            next(
                (f for f in listing.get("items", []) if str(f["id"]) == folder_id), None
            )
            if status == 200
            else None
        )
        if folder is None:
            report["skipped"].append({"path": path, "reason": "folder_unavailable"})
            continue
        try:
            receipt = await publisher.publish(
                document,
                relative,
                "created",
                source_version_ids,
                folder["revision"],
                str(uuid4()),
                60,
            )
        except ProjectError as error:
            # One refused file never fails the turn: it is reported, not saved.
            report["skipped"].append(
                {
                    "path": path,
                    "reason": "refused: "
                    + str(error.args[-1] if error.args else error)[:80],
                }
            )
            continue
        tools.deliverables.append(receipt)
        report.setdefault("receipts", []).append(receipt)
        report["saved"].append(
            {
                "path": relative,
                "folder_id": folder_id,
                "file_version_id": receipt["file_version_id"],
            }
        )
    return report


async def save_turn_files(
    execution: Any, workspace: Any, tools: Any, base: Any, state: str
) -> dict[str, Any]:
    """Save new files only when the setting is on and the turn state is done."""
    if not enabled() or state != "done":
        return {"saved": [], "skipped": [], "not_saved": [], "modified": []}
    return await save_new_files(execution, workspace, tools, base)
