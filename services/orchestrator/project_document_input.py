"""Model-friendly document input, normalized before the strict render boundary."""

import json
from pathlib import PurePosixPath
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from packages.doc_render.schema import Content, PresentationOutline, PresentationSlide


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    formulas: bool = False
    title: str | None = Field(default=None, min_length=1, max_length=4000)
    content: str | None = Field(default=None, min_length=1, max_length=220000)
    slides: list[PresentationSlide] | None = Field(
        default=None, min_length=1, max_length=30
    )


def markdown(text: str, title: str) -> Content:
    """A deliberately small text format: headings, paragraphs, lists and tables."""
    sections: list[dict[str, Any]] = []
    section: dict[str, Any] = dict(heading=title, paragraphs=[], bullets=[], tables=[])
    paragraph: list[str] = []
    lines = text.splitlines()

    def flush() -> None:
        if paragraph:
            section["paragraphs"].append(" ".join(paragraph))
            paragraph.clear()

    def finish() -> None:
        flush()
        if section["paragraphs"] or section["bullets"] or section["tables"]:
            sections.append(section.copy())

    def cells(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if re.search(r"!\[.*?\]\(", line) or line.startswith("```"):
            raise ValueError("Unsupported Markdown image or fenced block")
        heading = re.fullmatch(r"#{1,6}\s+(.+)", line)
        bullet = re.fullmatch(r"(?:[-+*]|\d+[.)])\s+(.+)", line)
        if heading:
            finish()
            section = dict(heading=heading[1], paragraphs=[], bullets=[], tables=[])
            if index == 0 and line.startswith("# "):
                title = heading[1]
        elif not line:
            flush()
        elif (
            "|" in line
            and index + 1 < len(lines)
            and all(
                re.fullmatch(r":?-{3,}:?", cell) for cell in cells(lines[index + 1])
            )
        ):
            flush()
            headers = cells(line)
            if len(cells(lines[index + 1])) != len(headers):
                raise ValueError("table_row_width")
            rows = []
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                # A deliberate empty Markdown cell is a visual placeholder;
                # keep strict Content text and rectangularity validation intact.
                rows.append([cell or "—" for cell in cells(lines[index])])
                index += 1
            section["tables"].append(dict(headers=headers, rows=rows))
            continue
        elif bullet:
            flush()
            section["bullets"].append(bullet[1])
        else:
            paragraph.append(line)
        index += 1
    finish()
    return Content.model_validate(dict(title=title, sections=sections))


def normalize(value: dict[str, Any]) -> dict[str, Any]:
    """Keep strict objects compatible; parse JSON once without coercing bad input."""
    if not isinstance(value, dict):
        raise ValueError("Document arguments must be an object")
    result = dict(value)
    content = result.get("content")
    if isinstance(content, str) and len(content) > 220000:
        raise ValueError("Document content exceeds 220000 characters")
    suffix = PurePosixPath(str(result.get("path", ""))).suffix.lower()
    if isinstance(content, str) and content.lstrip().startswith(("{", "[")):
        content = json.loads(content)
    if isinstance(content, dict):
        normalized_content = Content.model_validate(content).model_dump()
        formulas = result.pop("formulas", False)
        if not isinstance(formulas, bool):
            raise ValueError("Formulas must be a boolean")
        if formulas:
            if suffix != ".xlsx":
                raise ValueError("Formulas require XLSX")
            for section in normalized_content["sections"]:
                for table in section["tables"]:
                    table["formulas"] = True
        result["content"] = normalized_content
        return result
    simple = DocumentInput.model_validate(
        {
            key: result.pop(key)
            for key in ("title", "content", "slides", "formulas")
            if key in result
        }
    )
    suffix = PurePosixPath(str(result.get("path", ""))).suffix.lower()
    title = simple.title or PurePosixPath(str(result.get("path", ""))).stem
    if suffix == ".pptx" and simple.slides is not None and simple.content is None:
        normalized = PresentationOutline(
            title=title, slides=tuple(simple.slides)
        ).content()
    elif (
        suffix in {".docx", ".pdf", ".xlsx"}
        and simple.content is not None
        and simple.slides is None
    ):
        normalized = markdown(simple.content, title)
    else:
        raise ValueError("Use Markdown content for DOCX/PDF/XLSX or slides for PPTX")
    result["content"] = normalized.model_dump()
    if simple.formulas:
        if suffix != ".xlsx":
            raise ValueError("Formulas require XLSX")
        for section in result["content"]["sections"]:
            for table in section["tables"]:
                table["formulas"] = True
    return result
