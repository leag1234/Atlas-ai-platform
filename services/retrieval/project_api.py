"""Project HTTP boundary; scoped file routes reuse task-B authorization.

The included scoped router owns durable folders, files, instructions, memory,
and the paginated ledger and byte-summary read endpoints.
Legacy SQLite routes remain disabled whenever the scoped installation is enabled.
"""

from packages.limits import LimitError
from packages.validation import describe_validation

import logging
import os
from packages.ports import atlas_ports
import sqlite3
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.retrieval.project_documents import ProjectDocuments
from services.retrieval.projects import Projects
from services.retrieval.search import Gateway

from services.retrieval.project_scoped_api import router as scoped_router

router = APIRouter()
router.include_router(scoped_router)
Text = Annotated[str, Field(min_length=1, max_length=32000, pattern=r"\S")]
Name = Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")]
Fact = Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]


class Named(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Name


class ProjectInput(Named):
    instructions: str = Field(default="", max_length=4000)
    privacy: Literal["standard", "strict"] = "standard"


class DocumentInput(Named):
    text: Text
    lang: Literal["fr", "de", "es", "it", "en"] = "fr"


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: Text


class ContextInput(SearchInput):
    conversation_id: str


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    conversation_id: str
    question: Text
    answer: Text
    facts: list[Fact] = Field(max_length=8)
    revision: int = Field(ge=0)


class FactInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: Fact


def dispatch(method: str, path: str, body: bytes) -> object:
    parts = path.strip("/").split("/") if path.strip("/") else []
    store = Projects(Path(os.environ.get("ATLAS_PROJECT_DB", "BRAIN/projects.sqlite")))
    if not parts:
        if method == "GET":
            return store.listing()
        if method == "POST":
            p = ProjectInput.model_validate_json(body)
            return {"id": store.create(p.name, p.instructions, p.privacy)}
        raise KeyError("not_found")
    project = parts[0]
    try:
        UUID(project)
    except ValueError:
        raise KeyError("not_found") from None
    store.get(project)
    tail = parts[1:]
    if not tail:
        if method == "GET":
            return store.get(project)
        if method == "PATCH":
            p = ProjectInput.model_validate_json(body)
            store.update(project, p.name, p.instructions)
            store.set_privacy(project, p.privacy)
            return {"ok": True}
    if tail == ["conversations"]:
        if method == "GET":
            return store.conversations(project)
        if method == "POST":
            name = Named.model_validate_json(body)
            return {"id": store.conversation(project, name.name)}
    if len(tail) == 2 and tail[0] == "conversations" and method == "GET":
        return store.history(project, tail[1])
    if tail == ["context"] and method == "POST":
        context = ContextInput.model_validate_json(body)
        return store.context(project, context.conversation_id, context.query)
    if tail == ["turns"] and method == "POST":
        turn = TurnInput.model_validate_json(body)
        store.turn(
            project,
            turn.conversation_id,
            turn.question,
            turn.answer,
            turn.facts,
            turn.revision,
        )
        return {"ok": True}
    if tail == ["facts"] and method == "GET":
        return store.facts(project)
    if tail == ["facts"] and method == "DELETE":
        store.erase(project)
        return {"ok": True}
    if len(tail) == 2 and tail[0] == "facts":
        if method == "DELETE":
            store.erase(project, tail[1])
            return {"ok": True}
        if method == "PATCH":
            fact = FactInput.model_validate_json(body)
            store.edit(project, tail[1], fact.text)
            return {"ok": True}
    if tail in (["documents"], ["search"]) or (len(tail) == 2 and tail[0] == "sources"):
        documents = ProjectDocuments(
            store,
            Gateway(os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway"))),
        )
        if tail == ["documents"] and method == "POST":
            document = DocumentInput.model_validate_json(body)
            return {
                "id": documents.add(
                    project, document.name, document.text, document.lang
                )
            }
        if tail == ["documents"] and method == "GET":
            return documents.listing(project)
        if tail == ["search"] and method == "POST":
            query = SearchInput.model_validate_json(body)
            return {"passages": documents.search(project, query.query)}
        if len(tail) == 2 and tail[0] == "sources" and method == "GET":
            return documents.resolve(project, tail[1]).model_dump(
                include={"chunk_id", "doc_id", "source", "text"}
            )
    raise KeyError("not_found")


@router.api_route("/projects", methods=["GET", "POST"])
@router.api_route("/projects/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def projects(request: Request, path: str = "") -> Response:
    import asyncio

    if os.environ.get("ATLAS_PROJECT_DSN"):
        from services.retrieval.project_identity import ProjectError

        error = ProjectError(404, "not_found")
        return JSONResponse(error.body, status_code=404)

    if request.headers.get("origin") not in (None, str(request.base_url).rstrip("/")):
        return JSONResponse({"error": "origin_rejected"}, status_code=403)
    code = ""
    status = 200
    try:
        body = bytearray()
        async with asyncio.timeout(5):
            async for piece in request.stream():
                body.extend(piece)
                if len(body) > 160000:
                    raise LimitError("body_limit", len(body), 160000, "bytes")
        result = await asyncio.to_thread(dispatch, request.method, path, bytes(body))
        return JSONResponse(result)
    except LimitError as exc:
        status = 413
        return JSONResponse(
            {"error": exc.code, "message": exc.detail}, status_code=status
        )
    except ValidationError as exc:
        status = 422
        return JSONResponse(
            {"error": "invalid_request", "message": describe_validation(exc)},
            status_code=status,
        )
    except KeyError:
        status, code = 404, "not_found"
    except (ValidationError, ValueError, sqlite3.IntegrityError):
        status, code = 422, "invalid_request"
    except (OSError, sqlite3.Error, RuntimeError):
        status, code = 503, "project_unavailable"
    finally:
        logging.getLogger(__name__).info(
            "project_operation method=%s status=%s", request.method, status
        )
    return JSONResponse({"error": code}, status_code=status)
