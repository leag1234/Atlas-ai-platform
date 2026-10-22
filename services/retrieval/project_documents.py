"""Project-specific document indexes reuse M2 embeddings, chunks and ranking."""

import hashlib
import json
from uuid import uuid4
from typing import Any

from psycopg.types.json import Jsonb
from services.retrieval.project_files import ProjectFiles, after, timestamp
from services.retrieval.project_identity import ProjectError

from services.retrieval.extract import split_text
from services.retrieval.projects import Projects, bounded
from services.retrieval.search import Gateway, rank_scored
from services.retrieval.store import Chunk


class ProjectDocuments:
    def __init__(self, projects: Projects, gateway: Gateway) -> None:
        self.projects, self.gateway = projects, gateway
        with projects.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS project_documents (
                id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id),
                name TEXT, chunks TEXT)""")

    def add(self, project: str, name: str, text: str, lang: str) -> str:
        self.projects.get(project)
        bounded(name, 120)
        bounded(text, 32000)
        if lang not in {"fr", "de", "es", "it", "en"}:
            raise ValueError("invalid_language")
        parts = split_text(text)
        vectors, revision = self.gateway.embed(parts, "document")
        identifier = str(uuid4())
        digest = hashlib.sha256(text.encode()).hexdigest()
        chunks = [
            Chunk(
                chunk_id=hashlib.sha256(
                    f"{project}:{identifier}:{i}".encode()
                ).hexdigest(),
                doc_id=identifier,
                source=name,
                langue=lang,
                source_sha256=digest,
                embedding_revision=revision,
                position=i,
                text=part,
                embedding=vector,
            )
            for i, (part, vector) in enumerate(zip(parts, vectors, strict=True))
        ]
        with self.projects.connection() as db:
            self.projects.require(db, project)
            db.execute(
                "INSERT INTO project_documents VALUES (?,?,?,?)",
                (
                    identifier,
                    project,
                    name,
                    json.dumps([c.model_dump() for c in chunks]),
                ),
            )
        return identifier

    def listing(self, project: str) -> list[dict[str, object]]:
        with self.projects.connection() as db:
            self.projects.require(db, project)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,name FROM project_documents WHERE project=? ORDER BY rowid",
                    (project,),
                )
            ]

    def chunks(self, project: str) -> list[Chunk]:
        with self.projects.connection() as db:
            self.projects.require(db, project)
            return [
                Chunk.model_validate(c)
                for r in db.execute(
                    "SELECT chunks FROM project_documents WHERE project=? ORDER BY rowid",
                    (project,),
                )
                for c in json.loads(r["chunks"])
            ]

    def resolve(self, project: str, key: str) -> Chunk:
        for chunk in self.chunks(project):
            if chunk.chunk_id == key:
                return chunk
        raise KeyError("not_found")

    def search(self, project: str, query: str) -> list[dict[str, object]]:
        return [
            {
                **chunk.model_dump(include={"chunk_id", "doc_id", "source", "text"}),
                "score": score,
            }
            for chunk, score in rank_scored(query, self.chunks(project), self.gateway)
        ]


class ScopedProjectDocuments:
    """Source-version-bound extracts; context selection rechecks current restrictions.

    P0 stores UTF-8 text without a model call. Binary extraction belongs to the
    document processor; its status remains excluded until that processor runs.
    """

    def __init__(self, files: "ProjectFiles") -> None:
        self.files = files

    def ingest(self, file: dict[str, Any], content: bytes, job: dict[str, Any]) -> None:
        files = self.files
        folder = files.get("folder", file["folder_id"])
        parts: list[str] = []
        status = "excluded"
        if folder["model_use"] == "allow" and file["mime"].startswith("text/"):
            try:
                text = content.decode("utf-8")
                if len(text) <= 4 * 1024 * 1024:
                    parts = split_text(text)
                    status = "ready"
                else:
                    status = "failed"
            except UnicodeDecodeError:
                status = "failed"
        if parts:
            record = files.new()
            chunks: list[dict[str, Any]] = []
            position = 0
            for part in parts:
                start = text.find(part, position)
                start = max(0, start)
                position = start + len(part)
                chunks.append(
                    dict(
                        file_id=file["id"],
                        version_id=file["version_id"],
                        chunk_id=hashlib.sha256(
                            f"{file['version_id']}:{len(chunks)}".encode()
                        ).hexdigest(),
                        locator=dict(kind="text", start=start, end=position),
                        text=part,
                    )
                )
            size = sum(len(part.encode()) for part in parts)
            files.scope.reserve(record["id"], {"derived_bytes": float(size)})
            files.scope.reconcile(record["id"], "derived_bytes", float(size))
            record["chunks"] = chunks
            files.put("derived", record, file["version_id"], retention="E")
            files.emit("extraction_completed", [file["version_id"]])
        file["index_status"] = status
        version = files.get("version", file["version_id"])
        version["index_status"] = status
        for value in (file, version):
            # Processing metadata does not change the optimistic content revision.
            files.db.execute(
                "UPDATE atlas_projects.resources SET data=%s WHERE org_id=%s AND project_id=%s AND id=%s",
                (Jsonb(value), files.org, files.project, value["id"]),
            )
        job.update(status="failed" if status == "failed" else "completed", progress=1)
        files.save(job)
        files.db.execute(
            "UPDATE atlas_projects.resources SET expires_at=%s WHERE org_id=%s AND project_id=%s AND id=%s",
            (
                after(
                    timestamp(),
                    files.retention()["temporary_after_terminal_hours"] / 24,
                ),
                files.org,
                files.project,
                job["id"],
            ),
        )

    def sources(self) -> list[dict[str, Any]]:
        files = self.files
        result: list[dict[str, Any]] = []
        rows = files.db.execute(
            "SELECT parent_id,data FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND kind='derived' AND data->>'deleted_at' IS NULL",
            (files.org, files.project),
        ).fetchall()
        for row in rows:
            try:
                version = files.get("version", str(row["parent_id"]))
                file, _ = files.version(
                    version["file_id"], version["id"], model=True, extraction=True
                )
                if file["version_id"] == version["id"]:
                    result.extend(row["data"]["chunks"])
            except ProjectError as exc:
                if exc.status not in (403, 404, 409):
                    raise
        return result

    def resolve(self, key: str) -> dict[str, Any]:
        for source in self.sources():
            if source["chunk_id"] == key:
                return source
        raise ProjectError(404, "not_found")
