"""REQ-PROJ-01/03: bounded, located ingestion using the shared document readers."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
from typing import Any

from packages.attachment_reader import (
    MAX_BYTES,
    MAX_TEXT,
    failure_reason,
    office_parts,
    structured_units,
    validate_package,
)
from packages.table_arithmetic import _sheets, computed_tables
from services.retrieval.extract import HTMLText, split_located_text

MAX_CHUNKS = 2048
SECONDS = 30


def coverage(
    unit: str, total: int | None, selected: int, warnings: list[str]
) -> dict[str, Any]:
    return dict(
        unit=unit,
        total_units=total,
        selected_units=selected,
        complete=total is not None and selected == total and not warnings,
        warnings=warnings[:20]
        + (
            [f"{len(warnings) - 20} additional extraction warnings"]
            if len(warnings) > 20
            else []
        ),
    )


def column_name(number: int) -> str:
    result = ""
    while number:
        number, digit = divmod(number - 1, 26)
        result = chr(65 + digit) + result
    return result


def extract(content: bytes, extension: str) -> dict[str, Any]:
    """No URLs, macros, formula execution, OCR dispatch or canonical-store access."""
    unit = {
        ".pdf": "pages",
        ".pptx": "slides",
        ".docx": "paragraphs",
        ".xlsx": "cells",
        ".csv": "cells",
    }.get(extension, "characters")
    if not 0 < len(content) <= MAX_BYTES:
        raise ValueError(f"Document bytes: {len(content)}; allowed 1-{MAX_BYTES}")
    warnings: list[str] = []
    located: list[dict[str, Any]] = []
    total = selected = 0
    if extension in {".pdf", ".docx", ".xlsx"}:
        units, warnings = structured_units(content, extension)
        total = len(units)
        for value in units:
            locator = value["locator"]
            if "page" in locator:
                loc = dict(kind="page", page=locator["page"])
            elif "table" in locator:
                loc = dict(
                    kind="table_cell",
                    **{k: locator[k] for k in ("table", "row", "column")},
                )
            elif "paragraph" in locator:
                loc = dict(kind="paragraph", paragraph=locator["paragraph"])
            else:
                loc = dict(
                    kind="sheet_range", sheet=locator["sheet"], range=locator["cell"]
                )
            segment = dict(locator=loc, text=value["text"])
            if "formula" in value:
                segment.update(
                    formula=value["formula"], cached_value=value["cached_value"]
                )
                if value["formula"] is not None:
                    segment["text"] = (
                        "="
                        + value["formula"]
                        + " (cached: "
                        + (value["cached_value"] or "unavailable")
                        + ")"
                    )
            if segment["text"].strip():
                located.append(segment)
                selected += 1
    elif extension == ".pptx":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            validate_package(archive)
        parts = office_parts(content, extension)
        total = len(parts)
        if total > 1000:
            raise ValueError(f"Document slides: {total}; limit 1000")
        for index, text in enumerate(parts, 1):
            body = text.partition("\n")[2]
            if body.strip():
                located.append(dict(locator=dict(kind="slide", slide=index), text=body))
                selected += 1
            else:
                warnings.append(
                    f"Slide {index} has no stored text; images were not extracted"
                )
        warnings.append(
            "Slide images and layout were not extracted; speaker notes remain with their slide"
        )
    elif extension == ".csv":
        for sheet, rows in _sheets(content, extension).items():
            for row, cells in enumerate(rows, 1):
                total += len(cells)
                for column, (text, unsafe) in enumerate(cells, 1):
                    if text.strip():
                        located.append(
                            dict(
                                locator=dict(
                                    kind="sheet_range",
                                    sheet=sheet,
                                    range=f"{column_name(column)}{row}",
                                ),
                                text=text,
                            )
                        )
                        selected += 1
        warnings.append("CSV cells are stored text; formulas were not evaluated")
    elif extension in {".txt", ".md", ".html", ".htm"}:
        text = content.decode("utf-8-sig")
        if extension in {".html", ".htm"}:
            parser = HTMLText()
            parser.feed(text)
            parser.close()
            text = "".join(parser.parts)
            warnings.append(
                "Offsets refer to extracted HTML text; scripts, styles and templates are excluded"
            )
        total = len(text)
        if total > MAX_TEXT:
            warnings.append(
                f"Extracted characters: {total}; limit {MAX_TEXT}; remaining text omitted"
            )
            text = text[:MAX_TEXT]
        selected = len(text)
        located.append(
            dict(locator=dict(kind="text", start=0, end=len(text)), text=text)
        )
    else:
        raise ValueError("Unsupported project document format")
    if sum(len(s["text"]) for s in located) > MAX_TEXT:
        raise ValueError(f"Extracted characters exceed limit {MAX_TEXT}")
    if extension in {".xlsx", ".csv"}:
        for aggregate in computed_tables(content, extension):
            aggregate_sheet = str(aggregate.get("sheet", ""))
            addresses = [
                s["locator"]["range"]
                for s in located
                if s["locator"].get("sheet") == aggregate_sheet
            ]
            if aggregate["status"] != "computed" or not addresses:
                warnings.append(
                    "Deterministic aggregates unavailable: "
                    + str(aggregate.get("reason", "unsupported table"))
                )
                continue
            from packages.attachment_reader import cell_position

            positions = [cell_position(c) for c in addresses]
            last_row = max(p[0] for p in positions)
            last_column = max(p[1] for p in positions)
            located.append(
                dict(
                    locator=dict(
                        kind="sheet_range",
                        sheet=aggregate_sheet,
                        range=f"A1:{column_name(last_column)}{last_row}",
                    ),
                    text="computed_tables: "
                    + json.dumps(aggregate, ensure_ascii=False),
                )
            )
    chunks = []
    for segment in located:
        for start, end, part in split_located_text(segment["text"]):
            locator = dict(segment["locator"])
            if locator["kind"] == "text":
                locator.update(start=start, end=end)
            chunks.append(dict(segment, text=part, locator=locator))
            if len(chunks) > MAX_CHUNKS:
                raise ValueError(f"Extracted chunks: {len(chunks)}; limit {MAX_CHUNKS}")
    if not chunks:
        warnings.append("Document contains no indexable text")
    return dict(
        segments=chunks,
        coverage=coverage(unit, total, selected, warnings),
        next_cursor=None,
    )


def bounded_extract(content: bytes, extension: str) -> dict[str, Any]:
    """Parser process receives only bytes and format; stderr never exposes source text."""
    if len(content) > MAX_BYTES:
        raise ValueError(f"Document bytes: {len(content)}; limit {MAX_BYTES}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", __name__, extension],
            input=content,
            capture_output=True,
            cwd=Path(__file__).resolve().parents[2],
            env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(
            f"Extraction elapsed seconds reached limit {SECONDS}"
        ) from None
    if result.returncode:
        raise ValueError("Extraction process failed or exceeded its resource limit")
    value = json.loads(result.stdout)
    if "error" in value:
        raise ValueError(value["error"])
    return dict(value)


def main() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    def deny_network(event: str, arguments: tuple[object, ...]) -> None:
        if (
            event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn"))
            or event == "os.system"
        ):
            raise PermissionError(
                "Document reader network and command execution are forbidden"
            )

    sys.addaudithook(deny_network)
    try:
        result = extract(sys.stdin.buffer.read(MAX_BYTES + 1), sys.argv[1])
    except Exception as exc:
        # Only our/shared limit and safety errors may carry literal diagnostics.
        safe = type(exc) is ValueError and str(exc).startswith(
            (
                "Document ",
                "Encrypted PDF",
                "Extraction characters",
                "Extracted ",
                "Package entries:",
                "Expanded document bytes:",
                "Active package",
                "Invalid package",
                "DTD and entities",
                "Macros are forbidden",
                "External document",
                "Unsupported project",
            )
        )
        result = dict(error=str(exc) if safe else failure_reason(exc))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
