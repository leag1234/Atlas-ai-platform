"""REQ-PROJ-02/03: scoped immutable originals and project content lifecycle.

All entry points require task B's authorized transaction. Paths use UUIDs only;
publication metadata, quotas, source relations and boundary outbox commit together.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from urllib.parse import quote

from psycopg.types.json import Jsonb

from services.retrieval.project_file_inputs import relative_path
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_ledger import append
from services.retrieval.project_locality import (
    reject_local_only_egress,
    reject_local_only_upload,
)
from services.retrieval.project_store import Scope, entity, public


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def after(start: str, days: float) -> str:
    return (
        (datetime.fromisoformat(start) + timedelta(days=days))
        .isoformat()
        .replace("+00:00", "Z")
    )


def action_digest(scope: Scope, kind: str, effects: dict[str, Any]) -> str:
    """Broker integration: hash all effect-bearing fields, including scope/policy."""
    return hashlib.sha256(
        json.dumps(
            dict(
                org_id=scope.identity.org,
                project_id=str(scope.project_id),
                member_id=scope.identity.member,
                policy_revision=scope.org["revision"],
                action_kind=kind,
                effects=effects,
            ),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


PREVIEW_LIMIT = 1048576
PUBLICATION_CHECKS = {"package_readable", "requested_content", "source_coverage"}
PUBLICATION_MIMES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
}

# This fixed child receives document bytes only, with no service credentials.
# Resource limits bound hostile parser input independently of the HTTP deadline.
REOPEN_PROGRAM = r"""
import io, json, resource, sys, tempfile, zipfile
from pathlib import Path
resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
resource.setrlimit(resource.RLIMIT_AS, (536870912, 536870912))
resource.setrlimit(resource.RLIMIT_FSIZE, (16777216, 16777216))
from packages.attachment_reader import validate_package
from packages.doc_render.verify import verify
raw = sys.stdin.buffer.read(16777217)
if not 0 < len(raw) <= 16777216:
    raise ValueError("artifact_size")
suffix = sys.argv[1]
if suffix != ".pdf":
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        validate_package(archive)
with tempfile.TemporaryDirectory(prefix="atlas-verify-") as directory:
    path = Path(directory) / ("artifact" + suffix)
    path.write_bytes(raw)
    result = verify(path)
text = "\n\n".join(result.text).encode("utf-8")
if len(text) > 1048576:
    raise ValueError("preview_size")
sys.stdout.buffer.write(text)
"""


def reopen_artifact(content: bytes, suffix: str) -> bytes:
    if suffix not in PUBLICATION_MIMES or not content:
        raise ProjectError(422, "invalid_request", "Unsupported or empty artifact")
    try:
        result = subprocess.run(
            [sys.executable, "-c", REOPEN_PROGRAM, suffix],
            input=content,
            capture_output=True,
            timeout=20,
            cwd=Path(__file__).resolve().parents[2],
            env={},
        )
    except subprocess.TimeoutExpired:
        raise ProjectError(
            422, "invalid_request", "Artifact verification timed out"
        ) from None
    if result.returncode != 0 or not result.stdout.strip():
        raise ProjectError(422, "invalid_request", "Artifact verification failed")
    return result.stdout


class ProjectFiles:
    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self.org, self.project = scope.identity.org, scope.project_id
        self.db = scope.db
        self.root = Path(os.environ.get("ATLAS_DATA_ROOT", "BRAIN/project-data"))
        self.publication_context: dict[str, str] = {}

    def new(self, identifier: str | None = None) -> dict[str, Any]:
        return dict(
            entity(self.scope.identity.member, self.org, identifier),
            project_id=self.project,
        )

    def retention(self, folder: dict[str, Any] | None = None) -> dict[str, Any]:
        policy = dict(self.scope.policy["retention"])
        overrides = [public(self.scope.project or {}).get("retention_override")]
        seen: set[str] = set()
        while folder is not None:
            if folder["id"] in seen:
                raise ProjectError(422, "invalid_request")
            seen.add(folder["id"])
            overrides.append(folder["retention_override"])
            folder = (
                self.get("folder", folder["parent_id"]) if folder["parent_id"] else None
            )
        for override in overrides:
            for key, value in (override or {}).items():
                old = policy[key]
                policy[key] = (
                    old or value
                    if key == "legal_hold"
                    else (
                        old
                        if value is None
                        else value
                        if old is None
                        else min(old, value)
                    )
                )
        return policy

    def put(
        self,
        kind: str,
        value: dict[str, Any],
        parent: str | None = None,
        retention: str = "C",
        private: dict[str, Any] | None = None,
        expires: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO atlas_projects.resources(org_id,project_id,id,kind,parent_id,data,private,retention_class,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                self.org,
                self.project,
                value["id"],
                kind,
                parent,
                Jsonb(value),
                Jsonb(private or {}),
                retention,
                expires,
            ),
        )

        if kind == "deliverable":
            self.emit(
                "deliverable_created", [value["file_version_id"]], value["size_bytes"]
            )

    def save(self, value: dict[str, Any], parent: str | None = None) -> None:
        value["revision"] += 1
        value["updated_at"] = timestamp()
        self.db.execute(
            "UPDATE atlas_projects.resources SET data=%s,parent_id=coalesce(%s,parent_id) WHERE org_id=%s AND project_id=%s AND id=%s",
            (Jsonb(value), parent, self.org, self.project, value["id"]),
        )

    def row(self, kind: str, identifier: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND kind=%s AND id=%s",
            (self.org, self.project, kind, str(UUID(identifier))),
        ).fetchone()
        if row is None:
            raise ProjectError(404, "not_found")
        return row

    def get(self, kind: str, identifier: str) -> dict[str, Any]:
        row = self.row(kind, identifier)
        value: dict[str, Any] = row["data"]
        if value.get("deleted_at") or (
            row["expires_at"] and row["expires_at"] <= datetime.now(timezone.utc)
        ):
            raise ProjectError(404, "not_found")
        if kind in {
            "folder",
            "file",
            "version",
            "instructions",
            "memory",
            "deliverable",
            "conversation",
            "turn",
        }:
            folder = None
            if kind == "file":
                folder = self.get("folder", value["folder_id"])
            policy = (
                self.retention(folder)
                if kind != "folder"
                else self.scope.policy["retention"]
            )
            days = policy["content_expiry_days"]
            created = value["created_at"]
            if self.scope.project:
                created = min(created, self.scope.project["data"]["created_at"])
            if days is not None and after(created, days) <= timestamp():
                raise ProjectError(404, "not_found")
        if kind == "memory":
            from packages.project_memory import normalize

            value = normalize(value)
        return value

    def listing(self, kind: str, parent: str | None = None) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND kind=%s AND (%s::uuid IS NULL OR parent_id=%s) ORDER BY data->>'created_at',id",
            (self.org, self.project, kind, parent, parent),
        ).fetchall()
        result = []
        for row in rows:
            try:
                value = self.get(kind, str(row["id"]))
                if kind == "memory":
                    self.check_sources(value["source_file_version_ids"])
                if kind == "version":
                    self.version(value["file_id"], value["id"])
                if kind == "deliverable":
                    self.deliverable(value["id"])
                result.append(value)
            except ProjectError as exc:
                if exc.status not in (403, 404, 409):
                    raise
        return result

    def emit(
        self,
        kind: str,
        refs: list[str],
        source_bytes: int = 0,
        *,
        located_sources: list[dict[str, Any]] | None = None,
        selected_utf8_bytes: int = 0,
    ) -> None:
        """Record the legacy resource outbox and the immutable data ledger together."""
        event = self.new()
        event.update(kind=kind, resource_ids=refs)
        self.put(
            "outbox",
            event,
            retention="A",
            expires=after(event["created_at"], self.retention()["audit_days"]),
        )
        self.scope.event(kind, self.project)
        self.ledger(
            kind,
            refs,
            source_bytes,
            located_sources=located_sources,
            selected_utf8_bytes=selected_utf8_bytes,
        )

    def ledger(
        self,
        kind: str,
        refs: list[str],
        source_bytes: int = 0,
        *,
        located_sources: list[dict[str, Any]] | None = None,
        selected_utf8_bytes: int = 0,
    ) -> None:
        """Append a boundary event without requiring a mutable resource outbox."""
        resource_refs: list[dict[str, str | None]] = []
        seen: set[tuple[str, str]] = set()
        for identifier in refs:
            row = self.db.execute(
                "SELECT kind,data FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND id=%s",
                (self.org, self.project, identifier),
            ).fetchone()
            if row is None:
                continue
            data = row["data"]
            if row["kind"] == "version":
                pair = (data["file_id"], data["id"])
            elif row["kind"] == "file" and data.get("version_id"):
                pair = (data["id"], data["version_id"])
            else:
                continue
            if pair not in seen:
                seen.add(pair)
                resource_refs.append(
                    {"file_id": pair[0], "version_id": pair[1], "locator": None}
                )
        append(
            self.scope,
            kind=kind,
            request_id=self.publication_context.get("request_id", str(uuid4())),
            session_id=self.publication_context.get("session_id"),
            conversation_id=self.publication_context.get("conversation_id"),
            actor={
                "kind": "member",
                "member_id": self.scope.identity.member,
                "service": None,
            },
            resource_refs=located_sources
            if located_sources is not None
            else resource_refs,
            source={"kind": "server", "ref": None},
            destination={"kind": "server", "ref": None},
            byte_counts={
                "source_bytes": source_bytes,
                "selected_utf8_bytes": selected_utf8_bytes,
                "image_bytes": 0,
                "serialized_payload_bytes": 0,
                "response_bytes": 0,
            },
            outcome="succeeded",
        )

    def collision(
        self, kind: str, parent: str | None, name: str, exclude: str | None = None
    ) -> None:
        field = "name" if kind == "folder" else "relative_path"
        row = self.db.execute(
            "SELECT id FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND kind=%s AND parent_id IS NOT DISTINCT FROM %s::uuid AND data->>%s=%s AND data->>'deleted_at' IS NULL AND (%s::uuid IS NULL OR id<>%s)",
            (self.org, self.project, kind, parent, field, name, exclude, exclude),
        ).fetchone()
        if row:
            raise ProjectError(409, "path_conflict")

    def folder(
        self, data: dict[str, Any], identifier: str | None = None
    ) -> dict[str, Any]:
        parent = data.get("parent_id")
        if parent:
            self.get("folder", parent)
        self.collision("folder", parent, data["name"])
        value = self.new(identifier)
        value.update(
            parent_id=parent,
            name=data["name"],
            locality=data["locality"],
            model_use=data["model_use"],
            device_id=data.get("device_id"),
            device_folder_handle=data.get("device_folder_handle"),
            sync_revision=None,
            status="device_unavailable"
            if data["locality"] == "local_only"
            else "available",
            retention_override=None,
        )
        self.put("folder", value, parent)
        self.emit("folder_registered", [value["id"]])
        return value

    def patch_folder(
        self, identifier: str, data: dict[str, Any], expected: str | None
    ) -> dict[str, Any]:
        value = self.get("folder", identifier)
        self.scope.match(value, expected)
        if data.get("locality", value["locality"]) != value["locality"]:
            raise ProjectError(409, "locality_transition_requires_plan")
        parent = data.get("parent_id", value["parent_id"])
        ancestor = parent
        while ancestor:
            if ancestor == identifier:
                raise ProjectError(422, "invalid_request", "Folder cycle")
            ancestor = self.get("folder", ancestor)["parent_id"]
        if identifier == self.scope.project_data["deliverables_folder_id"] and (
            parent is not None or data.get("name", value["name"]) != value["name"]
        ):
            raise ProjectError(
                403, "policy_denied", "The deliverables folder is reserved"
            )
        self.collision("folder", parent, data.get("name", value["name"]), identifier)
        value.update({k: v for k, v in data.items() if k != "approval_id"})
        self.save(value)
        self.db.execute(
            "UPDATE atlas_projects.resources SET parent_id=%s WHERE org_id=%s AND project_id=%s AND id=%s",
            (parent, self.org, self.project, identifier),
        )
        self.emit("folder_registered", [identifier])
        return value

    def upload_folder(self, identifier: str) -> dict[str, Any]:
        value = self.get("folder", identifier)
        reject_local_only_upload(value["locality"])
        return value

    def blob_path(self, file: str, version: str) -> Path:
        relative = (
            Path("orgs")
            / str(UUID(self.org))
            / "projects"
            / str(UUID(self.project))
            / "blobs"
            / str(UUID(file))
            / str(UUID(version))
        )
        root = self.root.resolve()
        path = root / relative
        if not path.resolve().is_relative_to(root):
            raise ProjectError(403, "policy_denied")
        return path

    def write_blob(self, file: str, version: str, content: bytes) -> None:
        path = self.blob_path(file, version)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("xb") as stream:
            self.scope.rollback_files.append(path)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @staticmethod
    def mime(content: bytes) -> str:
        # libmagic examines bytes, not a client filename/Content-Type. No shell.
        result = subprocess.run(
            ["file", "--brief", "--mime-type", "-"],
            input=content,
            capture_output=True,
            check=True,
            timeout=5,
        )
        mime = result.stdout.decode("ascii").strip()
        return "application/octet-stream" if mime == "application/x-empty" else mime

    def create_file(
        self,
        folder_id: str,
        path: str,
        content: bytes,
        parent_revision: int,
        *,
        identifier: str | None = None,
        initial: bool = False,
        origin: str = "upload",
        verified_mime: str | None = None,
    ) -> dict[str, Any]:
        folder = self.upload_folder(folder_id)
        self.scope.match(folder, f'"{parent_revision}"')
        relative_path(path)
        self.collision("file", folder_id, path)
        value = self.new(identifier)
        value.update(
            folder_id=folder_id,
            relative_path=path,
            classification="internal",
            locality=folder["locality"],
        )
        self.put("file", value, folder_id)
        version = self.add_version(
            value, content, origin=origin, initial=initial, verified_mime=verified_mime
        )
        value.update(
            {
                k: version[k]
                for k in (
                    "mime",
                    "size_bytes",
                    "sha256",
                    "origin",
                    "availability",
                    "source_locator",
                    "index_status",
                )
            },
            version_id=version["id"],
        )
        self.db.execute(
            "UPDATE atlas_projects.resources SET data=%s WHERE org_id=%s AND project_id=%s AND id=%s",
            (Jsonb(value), self.org, self.project, value["id"]),
        )
        self.save(folder)
        self.emit("file_uploaded", [value["id"], version["id"]], len(content))
        return value

    def add_version(
        self,
        file: dict[str, Any],
        content: bytes,
        *,
        origin: str = "upload",
        initial: bool = False,
        verified_mime: str | None = None,
    ) -> dict[str, Any]:
        folder = self.upload_folder(file["folder_id"])
        limit = self.scope.policy["quotas"]["upload_bytes"]
        if len(content) > limit and not initial:
            raise ProjectError(
                413,
                "size_limit",
                f"upload: measured {len(content)} bytes; limit {limit} bytes",
                measurements=[dict(measured=len(content), limit=limit, unit="bytes")],
            )
        value = self.new()
        value.update(
            file_id=file["id"],
            parent_version_id=file.get("version_id"),
            mime=verified_mime or self.mime(content),
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            origin=origin,
            availability="available",
            source_locator=None,
            index_status="pending",
        )
        if not initial:
            amounts: dict[str, float] = {
                "stored_bytes": len(content),
                "versioned_bytes": len(content),
                "upload_count": 1,
            }
            self.scope.reserve(value["id"], amounts)
            for resource, amount in amounts.items():
                self.scope.reconcile(value["id"], resource, amount)
        self.write_blob(file["id"], value["id"], content)
        self.put("version", value, file["id"])
        old = value["parent_version_id"]
        days = self.retention(folder)["superseded_versions_days"]
        if old and days is not None:
            self.db.execute(
                "UPDATE atlas_projects.resources SET expires_at=%s WHERE org_id=%s AND project_id=%s AND id=%s",
                (after(timestamp(), days), self.org, self.project, old),
            )
        return value

    def publish_deliverable(
        self,
        metadata: dict[str, Any],
        content: bytes,
        preview: bytes | None,
        preview_mime: str | None,
    ) -> dict[str, Any]:
        from services.retrieval.project_work import Work

        session, _ = Work(self.scope).fence(metadata)
        folder_id = self.scope.project_data["deliverables_folder_id"]
        folder = self.upload_folder(folder_id)
        Work(self.scope).folder(folder_id)
        self.scope.match(folder, f'"{metadata["expected_parent_revision"]}"')
        if metadata["turn_id"] != metadata["request_id"]:
            raise ProjectError(
                422,
                "invalid_request",
                "Publication turn does not match execution request",
            )
        selected = {base["version_id"] for base in session["base_file_versions"]}
        if not set(metadata["source_version_ids"]) <= selected:
            raise ProjectError(404, "not_found")
        self.check_sources(metadata["source_version_ids"])
        limit = self.scope.policy["quotas"]["upload_bytes"]
        if len(content) > limit:
            raise ProjectError(
                413,
                "size_limit",
                f"Artifact bytes: measured {len(content)}; limit {limit}",
                measurements=[dict(measured=len(content), limit=limit, unit="bytes")],
            )
        self.scope.check_quota("stored_bytes", len(content) + len(preview or b""))
        validation = metadata["validation"]
        digest = hashlib.sha256(content).hexdigest()
        if (
            validation["status"] != "passed"
            or validation["verifier_exit"] != 0
            or not PUBLICATION_CHECKS <= set(validation["checks"])
            or validation["artifact_sha256"] != digest
        ):
            raise ProjectError(
                422, "invalid_request", "Missing or mismatched verification"
            )
        suffix = Path(metadata["path"]).suffix.lower()
        verified_preview = reopen_artifact(content, suffix)
        detected = self.mime(content)
        allowed = {PUBLICATION_MIMES[suffix]}
        if suffix != ".pdf":
            # libmagic reports ZIP or generic bytes for sorted Office packages.
            # Successful reopening above identifies the actual OOXML format.
            allowed.update({"application/zip", "application/octet-stream"})
        if detected not in allowed:
            raise ProjectError(
                422, "invalid_request", "Artifact MIME does not match format"
            )
        if preview is not None:
            if len(preview) > PREVIEW_LIMIT:
                raise ProjectError(
                    413,
                    "size_limit",
                    f"Preview bytes: measured {len(preview)}; limit {PREVIEW_LIMIT}",
                    measurements=[
                        dict(measured=len(preview), limit=PREVIEW_LIMIT, unit="bytes")
                    ],
                )
            if preview_mime == "text/plain":
                if preview != verified_preview:
                    raise ProjectError(
                        422, "invalid_request", "Preview does not match artifact"
                    )
            elif preview_mime == "application/pdf":
                if b"".join(reopen_artifact(preview, ".pdf").split()) != b"".join(
                    verified_preview.split()
                ):
                    raise ProjectError(
                        422, "invalid_request", "Preview does not match artifact"
                    )
            else:
                raise ProjectError(422, "invalid_request", "Unsupported preview MIME")
        # Parsing consumes wall time; a lease checked before it may now be expired.
        Work(self.scope).fence(metadata)
        self.publication_context = dict(
            request_id=metadata["request_id"],
            session_id=session["id"],
            conversation_id=session["conversation_id"],
        )
        file = self.create_file(
            folder_id,
            metadata["path"],
            content,
            metadata["expected_parent_revision"],
            origin="work",
            verified_mime=PUBLICATION_MIMES[suffix],
        )
        value = dict(
            self.new(),
            file_version_id=file["version_id"],
            session_id=session["id"],
            turn_id=metadata["turn_id"],
            source_version_ids=metadata["source_version_ids"],
            strategy=metadata["strategy"],
            validation=validation,
            published_at=timestamp(),
            visibility="project",
            mime=file["mime"],
            size_bytes=file["size_bytes"],
            sha256=file["sha256"],
        )
        value["download_route"] = (
            f"/api/atlas/orgs/{self.org}/projects/{self.project}/deliverables/{value['id']}/download"
        )
        private: dict[str, Any] = {}
        if preview is not None:
            derived = dict(
                self.new(),
                source_version_id=file["version_id"],
                processor_version="deliverable-preview-1",
                chunks=[],
                mime=preview_mime,
                size_bytes=len(preview),
                sha256=hashlib.sha256(preview).hexdigest(),
            )
            for resource in ("derived_bytes", "stored_bytes"):
                self.scope.reserve(derived["id"], {resource: len(preview)})
                self.scope.reconcile(derived["id"], resource, len(preview))
            self.put(
                "derived",
                derived,
                file["version_id"],
                retention="E",
                private=dict(content=base64.b64encode(preview).decode()),
            )
            private["preview_id"] = derived["id"]
        self.put("deliverable", value, file["version_id"], private=private)
        for identifier in [value["id"], *([private["preview_id"]] if private else [])]:
            for source in [file["version_id"], *metadata["source_version_ids"]]:
                self.db.execute(
                    "INSERT INTO atlas_projects.resource_sources VALUES(%s,%s,%s,%s)",
                    (self.org, self.project, identifier, source),
                )
        return value

    def deliverable(self, identifier: str) -> dict[str, Any]:
        value = self.get("deliverable", identifier)
        version = self.get("version", value["file_version_id"])
        self.version(version["file_id"], version["id"], model=True)
        from services.retrieval.project_work import Work

        for source in [version["id"], *value["source_version_ids"]]:
            source_version = self.get("version", source)
            source_file = self.get("file", source_version["file_id"])
            Work(self.scope).folder(source_file["folder_id"])
        self.check_sources(value["source_version_ids"])
        return value

    def preview(self, identifier: str) -> tuple[int, bytes, dict[str, str]]:
        value = self.deliverable(identifier)
        private = self.row("deliverable", identifier)["private"]
        if "preview_id" not in private:
            raise ProjectError(
                404, "not_found", "Preview is not available; download the artifact"
            )
        derived = self.get("derived", private["preview_id"])
        if derived["source_version_id"] != value["file_version_id"]:
            raise ProjectError(404, "not_found")
        content = base64.b64decode(
            self.row("derived", derived["id"])["private"]["content"], validate=True
        )
        if (
            len(content) != derived["size_bytes"]
            or hashlib.sha256(content).hexdigest() != derived["sha256"]
        ):
            raise ProjectError(503, "dependency_unavailable")
        return (
            200,
            content,
            {
                "Content-Type": derived["mime"],
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    def version(
        self,
        file_id: str,
        version_id: str | None = None,
        *,
        model: bool = False,
        extraction: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        file = self.get("file", file_id)
        folder = self.get("folder", file["folder_id"])
        if folder["locality"] == "local_only":
            if model:
                reject_local_only_egress(folder["locality"])
            raise ProjectError(409, "local_device_unavailable")
        if model and folder["model_use"] != "allow":
            raise ProjectError(
                403, "policy_denied", "Source does not permit model context"
            )
        version = self.get("version", version_id or file["version_id"])
        if version["file_id"] != file_id:
            raise ProjectError(404, "not_found")
        retained_extract = (
            extraction
            and model
            and version["origin"] == "legacy_extracted"
            and version["availability"] == "not_retained"
        )
        if version["availability"] != "available" and not retained_extract:
            raise ProjectError(404, "not_found", "Original bytes were not retained")
        if version["origin"] == "work":
            # Canonical file-content routes must not bypass derived-source policy.
            from services.retrieval.project_work import Work

            Work(self.scope).folder(file["folder_id"])
            rows = self.db.execute(
                "SELECT data FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND kind='deliverable' AND parent_id=%s",
                (self.org, self.project, version["id"]),
            ).fetchall()
            for row in rows:
                deliverable = self.get("deliverable", row["data"]["id"])
                self.check_sources(deliverable["source_version_ids"])
                for source in deliverable["source_version_ids"]:
                    source_version = self.get("version", source)
                    source_file = self.get("file", source_version["file_id"])
                    Work(self.scope).folder(source_file["folder_id"])
        return file, version

    def content(
        self,
        file_id: str,
        version_id: str | None = None,
        range_header: str | None = None,
        *,
        event_kind: str = "file_read",
    ) -> tuple[int, bytes, dict[str, str]]:
        file, version = self.version(file_id, version_id)
        content = self.blob_path(file_id, version["id"]).read_bytes()
        if (
            len(content) != version["size_bytes"]
            or hashlib.sha256(content).hexdigest() != version["sha256"]
        ):
            raise ProjectError(
                503,
                "dependency_unavailable",
                "Canonical bytes failed integrity verification",
            )
        headers = {
            "Content-Type": version["mime"],
            "Accept-Ranges": "bytes",
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(file["relative_path"], safe=""),
            "X-Content-Type-Options": "nosniff",
        }
        status = 200
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
            if match is None:
                raise ProjectError(416, "range_not_satisfiable")
            start = int(match[1])
            end = min(int(match[2]) if match[2] else len(content) - 1, len(content) - 1)
            if (
                start > end
                or end - start + 1 > self.scope.policy["quotas"]["upload_bytes"]
            ):
                raise ProjectError(416, "range_not_satisfiable")
            headers["Content-Range"] = f"bytes {start}-{end}/{len(content)}"
            content = content[start : end + 1]
            status = 206
        self.scope.event(event_kind, self.project)
        self.ledger(event_kind, [version["id"]], len(content))
        return status, content, headers

    def consume(
        self, approval_id: str | None, kind: str, effects: dict[str, Any]
    ) -> None:
        if approval_id is None:
            raise ProjectError(403, "approval_required")
        row = self.db.execute(
            "SELECT * FROM atlas_projects.file_approvals WHERE org_id=%s AND project_id=%s AND id=%s FOR UPDATE",
            (self.org, self.project, approval_id),
        ).fetchone()
        if row is None:
            raise ProjectError(403, "approval_required")
        approval = row["data"]
        if row.get("execution") is not None:
            from services.retrieval.project_work import Work

            if not Work(self.scope).approval_current(row):
                raise ProjectError(410, "approval_expired_or_replayed")
        if (
            row["consumed_at"]
            or approval["consumed_at"]
            or datetime.fromisoformat(approval["expires_at"])
            <= datetime.now(timezone.utc)
        ):
            raise ProjectError(410, "approval_expired_or_replayed")
        if (
            approval["decision"] != "allowed"
            or approval["action_kind"] != kind
            or approval[
                "requested_by_member_id"
                if row.get("execution")
                else "approver_member_id"
            ]
            != self.scope.identity.member
            or approval["org_id"] != self.org
            or approval["project_id"] != self.project
            or approval["policy_revision"] != self.scope.org["revision"]
            or approval["maximum_bytes"] < effects.get("size_bytes", 0)
            or ("path" in effects and approval["paths"] != [effects["path"]])
            or (
                "base_version" in effects
                and approval["version_refs"]
                != [
                    dict(file_id=effects["file_id"], version_id=effects["base_version"])
                ]
            )
            or approval["action_digest"] != action_digest(self.scope, kind, effects)
        ):
            raise ProjectError(403, "approval_required")
        self.db.execute(
            "UPDATE atlas_projects.file_approvals SET consumed_at=now() WHERE org_id=%s AND project_id=%s AND id=%s",
            (self.org, self.project, approval_id),
        )
        if row.get("execution"):
            from services.retrieval.project_work import Work

            Work(self.scope).audit(
                approval, row["execution"]["request_id"], "execution", "succeeded"
            )

    def replace(
        self,
        identifier: str,
        content: bytes,
        base: str,
        expected: str | None,
        approval: str,
    ) -> dict[str, Any]:
        file = self.get("file", identifier)
        self.upload_folder(file["folder_id"])
        self.scope.match(file, expected)
        if file["version_id"] != base:
            raise ProjectError(409, "revision_conflict", revision=file["revision"])
        if identifier == self.scope.project_data["instructions_file_id"]:
            try:
                if len(content.decode("utf-8")) > 4000:
                    raise ValueError("Instructions exceed 4000 characters")
            except UnicodeDecodeError:
                raise ProjectError(422, "invalid_request") from None
        self.consume(approval, "overwrite", self.overwrite_effects(file, content))
        version = self.add_version(file, content)
        file.update(
            {
                k: version[k]
                for k in (
                    "mime",
                    "size_bytes",
                    "sha256",
                    "origin",
                    "availability",
                    "source_locator",
                    "index_status",
                )
            },
            version_id=version["id"],
        )
        self.save(file)
        self.emit("file_versioned", [identifier, version["id"]], len(content))
        return version

    @staticmethod
    def overwrite_effects(file: dict[str, Any], content: bytes) -> dict[str, Any]:
        return dict(
            file_id=file["id"],
            folder_id=file["folder_id"],
            path=file["relative_path"],
            base_version=file["version_id"],
            revision=file["revision"],
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )

    def move_preconditions(
        self, identifier: str, data: dict[str, Any], expected: str | None
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
        file = self.get("file", identifier)
        self.scope.match(file, expected)
        if file["version_id"] != data["base_version"]:
            raise ProjectError(409, "revision_conflict", revision=file["revision"])
        if identifier == self.scope.project_data["instructions_file_id"]:
            raise ProjectError(403, "policy_denied", "PROJECT.md is reserved")
        destination = self.get("folder", data.get("folder_id", file["folder_id"]))
        source = self.get("folder", file["folder_id"])
        if destination["locality"] != source["locality"]:
            raise ProjectError(409, "locality_transition_requires_plan")
        path = data.get("path", file["relative_path"])
        self.collision("file", destination["id"], path, identifier)
        return file, source, destination, path

    def move(
        self, identifier: str, data: dict[str, Any], expected: str | None
    ) -> dict[str, Any]:
        file, source, destination, path = self.move_preconditions(
            identifier, data, expected
        )
        file.update(folder_id=destination["id"], relative_path=path)
        self.save(file, destination["id"])
        self.save(source)
        if source["id"] != destination["id"]:
            self.save(destination)
        if source["model_use"] != destination["model_use"]:
            self.memory_revision()
        self.emit("file_moved", [identifier])
        return file

    def instructions(self) -> dict[str, Any]:
        identifier = self.scope.project_data["instructions_file_id"]
        file, version = self.version(identifier)
        _, content, _ = self.content(identifier)
        common = {k: file[k] for k in self.new()}
        return dict(
            common,
            file_id=identifier,
            version=version["id"],
            text=content.decode("utf-8"),
            active_version_id=version["id"],
            edited_by=version["created_by_member_id"],
            effective_revision=file["revision"],
        )

    def check_sources(self, versions: list[str]) -> None:
        for identifier in versions:
            version = self.get("version", identifier)
            self.version(version["file_id"], identifier, model=True)

    def memory_revision(self) -> int:
        row = self.db.execute(
            "SELECT atlas_projects.advance_memory() AS revision"
        ).fetchone()
        if row is None:
            raise ProjectError(404, "not_found")
        revision = int(row["revision"])
        self.scope.project_data["memory_revision"] = revision
        return revision

    def memory(self, data: dict[str, Any]) -> dict[str, Any]:
        from services.retrieval.project_memory import create

        return create(self, data)

    def patch_memory(
        self, identifier: str, data: dict[str, Any], expected: str | None
    ) -> dict[str, Any]:
        from services.retrieval.project_memory import patch

        return patch(self, identifier, data, expected)

    def tombstone(
        self, value: dict[str, Any], folder: dict[str, Any] | None = None
    ) -> None:
        value["deleted_at"] = timestamp()
        policy = self.retention(folder)
        value["purge_at"] = (
            None
            if policy["legal_hold"]
            else after(value["deleted_at"], policy["deletion_grace_days"])
        )

    def delete_memory(
        self, identifier: str | None, expected: str | None, approval: str | None
    ) -> dict[str, Any]:
        values = (
            [self.get("memory", identifier)] if identifier else self.listing("memory")
        )
        self.scope.match(
            values[0]
            if identifier
            else {"revision": self.scope.project_data["memory_revision"]},
            expected,
        )
        self.consume(
            approval,
            "delete",
            dict(
                memory_ids=sorted(v["id"] for v in values),
                memory_revision=self.scope.project_data["memory_revision"],
            ),
        )
        revision = self.memory_revision()
        for value in values:
            self.tombstone(value)
            value.update(status="deleted", memory_revision=revision)
            self.save(value)
            from services.retrieval.project_memory import audit

            audit(self, value)
        self.emit(
            "context_selected",
            sorted(
                {
                    source
                    for value in values
                    for source in value["source_file_version_ids"]
                }
            ),
            sum(len(value["text"].encode("utf-8")) for value in values),
        )
        return dict(memory_revision=revision)

    def job(
        self, kind: str, refs: list[str], *, purge_at: str | None = None
    ) -> dict[str, Any]:
        value = self.new()
        policy = self.retention()
        value.update(
            kind=kind,
            status="queued",
            progress=0,
            session_id=None,
            purge_stores=[],
            backup_expiry_at=after(timestamp(), policy["backup_expiry_days"])
            if kind == "deletion"
            else None,
            legal_hold=policy["legal_hold"],
        )
        self.put(
            "job",
            value,
            retention="T",
            private=dict(resource_ids=refs, purge_at=purge_at),
        )
        return value

    def delete_file(
        self, identifier: str, expected: str | None, approval: str | None
    ) -> dict[str, Any]:
        file = self.get("file", identifier)
        self.scope.match(file, expected)
        if identifier == self.scope.project_data["instructions_file_id"]:
            raise ProjectError(403, "policy_denied", "PROJECT.md is reserved")
        self.consume(
            approval,
            "delete",
            dict(
                file_id=identifier,
                base_version=file["version_id"],
                revision=file["revision"],
            ),
        )
        folder = self.get("folder", file["folder_id"])
        versions = self.listing("version", identifier)
        self.tombstone(file, folder)
        file["availability"] = "deleted"
        self.save(file)
        for version in versions:
            self.tombstone(version, folder)
            version["availability"] = "deleted"
            self.save(version)
        self.save(folder)
        self.memory_revision()
        job = self.job("deletion", [identifier], purge_at=file["purge_at"])
        self.emit("file_deleted", [identifier])
        return dict(job_id=job["id"], purge_at=file["purge_at"])

    def context(self) -> dict[str, Any]:
        """Task-G seam: authorized data with revisions, never a system-policy prompt."""
        from services.retrieval.project_memory import check_cap

        check_cap(self)
        instructions = self.instructions()
        memory = [v for v in self.listing("memory") if v["status"] == "active"]
        return dict(
            instructions=instructions,
            memory=memory,
            instructions_revision=instructions["effective_revision"],
            memory_revision=self.scope.project_data["memory_revision"],
            policy_revision=self.scope.org["revision"],
        )

    def initialize(self, instructions: str) -> None:
        root = self.folder(dict(name="files", locality="server", model_use="allow"))
        self.folder(
            dict(name="deliverables", locality="server", model_use="allow"),
            self.scope.project_data["deliverables_folder_id"],
        )
        self.create_file(
            root["id"],
            "PROJECT.md",
            instructions.encode("utf-8"),
            root["revision"],
            identifier=self.scope.project_data["instructions_file_id"],
            initial=True,
        )
