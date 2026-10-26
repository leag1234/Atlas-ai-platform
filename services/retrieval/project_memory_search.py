"""One scoped derived index and hybrid ranker for documents and typed memory."""

from __future__ import annotations

import hashlib
import os
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from packages.project_memory import pinned
from services.retrieval.project_documents import ScopedProjectDocuments
from services.retrieval.project_files import ProjectFiles
from services.retrieval.project_index import event
from services.retrieval.search import Gateway, rank_scored, tokens
from services.retrieval.store import Chunk


class AuditedGateway(Gateway):
    def __init__(
        self, files: ProjectFiles, sources: list[dict[str, Any]], url: str
    ) -> None:
        super().__init__(url)
        self.files, self.sources = files, sources

    def post(self, operation: str, payload: dict[str, object]) -> object:
        import json

        files = self.files
        if (
            files.scope.role == "viewer"
            or files.scope.project_data["state"] != "active"
        ):
            # Read-only Chat cannot publish index records or editor ledger events.
            # CPU retrieval is still authorized; retain its boundary in the audit.
            files.scope.event("memory_retrieval_" + operation, files.project)
            return super().post(operation, payload)
        model = dict(
            call_id=str(uuid4()),
            attempt=1,
            provider_ref="model-gateway-cpu",
            purpose="embedding" if operation == "embeddings" else "rerank",
            sources=self.sources,
            instructions_version=files.instructions()["version"],
            memory_revision=files.scope.project_data["memory_revision"],
            byte_counts=dict(
                source_bytes=0,
                selected_utf8_bytes=len(json.dumps(payload).encode()),
                image_bytes=0,
                serialized_payload_bytes=len(json.dumps(payload).encode()),
                response_bytes=0,
            ),
            input_tokens=0,
            output_tokens=0,
            tokens_estimated=True,
            transmission="not_sent",
        )
        event(files, model, "model_dispatch_intent", "intent")
        try:
            result = super().post(operation, payload)
        except (ValueError, OSError):
            model["transmission"] = "possibly_sent"
            event(files, model, "model_unknown", "unknown")
            raise
        model["transmission"] = "acknowledged"
        model["byte_counts"]["response_bytes"] = self.response_bytes
        event(files, model, "model_acknowledged", "succeeded")
        return result


def search(
    files: ProjectFiles,
    query: str,
    passages: list[dict[str, Any]],
    memories: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    memories = [v for v in memories if v["status"] == "active" and not pinned(v)]
    values = {p["chunk_id"]: p for p in passages}
    values.update({m["id"]: m for m in memories})
    if not values:
        return [], []
    terms = set(tokens(query))
    keys = sorted(
        values, key=lambda k: (-len(terms & set(tokens(values[k]["text"]))), k)
    )
    url = os.environ.get("ATLAS_GATEWAY_URL")
    if url:
        sources = [
            {k: p[k] for k in ("file_id", "version_id", "locator")} for p in passages
        ]
        gateway = AuditedGateway(files, sources, url)
        documents = ScopedProjectDocuments(files)
        writable = (
            files.scope.role in {"owner", "editor"}
            and files.scope.project_data["state"] == "active"
        )
        chunks = []
        for key, value in values.items():
            is_memory = "kind" in value
            parent = value["id"] if is_memory else value["version_id"]
            record = documents.derived(parent)
            if is_memory and not record:
                record = dict(
                    files.new(),
                    processor="typed-memory-1",
                    memory_revision=value["revision"],
                    chunks=[dict(chunk_id=key, text=value["text"])],
                )
                if writable:
                    files.scope.reserve(
                        record["id"],
                        {"derived_bytes": float(len(str(record).encode()))},
                    )
                    files.put("derived", record, parent, retention="E")
            if not record:
                continue
            parts = record["chunks"]
            if len(record.get("vectors", [])) != len(parts):
                vectors, revision = gateway.embed(
                    [p["text"] for p in parts], "document"
                )
                record.update(vectors=vectors, embedding_revision=revision)
                if writable:
                    files.scope.reconcile(
                        record["id"], "derived_bytes", float(len(str(record).encode()))
                    )
                    files.db.execute(
                        "UPDATE atlas_projects.resources SET data=%s WHERE org_id=%s AND project_id=%s AND id=%s",
                        (Jsonb(record), files.org, files.project, record["id"]),
                    )
            position = next(i for i, p in enumerate(parts) if p["chunk_id"] == key)
            chunks.append(
                Chunk(
                    chunk_id=hashlib.sha256(key.encode()).hexdigest(),
                    doc_id=parent,
                    source=value.get("source", "project document"),
                    langue="en",
                    source_sha256=hashlib.sha256(value["text"].encode()).hexdigest(),
                    embedding_revision=record["embedding_revision"],
                    position=position,
                    text=value["text"],
                    embedding=record["vectors"][position],
                )
            )
        if chunks:
            mapping = {hashlib.sha256(k.encode()).hexdigest(): k for k in values}
            keys = [mapping[c.chunk_id] for c, _ in rank_scored(query, chunks, gateway)]
    selected = [values[k] for k in keys[:8]]
    return [v for v in selected if "kind" not in v], [
        v for v in selected if "kind" in v
    ]
