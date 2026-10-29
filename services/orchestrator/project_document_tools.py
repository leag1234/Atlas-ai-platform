"""REQ-PROJ-05: scoped document tools; bytes go only to the networkless worker."""

from __future__ import annotations

import base64
import asyncio
import hashlib
import json
from pathlib import PurePosixPath
import time
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from packages.doc_render.schema import Content
from services.orchestrator.project_document_input import DocumentInput, normalize


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Selector(Arguments):
    page_start: int | None = Field(default=None, ge=1, le=1000)
    page_end: int | None = Field(default=None, ge=1, le=1000)
    paragraph_start: int | None = Field(default=None, ge=1, le=100000)
    paragraph_end: int | None = Field(default=None, ge=1, le=100000)
    sheet: str | None = Field(default=None, min_length=1, max_length=255)
    range: str | None = Field(default=None, max_length=32)


class Input(Arguments):
    file_id: str
    version: str
    selector: Selector = Field(default_factory=Selector)

    @field_validator("selector", mode="before")
    @classmethod
    def decode_selector(cls, value: Any) -> Any:
        # Some providers encode nested tool objects as JSON strings. Decode
        # once, then retain the same closed, strictly typed selector boundary.
        return json.loads(value) if isinstance(value, str) else value


class Read(Input):
    cursor: str | None = Field(default=None, max_length=100)


class Options(Arguments):
    fidelity: Literal["text"] = "text"


class Convert(Input):
    target: Literal["text", "markdown", "csv", "pdf"]
    options: Options = Field(default_factory=Options)


class Compare(Arguments):
    left: Input
    right: Input


class Move(Arguments):
    file_id: str
    base_version: str
    expected_revision: int = Field(ge=0)
    folder_id: str
    path: str = Field(min_length=1, max_length=1024)


class Organize(Arguments):
    mode: Literal["preview", "apply"]
    operations: list[Move] = Field(min_length=1, max_length=100)


class Publish(Arguments):
    path: str = Field(min_length=1, max_length=1024)
    content: Content
    sources: list[Input] = Field(max_length=100)
    strategy: Literal["created", "regenerated", "edited_in_place"] = "created"


class ModelPublish(DocumentInput):
    path: str = Field(min_length=1, max_length=1024)
    sources: list[Input] = Field(max_length=100)
    strategy: Literal["created", "regenerated", "edited_in_place"] = "created"


SCHEMAS: dict[str, type[Arguments]] = {
    "read_document": Read,
    "convert_document": Convert,
    "compare_documents": Compare,
    "organize_files": Organize,
    "publish_document": Publish,
}
DESCRIPTIONS = {
    "read_document": "Read selected immutable PDF pages, DOCX body paragraphs/tables or XLSX stored cells with precise locators, bounded coverage and continuation. Formulas are never executed.",
    "convert_document": "Convert selected PDF text to text/Markdown, DOCX body to text/reflowed PDF, or one XLSX sheet to CSV/reflowed PDF. Text fidelity only; stages a new workspace file, never publishes or overwrites.",
    "compare_documents": "Compare two explicit immutable document inputs and their independent selectors; return located changes and bounded completeness. Partial comparisons are not exhaustive.",
    "organize_files": "Preview or atomically apply up to 100 version/revision-bound canonical moves in selected session folders; no swaps, overwrites or locality changes. Preview does not apply.",
    "publish_document": "Create a durable DOCX, XLSX, PPTX or PDF in project deliverables with a verified text preview. For DOCX/PDF/XLSX send content as Markdown text with headings, paragraphs, lists and simple pipe tables. For XLSX formulas set formulas=true: cells starting with = support SUM and SUMIF with exact criteria and bounded local cell/range references, including numbered sheets. Each section is a sheet named 1, 2, etc.; first sheet starts with title and section heading, other sheets with the heading; table headers follow any paragraphs/bullets. Numeric table cells are numbers. Formula caches are calculated and verified by code; unsupported formulas are rejected. For PPTX send title and slides with title and bullets; omit content. A JSON-encoded strict Content object is also accepted in content. Declare immutable sources and selectors. Markdown publication appends missing selected source excerpts as a Sources appendix. Strict Content objects must already contain every selected source segment. Use an empty sources list for original content. Images require scoped resolution and are rejected. Existing paths are never overwritten. Returns canonical download and preview links only after reopening and verification.",
}


def declarations() -> list[dict[str, object]]:
    return [
        dict(
            type="function",
            function=dict(
                name=name,
                description=DESCRIPTIONS[name],
                parameters=(
                    ModelPublish if name == "publish_document" else schema
                ).model_json_schema(),
            ),
        )
        for name, schema in SCHEMAS.items()
    ]


async def resolve(
    execution: Any, value: dict[str, Any], receipts: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    from pathlib import PurePosixPath
    from services.retrieval.project_identity import ProjectError

    file_id, version = str(UUID(value["file_id"])), str(UUID(value["version"]))
    if dict(file_id=file_id, version_id=version) not in execution.session[
        "base_file_versions"
    ] and not any(
        receipt.get("file_version_id") == version
        and receipt.get("turn_id") == execution.request_id
        for receipt in (receipts or [])
    ):
        raise ProjectError(404, "not_found")
    await execution.check()
    own_receipt = next(
        (
            receipt
            for receipt in (receipts or [])
            if receipt.get("id") == file_id
            and receipt.get("file_version_id") == version
            and receipt.get("turn_id") == execution.request_id
        ),
        None,
    )
    if own_receipt is not None:
        status, listing = await execution.broker.public("GET", "files")
        if status != 200:
            raise ProjectError(status, "dependency_unavailable")
        published = next(
            (item for item in listing["items"] if item["version_id"] == version), None
        )
        if published is None:
            raise ProjectError(404, "not_found")
        file_id = published["id"]
    status, file = await execution.broker.public("GET", "files/" + file_id)
    if status != 200:
        raise ProjectError(status, str(file.get("error", "not_found")))
    if file["version_id"] != version:
        raise ProjectError(409, "revision_conflict")
    raw = await execution.broker.content(file_id, version, 16 * 1024 * 1024)
    if (
        len(raw) != file["size_bytes"]
        or hashlib.sha256(raw).hexdigest() != file["sha256"]
    ):
        raise ProjectError(503, "dependency_unavailable")
    return dict(
        value,
        file_id=file_id,
        version=version,
        extension=PurePosixPath(file["relative_path"]).suffix.lower(),
        content=base64.b64encode(raw).decode(),
    )


async def execute(
    tools: Any, name: str, arguments: str, timeout: float
) -> dict[str, Any]:
    value = json.loads(arguments)
    markdown_input = (
        name == "publish_document"
        and isinstance(value, dict)
        and isinstance(value.get("content"), str)
        and not value["content"].lstrip().startswith(("{", "["))
    )
    if name == "publish_document":
        value = normalize(value)
    args = SCHEMAS[name].model_validate(value).model_dump(exclude_none=True)
    await tools.execution.check()
    if name == "publish_document":
        return await publish(tools, args, timeout, append_sources=markdown_input)
    if name == "organize_files":
        return dict(
            await tools.execution.broker.call(
                "organize", dict(tools.execution.fence(), **args)
            )
        )
    if name == "compare_documents":
        payload = dict(
            left=await resolve(
                tools.execution,
                args["left"],
                tools.deliverables if isinstance(tools.deliverables, list) else [],
            ),
            right=await resolve(
                tools.execution,
                args["right"],
                tools.deliverables if isinstance(tools.deliverables, list) else [],
            ),
        )
    else:
        payload = await resolve(
            tools.execution,
            args,
            tools.deliverables if isinstance(tools.deliverables, list) else [],
        )
    result = await tools.workspace.invoke(name, payload, timeout)
    await tools.execution.check()
    return dict(result)


def include_sources(content: Content, texts: list[str]) -> Content:
    """Keep cited source excerpts in an appendix without altering the synthesis."""
    rendered = "".join("".join(content.texts()).split())
    missing = [
        " ".join(text.split())
        for text in texts
        if "".join(text.split()) not in rendered
    ]
    if not missing:
        return content
    from packages.doc_render.schema import Section

    paragraphs = tuple(
        part[i : i + 4000] for part in missing for i in range(0, len(part), 4000)
    )
    sections = tuple(
        Section(heading="Sources", paragraphs=paragraphs[i : i + 50])
        for i in range(0, len(paragraphs), 50)
    )
    return Content(title=content.title, sections=content.sections + sections)


async def publish(
    tools: Any, args: dict[str, Any], timeout: float, *, append_sources: bool = False
) -> dict[str, Any]:
    from services.orchestrator.presentation_delivery import build_project_document
    from services.orchestrator.terminal_client import ProjectPublisher
    from services.orchestrator.file_results import project_deliverable_links
    from services.retrieval.project_identity import ProjectError

    deadline = time.monotonic() + timeout

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("document_tool_timeout")
        return seconds

    path = PurePosixPath(args["path"])
    path = path.with_suffix(path.suffix.lower())
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in args["path"].split("/"))
        or "\\" in args["path"]
        or path.suffix not in {".docx", ".xlsx", ".pptx", ".pdf"}
    ):
        raise ValueError("Invalid deliverable path or format")

    execution = tools.execution
    status, project = await execution.broker.public("GET", "")
    if status != 200:
        raise ProjectError(status, str(project.get("error", "not_found")))
    status, folders = await execution.broker.public("GET", "folders")
    if status != 200:
        raise ProjectError(status, str(folders.get("error", "not_found")))
    folder = next(
        (f for f in folders["items"] if f["id"] == project["deliverables_folder_id"]),
        None,
    )
    if folder is None:
        raise ProjectError(404, "not_found")

    source_sheets: list[list[dict[str, Any]]] = []
    source_texts: list[str] = []
    versions: list[str] = []
    for source in args["sources"]:
        resolved = await resolve(
            execution,
            source,
            tools.deliverables if isinstance(tools.deliverables, list) else [],
        )
        read = await tools.workspace.invoke("read_document", resolved, remaining())
        await execution.check()
        if read.get("error"):
            raise ValueError(str(read["error"]))
        if not read["coverage"]["complete"]:
            raise ValueError("Source coverage is incomplete; select a smaller range")
        texts = [
            segment["text"] for segment in read["segments"] if segment["text"].strip()
        ]
        if not texts:
            raise ValueError("Source selection contains no verifiable text")
        if resolved["extension"] == ".xlsx":
            source_sheets.append(read["segments"])
        source_texts.extend(texts)
        versions.append(resolved["version"])

    content = Content.model_validate(args["content"])
    if append_sources:
        content = include_sources(content, source_texts)
    document = await asyncio.to_thread(
        build_project_document,
        content,
        path.suffix,
        source_texts,
        max_bytes=16 * 1024 * 1024,
        timeout=remaining(),
    )
    if path.suffix == ".xlsx" and any(
        table.formulas for section in content.sections for table in section.tables
    ):
        from packages.doc_render.spreadsheet import validate_sources

        validate_sources(document.content, source_sheets)
    receipt = await ProjectPublisher(execution).publish(
        document,
        str(path),
        args["strategy"],
        list(dict.fromkeys(versions)),
        folder["revision"],
        str(uuid4()),
        remaining(),
    )
    links = project_deliverable_links(
        receipt, execution.broker.org, execution.broker.project
    )
    tools.deliverables.append(receipt)
    return dict(
        deliverable=receipt,
        text=links,
        file_version_ids=[receipt["file_version_id"]],
        read_input=dict(file_id=receipt["id"], version=receipt["file_version_id"]),
    )
