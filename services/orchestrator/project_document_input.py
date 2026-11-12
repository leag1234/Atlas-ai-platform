"""Model-friendly document input, normalized before the strict render boundary."""

from collections.abc import Iterator
from contextlib import contextmanager
from difflib import get_close_matches
import json
from pathlib import PurePosixPath
import re
from typing import Any, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.doc_render.schema import Content, PresentationOutline, PresentationSlide


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    formulas: bool = False
    title: str | None = Field(default=None, min_length=1, max_length=4000)
    content: str | None = Field(default=None, min_length=1, max_length=220000)
    slides: list[PresentationSlide] | None = Field(
        default=None, min_length=1, max_length=30
    )


# What a named rule of the content contract asks for.
RULES = {
    "table_row_width": "every row needs exactly as many cells as the table has headers",
    "table_headers_required": "a table needs at least one column name",
    "section_body_required": "a section needs at least one paragraph, bullet, table or image",
    "blank_or_control_text": "a text is blank or holds a tab or a line break; write one line of visible text per item",
    "content_size_limit": "the texts exceed 200000 characters in total",
}


def nested(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for part in get_args(annotation):
        found = nested(part)
        if found is not None:
            return found
    return None


def fields_at(model: type[BaseModel] | None, path: tuple[int | str, ...]) -> list[str]:
    """The field names accepted at a path of the validated object."""
    for part in path:
        if model is None:
            return []
        if isinstance(part, str):
            field = model.model_fields.get(part)
            model = nested(field.annotation) if field else None
    return list(model.model_fields) if model else []


def explain(error: ValidationError, model: type[BaseModel], root: str = "") -> str:
    """A refusal the model can act on: each field path and its rule, nothing internal."""
    reported = error.errors(include_url=False, include_input=False)
    # An item refused for its own reason also leaves its list short: only the
    # item's reason is something to correct.
    problems = [
        problem
        for problem in reported
        if problem["type"] != "too_short"
        or not any(
            len(other["loc"]) > len(problem["loc"])
            and other["loc"][: len(problem["loc"])] == problem["loc"]
            for other in reported
        )
    ]
    lines: list[str] = []
    for problem in problems[:5]:
        path = problem["loc"]
        where = root
        for part in path:
            if isinstance(part, int):
                where += f"[{part}]"
            else:
                where += ("." if where else "") + part
        kind = problem["type"]
        if kind == "extra_forbidden":
            allowed = fields_at(model, path[:-1])
            close = get_close_matches(str(path[-1]), allowed, 1)
            rule = "unknown field"
            if close:
                rule += f' (did you mean "{close[0]}"?)'
            if allowed:
                rule += "; fields allowed here: " + ", ".join(allowed)
        elif kind == "missing":
            rule = "this required field is missing"
        elif kind in ("model_type", "model_attributes_type"):
            allowed = fields_at(model, path)
            rule = "an object is expected here"
            if allowed:
                rule += " with the fields: " + ", ".join(allowed)
        elif kind == "value_error":
            code = str(problem["msg"]).removeprefix("Value error, ")
            rule = RULES.get(code, code)
        elif kind in ("too_long", "too_short"):
            context = problem.get("ctx", {})
            bound = "at most" if kind == "too_long" else "at least"
            limit = context.get("max_length", context.get("min_length"))
            rule = f"{bound} {limit} items here, {context.get('actual_length')} given"
        else:
            rule = str(problem["msg"])
        lines.append(f"{where or 'arguments'}: {rule}")
    if len(problems) > 5:
        lines.append(f"and {len(problems) - 5} more")
    return (
        "Invalid document arguments. "
        + "; ".join(lines)
        + ". Correct these fields and call the tool again."
    )


@contextmanager
def explained(model: type[BaseModel], root: str = "") -> Iterator[None]:
    """Turn a validation report into the short refusal of explain()."""
    try:
        yield
    except ValidationError as error:
        raise ValueError(explain(error, model, root)) from None


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
        # One outer pipe on each side is the table border; an empty last cell
        # written "||" stays a cell.
        line = line.strip()
        line = line[1:] if line.startswith("|") else line
        line = line[:-1] if line.endswith("|") else line
        return [cell.strip() for cell in line.split("|")]

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
                # An empty Markdown cell stays an empty cell in every format (a
                # value to fill in later); a missing cell is still a width error.
                row = cells(lines[index])
                while len(row) > len(headers) and not row[-1]:
                    # A doubled closing pipe adds no column.
                    row.pop()
                rows.append(row)
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
    with explained(Content, "content"):
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
        try:
            content = json.loads(content)
        except json.JSONDecodeError as error:
            raise ValueError(
                "content starts like JSON but is not valid JSON (line "
                f"{error.lineno}, column {error.colno}: {error.msg}). Send valid "
                "JSON, or Markdown text."
            ) from None
    if isinstance(content, dict):
        with explained(Content, "content"):
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
    with explained(DocumentInput):
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
        with explained(Content, "content"):
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
