"""Task-C dispatch behind task B's identity, authorization and idempotency gates."""

from __future__ import annotations

from email import policy
import json
from email.parser import BytesParser
from typing import Any
from uuid import UUID

from services.retrieval.project_file_inputs import (
    FilePatch,
    FolderCreate,
    FolderPatch,
    InstructionsPut,
    MemoryCreate,
    MemoryPatch,
    relative_path,
)
from services.retrieval.project_files import PREVIEW_LIMIT, ProjectFiles
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_store import Scope


def multipart(
    body: bytes, content_type: str, version: bool
) -> tuple[dict[str, str], bytes]:
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + content_type.encode("ascii")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + body
    )
    if (
        message.get_content_type() != "multipart/form-data"
        or not message.is_multipart()
        or message.defects
    ):
        raise ProjectError(422, "invalid_request")
    fields: dict[str, str] = {}
    content: bytes | None = None
    seen: set[str] = set()
    allowed = (
        {"file", "base_version", "approval_id"}
        if version
        else {"file", "path", "expected_parent_revision"}
    )
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if (
            not isinstance(name, str)
            or name in seen
            or name not in allowed
            or part.defects
            or part.is_multipart()
        ):
            raise ProjectError(422, "invalid_request")
        seen.add(name)
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes):
            raise ProjectError(422, "invalid_request")
        if name == "file":
            content = data
        else:
            fields[name] = data.decode("utf-8")
    if seen != allowed or content is None:
        raise ProjectError(422, "invalid_request")
    if version:
        for name in ("base_version", "approval_id"):
            fields[name] = str(UUID(fields[name]))
    else:
        relative_path(fields["path"])
        if not fields["expected_parent_revision"].isdigit():
            raise ProjectError(422, "invalid_request")
    return fields, content


def publication_parts(
    body: bytes, content_type: str
) -> tuple[dict[str, Any], bytes, bytes | None, str | None]:
    from services.retrieval.project_work_contract import validate

    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + content_type.encode("ascii")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + body
    )
    if (
        message.get_content_type() != "multipart/form-data"
        or not message.is_multipart()
        or message.defects
    ):
        raise ProjectError(422, "invalid_request")
    parts: dict[str, bytes] = {}
    types: dict[str, str] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if (
            name not in {"metadata", "file", "preview"}
            or name in parts
            or part.defects
            or part.is_multipart()
            or part.get("Content-Transfer-Encoding")
        ):
            raise ProjectError(422, "invalid_request")
        raw = part.get_payload(decode=True)
        if not isinstance(name, str) or not isinstance(raw, bytes):
            raise ProjectError(422, "invalid_request")
        limit = (
            160000
            if name == "metadata"
            else PREVIEW_LIMIT
            if name == "preview"
            else None
        )
        if limit is not None and len(raw) > limit:
            raise ProjectError(
                413,
                "size_limit",
                f"{name} bytes: measured {len(raw)}; limit {limit}",
                measurements=[dict(measured=len(raw), limit=limit, unit="bytes")],
            )
        parts[name], types[name] = raw, part.get_content_type()
    if (
        not {"metadata", "file"} <= parts.keys()
        or types["metadata"] != "application/json"
    ):
        raise ProjectError(422, "invalid_request")
    metadata = validate("ExecutionPublish", json.loads(parts["metadata"]))
    return metadata, parts["file"], parts.get("preview"), types.get("preview")


def preflight(scope: Scope, method: str, tail: list[str], query: dict[str, str]) -> int:
    files = ProjectFiles(scope)
    if method == "POST" and tail == ["files"]:
        if "folder_id" not in query:
            raise ProjectError(422, "invalid_request", "folder_id query is required")
        files.upload_folder(str(UUID(query["folder_id"])))
        return int(scope.policy["quotas"]["upload_bytes"]) + 65536
    if (
        method == "POST"
        and len(tail) == 3
        and tail[0] == "files"
        and tail[2] == "versions"
    ):
        file = files.get("file", tail[1])
        files.upload_folder(file["folder_id"])
        return int(scope.policy["quotas"]["upload_bytes"]) + 65536
    return 160000


def execute_files(
    scope: Scope,
    method: str,
    tail: list[str],
    body: bytes,
    headers: dict[str, str],
    query: dict[str, str],
) -> tuple[int, Any]:
    files = ProjectFiles(scope)
    expected = headers.get("if-match")
    if tail == ["folders"]:
        if method == "GET":
            return 200, dict(items=files.listing("folder"), next_cursor=None)
        if method == "POST":
            data = FolderCreate.model_validate_json(body).model_dump(mode="json")
            return 201, files.folder(data)
    if len(tail) == 2 and tail[0] == "folders" and method == "PATCH":
        data = FolderPatch.model_validate_json(body).model_dump(
            mode="json", exclude_unset=True
        )
        return 200, files.patch_folder(tail[1], data, expected)
    if tail == ["files"]:
        if method == "GET":
            if "folder_id" in query:
                files.get("folder", query["folder_id"])
            return 200, dict(
                items=files.listing("file", query.get("folder_id")), next_cursor=None
            )
        if method == "POST":
            preflight(scope, method, tail, query)
            fields, content = multipart(body, headers.get("content-type", ""), False)
            file = files.create_file(
                query["folder_id"],
                fields["path"],
                content,
                int(fields["expected_parent_revision"]),
            )
            job = files.job("ingestion", [file["version_id"]])
            from services.retrieval.project_documents import ScopedProjectDocuments

            ScopedProjectDocuments(files).ingest(file, content, job)
            return 201, dict(file=file, job_id=job["id"])
    if len(tail) >= 2 and tail[0] == "files":
        identifier = tail[1]
        if len(tail) == 2:
            if method == "GET":
                return 200, files.get("file", identifier)
            if method == "PATCH":
                data = FilePatch.model_validate_json(body).model_dump(
                    mode="json", exclude_unset=True
                )
                return 200, files.move(identifier, data, expected)
            if method == "DELETE":
                return 202, files.delete_file(
                    identifier, expected, query.get("approval_id")
                )
        if tail[2:] == ["versions"]:
            file = files.get("file", identifier)
            if method == "GET":
                return 200, dict(
                    items=files.listing("version", identifier), next_cursor=None
                )
            if method == "POST":
                fields, content = multipart(body, headers.get("content-type", ""), True)
                version = files.replace(
                    identifier,
                    content,
                    fields["base_version"],
                    expected,
                    fields["approval_id"],
                )
                if identifier != scope.project_data["instructions_file_id"]:
                    from services.retrieval.project_documents import (
                        ScopedProjectDocuments,
                    )

                    file = files.get("file", identifier)
                    job = files.job("ingestion", [version["id"]])
                    ScopedProjectDocuments(files).ingest(file, content, job)
                    version = files.get("version", version["id"])
                return 201, version
        if tail[2:] == ["content"] and method == "GET":
            return 200, files.content(
                identifier, query.get("version"), headers.get("range")
            )
    if tail == ["instructions"]:
        if method == "GET":
            return 200, files.instructions()
        if method == "PUT":
            instructions = InstructionsPut.model_validate_json(body)
            files.replace(
                scope.project_data["instructions_file_id"],
                instructions.text.encode(),
                str(instructions.base_version),
                expected,
                str(instructions.approval_id),
            )
            return 200, files.instructions()
    if tail and tail[0] == "memory":
        if tail == ["memory"]:
            if method == "GET":
                return 200, dict(items=files.listing("memory"), next_cursor=None)
            if method == "POST":
                return 201, files.memory(
                    MemoryCreate.model_validate_json(body).model_dump(mode="json")
                )
        if len(tail) == 2 and method == "PATCH":
            data = MemoryPatch.model_validate_json(body).model_dump(
                mode="json", exclude_unset=True
            )
            return 200, files.patch_memory(tail[1], data, expected)
        if method == "DELETE" and len(tail) <= 2:
            return 200, files.delete_memory(
                tail[1] if len(tail) == 2 else None, expected, query.get("approval_id")
            )
    if tail == ["deliverables"] and method == "GET":
        return 200, dict(items=files.listing("deliverable"), next_cursor=None)
    if (
        len(tail) == 3
        and tail[0] == "deliverables"
        and tail[2] in {"download", "preview"}
        and method == "GET"
    ):
        if tail[2] == "preview":
            return 200, files.preview(tail[1])
        deliverable = files.deliverable(tail[1])
        version = files.get("version", deliverable["file_version_id"])
        return 200, files.content(
            version["file_id"],
            version["id"],
            headers.get("range"),
            event_kind="deliverable_downloaded",
        )
    if tail == ["jobs"] and method == "GET":
        return 200, dict(items=files.listing("job"), next_cursor=None)
    if len(tail) == 2 and tail[0] == "sources" and method == "GET":
        from services.retrieval.project_documents import ScopedProjectDocuments

        return 200, ScopedProjectDocuments(files).resolve(tail[1])
    raise ProjectError(409, "feature_not_available")


def authorize_replay(scope: Scope, method: str, tail: list[str], result: Any) -> None:
    """A stored success response must not resurrect deleted/restricted content."""
    if scope.project is None or method == "DELETE" or not isinstance(result, dict):
        return
    files = ProjectFiles(scope)
    if tail == ["files"] and "file" in result:
        files.get("file", result["file"]["id"])
    elif tail and tail[0] == "files" and "file_id" in result:
        files.version(result["file_id"], result["id"])
    elif tail and tail[0] == "files" and "id" in result:
        files.get("file", result["id"])
    elif tail and tail[0] == "memory" and "id" in result:
        memory = files.get("memory", result["id"])
        files.check_sources(memory["source_file_version_ids"])
