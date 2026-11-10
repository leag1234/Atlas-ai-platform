"""Save the new files a Work turn created into the project (option A, step A1).

At the end of a project Work turn, the files the turn created in the workspace
become project files without approval (docs/17-project-mode.md 4.5: new
deliverables within the requested task need no approval). Modified existing
project files are step A2: each one is saved as a new version only after the
owner approves the overwrite. Behind ATLAS_WORK_SAVE=1, default off.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import tarfile
import time
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


async def project_file_paths(execution: Any) -> dict[str, dict[str, Any]]:
    """Project files keyed by workspace path (<folder_id>/<relative_path>)."""
    paths: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    for _ in range(20):
        status, page = await execution.broker.public(
            "GET", "files" + ("?cursor=" + cursor if cursor else "")
        )
        if status != 200:
            break
        for file in page.get("items", []):
            if file.get("folder_id") and file.get("relative_path"):
                key = str(
                    PurePosixPath(str(file["folder_id"])) / str(file["relative_path"])
                )
                paths[key] = dict(
                    file_id=file["id"], relative_path=file["relative_path"]
                )
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return paths


async def deliverables_folder(execution: Any) -> str | None:
    status, project = await execution.broker.public("GET", "")
    if status != 200 or not project.get("deliverables_folder_id"):
        return None
    return str(project["deliverables_folder_id"])


def root_folder(folders: list[dict[str, Any]]) -> str | None:
    """The project's top folder: where files written outside any folder go."""
    for folder in folders:
        if not folder.get("parent_id"):
            return str(folder["id"])
    return str(folders[0]["id"]) if folders else None


def place(
    path: str, folder_ids: set[str], default: str | None
) -> tuple[str, str] | None:
    """(folder_id, relative path) of a workspace path.

    Base files are imported under their folder id; an engine or a command may
    also write at the workspace root (for example /workspace/stats.py). Those
    files belong to the project's top folder.
    """
    parts = path.split("/", 1)
    if len(parts) == 2 and parts[0] in folder_ids:
        return parts[0], parts[1]
    if default is None:
        return None
    return default, path


async def save_new_files(
    execution: Any, workspace: Any, tools: Any, base: Any
) -> dict[str, Any]:
    """Publish the turn's new workspace files as project files, strategy created."""
    report, _ = await _export_and_save_new_files(execution, workspace, tools, base)
    return report


async def _export_and_save_new_files(
    execution: Any, workspace: Any, tools: Any, base: Any
) -> tuple[dict[str, Any], dict[str, bytes]]:
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
        return report, {}
    exported = await workspace.invoke("export", {"baseline": baseline}, 60)
    if exported.get("error"):
        report["error"] = str(exported["error"])
        return report, {}
    new_paths, modified, binary = parse_patch(str(exported.get("patch") or ""))
    report["modified"] = modified
    contents = archive_files(base64.b64decode(exported["archive"], validate=True))
    bases = base_workspace_paths(base)
    status, folders = await execution.broker.public("GET", "folders")
    if status != 200:
        report["error"] = "folders_unavailable"
        return report, {}
    folders_by_id = {str(f["id"]): f for f in folders["items"]}
    source_version_ids = [
        str(v["version_id"]) for v in execution.session["base_file_versions"]
    ]
    publisher = ProjectPublisher(execution)
    existing = await project_file_paths(execution)
    # Work publications always land in the project's deliverables folder (the
    # retrieval side refuses any other); files written at the workspace root go
    # there too, so a later turn finds them at the same project path.
    default = await deliverables_folder(execution) or root_folder(
        list(folders["items"])
    )
    for path in new_paths:
        placed = place(path, set(folders_by_id), default)
        if placed is None:
            report["skipped"].append({"path": path, "reason": "unknown_folder"})
            continue
        relative = placed[1]
        folder_id = default or placed[0]
        if path in bases:
            continue
        if folder_id + "/" + relative in existing:
            # Created by an earlier turn of this session (kept workspace), so
            # already a project file: saving it is an overwrite, with approval.
            report["modified"].append(path)
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
        # The receipt carries no path; the answer link names the saved file.
        receipt = dict(receipt, relative_path=relative)
        tools.deliverables.append(receipt)
        report.setdefault("receipts", []).append(receipt)
        report["saved"].append(
            {
                "path": relative,
                "folder_id": folder_id,
                "file_version_id": receipt["file_version_id"],
            }
        )
    return report, contents


async def save_modified_files(
    execution: Any, tools: Any, report: dict[str, Any], contents: dict[str, bytes]
) -> None:
    """Save modified base files as new versions, each behind an overwrite approval.

    Project mode 4.5: overwriting existing content requires the owner's
    approval, so every modified file gets one scoped approval (same broker
    call and wait loop as request_approval) before its version is posted.
    Never fails the turn: every outcome is recorded in the report.
    """
    from services.orchestrator.code_tools import wait_for_approval
    from services.retrieval.project_identity import ProjectError

    modified = report.get("modified") or []
    if not modified:
        return
    # Session base versions carry ids only, and files saved by earlier turns of
    # the session are not in its base: resolve paths against the project files.
    bases = await project_file_paths(execution)
    status, folders = await execution.broker.public("GET", "folders")
    items = list(folders.get("items", [])) if status == 200 else []
    folder_ids = {str(f["id"]) for f in items}
    default = await deliverables_folder(execution) or root_folder(items)
    deadline = time.monotonic() + 300
    for path in modified:
        placed = place(path, folder_ids, default)
        entry = bases.get(placed[0] + "/" + placed[1]) if placed else None
        if entry is None:
            report.setdefault("not_saved", []).append(
                {"path": path, "reason": "no_base_file"}
            )
            continue
        content = contents.get(path)
        if content is None:
            report.setdefault("not_saved", []).append(
                {"path": path, "reason": "missing"}
            )
            continue
        if not allowed_type(str(entry.get("relative_path"))):
            report.setdefault("not_saved", []).append({"path": path, "reason": "type"})
            continue
        status, file = await execution.broker.public(
            "GET", "files/" + str(entry["file_id"])
        )
        if status != 200:
            report.setdefault("not_saved", []).append(
                {"path": path, "reason": "file_unavailable"}
            )
            continue
        effect = dict(
            file_id=str(file["id"]),
            folder_id=str(file["folder_id"]),
            path=str(file["relative_path"]),
            base_version=str(file["version_id"]),
            revision=int(file["revision"]),
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )
        try:
            approval = await execution.broker.call(
                "approvals",
                dict(
                    execution.fence(),
                    action_kind="overwrite",
                    effects=effect,
                    maximum_bytes=len(content),
                    approver_member_id=execution.session["lease"]["holder_member_id"],
                    destination=dict(kind="server", ref=execution.broker.project),
                ),
            )
        except ProjectError as error:
            report.setdefault("not_saved", []).append(
                {
                    "path": path,
                    "reason": "approval_failed: "
                    + str(error.args[-1] if error.args else error)[:80],
                }
            )
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            report.setdefault("not_saved", []).append(
                {"path": path, "reason": "expired"}
            )
            continue
        decision = await wait_for_approval(
            execution, tools, approval, min(120, remaining)
        )
        # The broker records an accepted approval as "allowed".
        if decision != "allowed":
            report.setdefault("not_saved", []).append(
                {"path": path, "reason": decision}
            )
            continue
        boundary = "atlas-" + uuid4().hex
        body = b"".join(
            [
                b"--" + boundary.encode() + b"\r\n",
                b'Content-Disposition: form-data; name="base_version"\r\n\r\n',
                str(effect["base_version"]).encode() + b"\r\n",
                b"--" + boundary.encode() + b"\r\n",
                b'Content-Disposition: form-data; name="approval_id"\r\n\r\n',
                str(approval["id"]).encode() + b"\r\n",
                b"--" + boundary.encode() + b"\r\n",
                (
                    b'Content-Disposition: form-data; name="file"; filename="'
                    + PurePosixPath(path).name.encode()
                    + b'"\r\nContent-Type: application/octet-stream\r\n\r\n'
                ),
                content + b"\r\n",
                b"--" + boundary.encode() + b"--\r\n",
            ]
        )
        try:
            status, version = await execution.broker.public(
                "POST",
                "files/" + str(file["id"]) + "/versions",
                body=body,
                headers={
                    "Content-Type": "multipart/form-data; boundary=" + boundary,
                    "If-Match": '"' + str(file["revision"]) + '"',
                    # Every write through the project API needs one; the approval
                    # id makes a retry of the same overwrite idempotent.
                    "Idempotency-Key": "work-save-" + str(approval["id"]),
                },
            )
        except ProjectError as error:
            report.setdefault("not_saved", []).append(
                {
                    "path": path,
                    "reason": "post_failed: "
                    + str(error.args[-1] if error.args else error)[:80],
                }
            )
            continue
        if status != 201:
            report.setdefault("not_saved", []).append(
                {
                    "path": path,
                    "reason": "post_failed: "
                    + str(version.get("error", "unknown"))[:80],
                }
            )
            continue
        # A new version is not a publication receipt: it is reported on its own.
        report.setdefault("saved_modified", []).append(
            {
                "path": str(file["relative_path"]),
                "folder_id": str(file["folder_id"]),
                "file_id": str(file["id"]),
                "file_version_id": version["id"],
            }
        )


async def save_turn_files(
    execution: Any, workspace: Any, tools: Any, base: Any, state: str
) -> dict[str, Any]:
    """Save new files only when the setting is on and the turn state is done."""
    if not enabled() or state != "done":
        return {"saved": [], "skipped": [], "not_saved": [], "modified": []}
    report, contents = await _export_and_save_new_files(
        execution, workspace, tools, base
    )
    if report.get("modified") and contents:
        await save_modified_files(execution, tools, report, contents)
    import logging

    # Paths only (never content): a silent skip must be visible in the service log.
    logging.getLogger(__name__).warning(
        "work_save saved=%d modified_saved=%d skipped=%s not_saved=%s",
        len(report.get("saved", [])),
        len(report.get("saved_modified", [])),
        [(e.get("path", "")[-80:], e.get("reason")) for e in report.get("skipped", [])][
            :20
        ],
        [
            (e.get("path", "")[-80:], e.get("reason"))
            for e in report.get("not_saved", [])
            if isinstance(e, dict)
        ][:20],
    )
    return report


SAVED = {
    "fr": "Fichier du projet mis à jour : ",
    "de": "Projektdatei aktualisiert: ",
    "es": "Archivo del proyecto actualizado: ",
    "it": "File del progetto aggiornato: ",
}
NOT_SAVED = {
    "fr": "Non enregistrés (approbation refusée ou expirée) : ",
    "de": "Nicht gespeichert (Freigabe abgelehnt oder abgelaufen): ",
    "es": "No guardados (aprobación denegada o caducada): ",
    "it": "Non salvati (approvazione negata o scaduta): ",
}


def saved_line(lang: str) -> str:
    return "\n\n" + SAVED.get(lang, "Project file updated: ")


def not_saved_line(lang: str) -> str:
    return "\n\n" + NOT_SAVED.get(lang, "Not saved (approval denied or expired): ")
