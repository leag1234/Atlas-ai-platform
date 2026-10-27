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
    """Source-version-bound extracts; every read rechecks current restrictions."""

    def __init__(self, files: "ProjectFiles") -> None:
        self.files = files

    def authorize(self, file: dict[str, Any]) -> None:
        from services.retrieval.project_work import Work

        Work(self.files.scope).folder(file["folder_id"])

    def ingest(self, file: dict[str, Any], content: bytes, job: dict[str, Any]) -> None:
        from pathlib import PurePosixPath
        from services.retrieval.project_extraction import bounded_extract, coverage

        files = self.files
        allowed = True
        try:
            self.authorize(file)
        except ProjectError as exc:
            if exc.status not in {403, 404, 409}:
                raise
            allowed = False
        extracted: dict[str, Any] = dict(
            segments=[], coverage=coverage("characters", None, 0, [])
        )
        status = "excluded"
        if not allowed:
            # Restricted documents never reach a parser, index or model.
            reason = "Folder model use does not allow indexing"
            extracted["coverage"]["warnings"].append(reason)
        else:
            extension = PurePosixPath(file["relative_path"]).suffix.lower()
            if extension not in {
                ".pdf",
                ".docx",
                ".xlsx",
                ".csv",
                ".pptx",
                ".md",
                ".html",
                ".htm",
            } and file["mime"].startswith("text/"):
                extension = ".txt"
            try:
                from services.retrieval.project_work_contract import validate

                candidate = bounded_extract(content, extension)
                try:
                    validate("Extraction", candidate)
                except ProjectError:
                    raise ValueError(
                        "Document structure has invalid source locations"
                    ) from None
                extracted = candidate
                status = "pending" if extracted["segments"] else "excluded"
            except ValueError as exc:
                extracted["coverage"]["warnings"].append(str(exc))
        chunks = []
        for position, segment in enumerate(extracted["segments"]):
            chunks.append(
                dict(
                    file_id=file["id"],
                    version_id=file["version_id"],
                    chunk_id=hashlib.sha256(
                        f"{file['version_id']}:{position}".encode()
                    ).hexdigest(),
                    locator=segment["locator"],
                    text=segment["text"],
                )
            )
        record = dict(
            files.new(),
            chunks=chunks,
            extraction=extracted,
            processor="located-ingestion-1",
            job_id=job["id"],
        )
        size = sum(len(c["text"].encode()) for c in chunks)
        files.scope.reserve(record["id"], {"derived_bytes": float(size)})
        files.scope.reconcile(record["id"], "derived_bytes", float(size))
        files.put("derived", record, file["version_id"], retention="E")
        if chunks:
            files.emit(
                "extraction_completed",
                [file["version_id"]],
                located_sources=[
                    {k: c[k] for k in ("file_id", "version_id", "locator")}
                    for c in chunks
                ],
                selected_utf8_bytes=size,
            )
        self.status(file, status)
        job.update(
            status="running" if chunks else "completed", progress=0.5 if chunks else 1
        )
        files.save(job)
        if not chunks:
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

    def status(self, file: dict[str, Any], status: str) -> None:
        files = self.files
        version = files.get("version", file["version_id"])
        for value in (file, version):
            value["index_status"] = status
            files.db.execute(
                "UPDATE atlas_projects.resources SET data=%s WHERE org_id=%s AND project_id=%s AND id=%s",
                (Jsonb(value), files.org, files.project, value["id"]),
            )

    def derived(self, version: str) -> dict[str, Any] | None:
        row = self.files.db.execute(
            "SELECT data FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND kind='derived' AND parent_id=%s AND data->>'deleted_at' IS NULL",
            (self.files.org, self.files.project, version),
        ).fetchone()
        return dict(row["data"]) if row else None

    def extraction(self, identifier: str, query: dict[str, str]) -> dict[str, Any]:
        import os
        from services.retrieval.project_pages import page
        from services.retrieval.project_extraction import coverage

        file, version = self.files.version(
            identifier, query.get("version"), model=True, extraction=True
        )
        self.authorize(file)
        record = self.derived(version["id"])
        if record is None:
            raise ProjectError(
                404, "not_found", "No retained extraction for this version"
            )
        extracted = record.get("extraction") or dict(
            segments=[{k: c[k] for k in ("locator", "text")} for c in record["chunks"]],
            coverage=coverage(
                "characters", None, 0, ["Legacy extraction coverage is unknown"]
            ),
        )
        limit = int(query.get("limit", "50"))
        if not 1 <= limit <= 100:
            raise ProjectError(422, "invalid_request")
        result = page(
            self.files.scope,
            "extraction/" + identifier + "/" + version["id"],
            extracted["segments"],
            min(limit, 10),
            query.get("cursor"),
            os.environ.get("ATLAS_PROJECT_SIGNING_KEY", "").encode(),
        )
        cov = dict(extracted["coverage"])
        if query.get("cursor") or result["next_cursor"]:
            cov["complete"] = False
            cov["warnings"] = cov["warnings"] + [
                "This response is one page of the indexed extraction"
            ]
        return dict(
            segments=result["items"], coverage=cov, next_cursor=result["next_cursor"]
        )

    def sources(self) -> list[dict[str, Any]]:
        files = self.files
        result: list[dict[str, Any]] = []
        rows = files.db.execute(
            "SELECT parent_id,data FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND kind='derived' AND data->>'deleted_at' IS NULL",
            (files.org, files.project),
        ).fetchall()
        for row in rows:
            if row["data"].get("processor") == "typed-memory-1":
                continue
            try:
                version = files.get("version", str(row["parent_id"]))
                file, _ = files.version(
                    version["file_id"], version["id"], model=True, extraction=True
                )
                self.authorize(file)
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
