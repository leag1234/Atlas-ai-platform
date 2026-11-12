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
import json
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
                    file_id=file["id"],
                    relative_path=file["relative_path"],
                    sha256=file.get("sha256"),
                    version_id=file.get("version_id"),
                )
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return paths


async def approve_unattended(execution: Any, approval: dict[str, Any]) -> bool:
    """Accept an overwrite approval on the person's behalf, behind the chat only.

    Owner decision of 2026-11-11: in the hidden chat workspace, where no
    approval card can be answered, overwriting a file is approved
    automatically. The earlier versions are kept, and a link already given
    keeps serving its own version. The decision goes through the same
    endpoint as a click, as the approver, and is audited like one.
    """
    from services.retrieval.project_identity import ProjectError

    try:
        digest = approval.get("action_digest")
        if not digest:
            status, session = await execution.broker.public(
                "GET", "work-sessions/" + execution.session["id"]
            )
            pending = session.get("pending_approvals", []) if status == 200 else []
            digest = next(
                (
                    a.get("action_digest")
                    for a in pending
                    if a.get("id") == approval["id"]
                ),
                None,
            )
        if not digest:
            return False
        status, _ = await execution.broker.public(
            "POST",
            "approvals/" + str(approval["id"]) + "/decision",
            body=json.dumps({"allow": True, "action_digest": digest}).encode(),
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "chat-approve-" + str(approval["id"]),
            },
        )
    except ProjectError:
        return False
    return bool(status == 200)


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


def chat_target(
    relative: str,
    content: bytes | None,
    folder_id: str,
    existing: dict[str, dict[str, Any]],
    answers: list[str],
    project: str,
) -> str | None:
    """In the hidden chat project, the existing file a workspace file stands for.

    The name or one of its numbered copies holding the same bytes (linked as it
    is), else the one this conversation's answers linked last (updated in place,
    the overwrite being approved automatically there). None when the name is
    free or taken only by other conversations: the file is then saved under
    its name or as a numbered copy.
    """
    from services.orchestrator.chat_file_names import (
        family,
        folder_names,
        last_delivered,
    )

    names = family(relative, folder_names(existing, folder_id))
    if not names:
        return None
    if content is not None:
        digest = hashlib.sha256(content).hexdigest()
        for name in names:
            if existing[folder_id + "/" + name].get("sha256") == digest:
                return name
    return last_delivered(names, answers, project)


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
        numbered = bool(getattr(tools, "numbered_copies", False))
        if numbered:
            target = chat_target(
                relative,
                contents.get(path),
                folder_id,
                existing,
                getattr(tools, "earlier_answers", []),
                execution.broker.project,
            )
            if target is not None:
                # The same bytes are already there, or this conversation saved
                # the file: linked as it is, or updated (auto-approved overwrite).
                report["modified"].append(path)
                report.setdefault("targets", {})[path] = folder_id + "/" + target
                continue
        elif folder_id + "/" + relative in existing:
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
            if numbered:
                # A name another conversation took is saved as a numbered copy;
                # a folder moved by a publication meanwhile is retried.
                from services.orchestrator.chat_file_names import (
                    folder_names,
                    folder_revision,
                    publish_copy,
                )

                receipt, relative = await publish_copy(
                    lambda name, revision: publisher.publish(
                        document,
                        name,
                        "created",
                        source_version_ids,
                        revision,
                        str(uuid4()),
                        60,
                    ),
                    relative,
                    folder_names(existing, folder_id),
                    lambda: folder_revision(execution, folder_id),
                )
                existing[folder_id + "/" + relative] = dict(
                    file_id=receipt.get("id"), relative_path=relative
                )
            else:
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
    targets = report.get("targets") or {}
    for path in modified:
        placed = place(path, folder_ids, default)
        key = targets.get(path) or (placed[0] + "/" + placed[1] if placed else None)
        entry = bases.get(key) if key else None
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
        if entry.get("sha256") == hashlib.sha256(content).hexdigest():
            # The project already holds these exact bytes: nothing to overwrite,
            # nothing to approve, but the answer still links the file. The chat's
            # project is shared by its conversations, and a CSV written again
            # with the same name and content came back with no link at all
            # (staging corpus 2026-11-11).
            if entry.get("version_id"):
                report.setdefault("unchanged", []).append(
                    {
                        "path": str(entry.get("relative_path")),
                        "file_id": str(entry["file_id"]),
                        "file_version_id": str(entry["version_id"]),
                    }
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
        if getattr(
            tools, "approvals_unattended", False
        ) and not await approve_unattended(execution, approval):
            # Never hold a chat answer for an approval nobody can give.
            report.setdefault("not_saved", []).append(
                {"path": path, "reason": "unattended"}
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


UNCHANGED = {
    "fr": "Fichier du projet (déjà à jour) : ",
    "de": "Projektdatei (bereits aktuell): ",
    "es": "Archivo del proyecto (ya actualizado): ",
    "it": "File del progetto (già aggiornato): ",
}


def project_file_lines(
    report: dict[str, Any], org: str, project: str, lang: str
) -> str:
    """Links to the project files a turn changed, or wrote again unchanged."""
    text = ""
    for key, labels, default in (
        ("saved_modified", SAVED, "Project file updated: "),
        ("unchanged", UNCHANGED, "Project file (already up to date): "),
    ):
        for entry in report.get(key, []):
            text += (
                "\n\n"
                + labels.get(lang, default)
                + f"[{entry['path']}](/api/atlas/orgs/{org}/projects/{project}"
                + f"/files/{entry['file_id']}/content?version={entry['file_version_id']})"
            )
    return text


def not_saved_line(lang: str) -> str:
    return "\n\n" + NOT_SAVED.get(lang, "Not saved (approval denied or expired): ")
