"""Scoped embedding publication; durable ledger intent precedes each gateway call."""

from __future__ import annotations

import json
import os
import time
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from services.retrieval.project_documents import ScopedProjectDocuments
from services.retrieval.project_files import ProjectFiles, after, timestamp
from services.retrieval.project_identity import Identity, ProjectError
from services.retrieval.project_ledger import append
from services.retrieval.project_store import ScopedProjects
from services.retrieval.search import Gateway


def save(files: ProjectFiles, record: dict[str, Any]) -> None:
    size = sum(len(c["text"].encode()) for c in record["chunks"]) + len(
        json.dumps(record.get("vectors", [])).encode()
    )
    files.scope.reconcile(record["id"], "derived_bytes", float(size))
    files.db.execute(
        "UPDATE atlas_projects.resources SET data=%s WHERE org_id=%s AND project_id=%s AND id=%s",
        (Jsonb(record), files.org, files.project, record["id"]),
    )


def finish_job(files: ProjectFiles, record: dict[str, Any], succeeded: bool) -> None:
    job = files.get("job", record["job_id"])
    job.update(status="completed" if succeeded else "failed", progress=1)
    files.save(job)
    files.db.execute(
        "UPDATE atlas_projects.resources SET expires_at=%s WHERE org_id=%s AND project_id=%s AND id=%s",
        (
            after(
                timestamp(), files.retention()["temporary_after_terminal_hours"] / 24
            ),
            files.org,
            files.project,
            job["id"],
        ),
    )


def event(files: ProjectFiles, model: dict[str, Any], kind: str, outcome: str) -> None:
    append(
        files.scope,
        kind=kind,
        request_id=model["call_id"],
        actor=dict(kind="service", member_id=None, service="retrieval"),
        resource_refs=model["sources"],
        source=dict(kind="server", ref=None),
        destination=dict(kind="model", ref=model["provider_ref"]),
        byte_counts=model["byte_counts"],
        outcome=outcome,
        model_dispatch=model,
    )


def index_upload(
    repository: ScopedProjects, identity: Identity, file_id: str, version_id: str
) -> None:
    """Runs after the upload commits; replays never dispatch the same attempt twice."""
    started = time.monotonic()
    allowance = min(120.0, max(0.0, identity.expires - time.time() - 2))
    deadline = started + allowance
    gateway_url = os.environ.get("ATLAS_GATEWAY_URL")
    gateway = Gateway(gateway_url) if gateway_url else None
    with repository.scope(identity, identity.org, identity.project) as scope:
        files = ProjectFiles(scope)
        documents = ScopedProjectDocuments(files)
        file, _ = files.version(file_id, version_id, model=True, extraction=True)
        documents.authorize(file)
        if file["version_id"] != version_id:
            return
        record = documents.derived(version_id)
        if (
            not record
            or record.get("processor") != "located-ingestion-1"
            or not record["chunks"]
            or record.get("index_attempted")
        ):
            return
        record["index_attempted"] = True
        record["vectors"] = []
        if gateway is None:
            record["extraction"]["coverage"]["warnings"].append(
                "Embeddings unavailable: model gateway is not configured; only extracted text is available"
            )
            record["extraction"]["coverage"]["complete"] = False
            documents.status(file, "failed")
            finish_job(files, record, False)
            save(files, record)
            return
        record["index_operation"] = str(uuid4())
        try:
            scope.reserve(record["index_operation"], {"concurrent_jobs": 1.0})
        except ProjectError as exc:
            record["extraction"]["coverage"]["warnings"].append(exc.body["message"])
            record["extraction"]["coverage"]["complete"] = False
            documents.status(file, "failed")
            finish_job(files, record, False)
            save(files, record)
            return
        deadline = min(
            deadline,
            time.monotonic() + scope.policy["quotas"]["per_turn"]["wall_seconds"],
        )
        allowance = deadline - started
        save(files, record)
    try:
        for offset in range(0, len(record["chunks"]), 32):
            batch = record["chunks"][offset : offset + 32]
            payload: dict[str, Any] = dict(
                texts=[c["text"] for c in batch], kind="document"
            )
            refs = [
                {k: c[k] for k in ("file_id", "version_id", "locator")} for c in batch
            ]
            with repository.scope(identity, identity.org, identity.project) as scope:
                files = ProjectFiles(scope)
                documents = ScopedProjectDocuments(files)
                file, version = files.version(
                    file_id, version_id, model=True, extraction=True
                )
                documents.authorize(file)
                if file["version_id"] != version_id:
                    raise ProjectError(409, "revision_conflict")
                scope.check_quota("derived_bytes", len(batch) * 1024 * 24)
                if time.monotonic() >= deadline:
                    raise ValueError(
                        f"Indexing elapsed seconds: {time.monotonic() - started:.3f}; limit {allowance:.3f}"
                    )
                instructions = files.instructions()
                model = dict(
                    call_id=str(uuid4()),
                    attempt=1,
                    provider_ref="model-gateway-cpu",
                    purpose="embedding",
                    sources=refs,
                    instructions_version=instructions["version"],
                    memory_revision=scope.project_data["memory_revision"],
                    byte_counts=dict(
                        source_bytes=version["size_bytes"],
                        selected_utf8_bytes=sum(len(c["text"].encode()) for c in batch),
                        image_bytes=0,
                        serialized_payload_bytes=len(
                            json.dumps(payload, allow_nan=False).encode()
                        ),
                        response_bytes=0,
                    ),
                    input_tokens=0,
                    output_tokens=0,
                    tokens_estimated=True,
                    transmission="not_sent",
                )
                event(files, model, "model_dispatch_intent", "intent")
            # Intent is committed. Hold current policy lock over the bounded call so
            # revocation cannot race the final authorization check and egress.
            with repository.scope(identity, identity.org, identity.project) as scope:
                scope.authorize("POST", ["files"])
                files = ProjectFiles(scope)
                checked_file, _ = files.version(
                    file_id, version_id, model=True, extraction=True
                )
                ScopedProjectDocuments(files).authorize(checked_file)
                gateway.timeout = min(30.0, max(0.01, deadline - time.monotonic()))
                try:
                    vectors, revision = gateway.embed(payload["texts"], "document")
                    model["transmission"] = "acknowledged"
                    model["byte_counts"]["response_bytes"] = gateway.response_bytes
                    event(files, model, "model_acknowledged", "succeeded")
                except (ValueError, OSError, TimeoutError):
                    model["transmission"] = "possibly_sent"
                    event(files, model, "model_unknown", "unknown")
                    record["index_error"] = (
                        "Embedding gateway failed; transmission may have occurred"
                    )
                    save(files, record)
                    break
                if record.get("embedding_revision", revision) != revision:
                    record["index_error"] = "Embedding revision changed during indexing"
                    save(files, record)
                    break
                candidate = dict(
                    record,
                    embedding_revision=revision,
                    vectors=record["vectors"] + vectors,
                )
                try:
                    save(files, candidate)
                except ProjectError as exc:
                    record["index_error"] = exc.body["message"]
                    save(files, record)
                    break
                record = candidate
    except (ProjectError, ValueError) as exc:
        record["index_error"] = (
            exc.body["message"] if isinstance(exc, ProjectError) else str(exc)
        )
    finally:
        with repository.scope(identity, identity.org, identity.project) as scope:
            files = ProjectFiles(scope)
            documents = ScopedProjectDocuments(files)
            file = files.get("file", file_id)
            scope.reconcile(record["index_operation"], "concurrent_jobs", 0)
            complete = len(record["vectors"]) == len(record["chunks"])
            if not complete:
                record["extraction"]["coverage"]["complete"] = False
                record["extraction"]["coverage"]["warnings"].append(
                    record.get("index_error", "Embedding index is incomplete")
                )
            save(files, record)
            finish_job(files, record, complete)
            if file["version_id"] == version_id:
                documents.status(file, "ready" if complete else "failed")
