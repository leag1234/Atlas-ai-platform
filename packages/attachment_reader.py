"""Fixed sandbox reader: bounded source extraction and M10 evidence selection."""

import base64
import io
import json
from pathlib import Path
import posixpath
import re
import zipfile
from typing import Any
from xml.etree import ElementTree as ET

MAX_BYTES = 16 * 1024 * 1024
MAX_TEXT = 4 * 1024 * 1024
INLINE_BYTES = 7900
# Attached documents reach the model with about 30k tokens of context (owner decision 2026-10-15).
DOCUMENT_CONTEXT_BYTES = 120_000


def xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(archive.read(name))


def tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def office_parts(raw: bytes, extension: str) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if sum(info.file_size for info in archive.infolist()) > MAX_BYTES * 4:
            raise ValueError("expanded_size_limit")
        if extension == ".docx":
            body = xml(archive, "word/document.xml")
            return [
                "".join(node.text or "" for node in part.iter() if tag(node) == "t")
                for part in body.iter()
                if tag(part) == "p"
            ]
        base = "xl" if extension == ".xlsx" else "ppt"
        main = "workbook" if extension == ".xlsx" else "presentation"
        relationships = {
            node.attrib["Id"]: posixpath.normpath(
                posixpath.join(base, node.attrib["Target"])
            ).lstrip("/")
            for node in xml(archive, f"{base}/_rels/{main}.xml.rels")
            if node.attrib.get("TargetMode") != "External"
        }
        ordered = [
            node
            for node in xml(archive, f"{base}/{main}.xml").iter()
            if tag(node) == ("sheet" if extension == ".xlsx" else "sldId")
        ]
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = [
                "".join(n.text or "" for n in node.iter() if tag(n) == "t")
                for node in xml(archive, "xl/sharedStrings.xml")
            ]
        parts = []
        for index, entry in enumerate(ordered, 1):
            relation = next(v for k, v in entry.attrib.items() if k.endswith("}id"))
            part = xml(archive, relationships[relation])
            if extension in {".pptx", ".potx"}:
                text = "\n".join(n.text or "" for n in part.iter() if tag(n) == "t")
                notes = _speaker_notes(archive, relationships[relation])
                sections = [f"[Slide {index}]"]
                if text:
                    sections.append(text)
                if notes:
                    sections.append("[Speaker notes]\n" + notes)
                parts.append("\n".join(sections))
                continue
            rows = []
            for row in part.iter():
                if tag(row) != "row":
                    continue
                cells = []
                for cell in row:
                    value = "".join(
                        n.text or "" for n in cell.iter() if tag(n) in {"v", "t"}
                    )
                    if cell.attrib.get("t") == "s" and value:
                        value = shared[int(value)]
                    formula = next((n.text for n in cell if tag(n) == "f"), None)
                    if formula:
                        value = (
                            "="
                            + formula
                            + (" (cached: " + value + ")" if value else "")
                        )
                    cells.append(cell.attrib.get("r", "") + ": " + value)
                rows.append("\t".join(cells))
            parts.append("[Sheet: " + entry.attrib["name"] + "]\n" + "\n".join(rows))
        return parts


def _speaker_notes(archive: zipfile.ZipFile, slide_path: str) -> str:
    """Read the notes-slide text linked to one presentation slide, if present."""
    directory, filename = posixpath.split(slide_path)
    rels_path = posixpath.join(directory, "_rels", filename + ".rels")
    if rels_path not in archive.namelist():
        return ""
    relationships = xml(archive, rels_path)
    for relation in relationships:
        if not relation.attrib.get("Type", "").endswith("/notesSlide"):
            continue
        target = relation.attrib.get("Target")
        if not target or relation.attrib.get("TargetMode") == "External":
            continue
        notes_path = posixpath.normpath(posixpath.join(directory, target)).lstrip("/")
        if notes_path not in archive.namelist():
            continue
        notes = xml(archive, notes_path)
        return "\n".join(node.text or "" for node in notes.iter() if tag(node) == "t")
    return ""


def failure_reason(exc: Exception) -> str:
    # Third-party exception strings may include document bytes or sensitive paths.
    message = str(exc)
    if re.fullmatch(
        r"terminal_(?:http_[0-9]{3}|configuration_missing|unavailable|command_timeout|output_truncated)",
        message,
    ):
        return message + " (" + type(exc).__name__ + ")"
    reasons = {
        "FileNotFoundError": "File not found",
        "PermissionError": "File access denied",
        "UnicodeDecodeError": "Text is not valid UTF-8",
        "PdfReadError": "Document extraction failed: invalid PDF structure",
        "PdfStreamError": "Invalid PDF stream",
        "BadZipFile": "Invalid Office archive",
        "ParseError": "Invalid Office XML",
        "ModuleNotFoundError": "Required reader library is unavailable",
        "KeyError": "Required document structure is missing",
        "IndexError": "Invalid document reference",
        "StopIteration": "Required document relationship is missing",
    }
    return (
        reasons.get(type(exc).__name__, "Document extraction failed")
        + " ("
        + type(exc).__name__
        + ")"
    )


def extract_bytes(
    raw: bytes, extension: str, ocr_pages: dict[int, str] | None = None
) -> tuple[str, int, list[str]]:
    errors: list[str] = []
    pages = 0
    if not 0 < len(raw) <= MAX_BYTES:
        return "", pages, [f"Document bytes: {len(raw)}; allowed 1-{MAX_BYTES}"]
    parts: list[str] = []
    if extension == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            return (
                "",
                pages,
                ["Password-protected PDF: provide an unencrypted document"],
            )
        pages = len(reader.pages)
        if pages > 1000:
            return "", pages, [f"Document pages: {pages}; limit 1000"]
        characters = 0
        for index, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                errors.append(f"Page {index}: " + failure_reason(exc))
                continue
            if not text.strip() and ocr_pages and ocr_pages.get(index, "").strip():
                text = ocr_pages[index]
            if not text.strip():
                errors.append(
                    f"PDF page {index}/{pages} has no text layer; OCR is required (not available)"
                )
                continue
            characters += len(text)
            if characters > MAX_TEXT:
                errors.append(
                    f"Extracted text limit {MAX_TEXT} characters reached at page {index}; remaining pages were not read"
                )
                break
            parts.append(f"[Page {index}]\n{text}")
    elif extension == ".xls":
        errors.append("Legacy XLS computation is unavailable; export to XLSX or CSV")
    elif extension == ".ods":
        parts = odf_parts(raw, extension)
    elif extension in {".docx", ".xlsx", ".pptx", ".potx"}:
        parts = office_parts(raw, extension)
        if extension in {".pptx", ".potx"}:
            pages = len(parts)
    elif extension in {".txt", ".md", ".csv"}:
        parts = [raw.decode("utf-8-sig")]
    else:
        errors.append(
            "Unsupported document type; allowed PDF, DOCX, XLSX, PPTX, POTX, TXT, MD, CSV"
        )
    text = "\n\n".join(parts)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]
        errors.append(
            f"Extracted text limit {MAX_TEXT} characters reached; remaining text was not read"
        )
    if not text.strip() and not errors:
        errors.append("Document contains no readable text")
    return text, pages, errors


def read_attachment(path: Path, question: str) -> dict[str, object]:
    result: dict[str, object] = {
        "filename": path.name,
        "size": 0,
        "pages": 0,
        "text": "",
        "errors": [],
        "hierarchical_synthesis": False,
        "extracted_characters": 0,
    }
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        result["size"] = len(raw)
        text, pages, errors = extract_bytes(raw, path.suffix.lower())
        from packages.table_arithmetic import computed_tables

        tables = computed_tables(raw, path.suffix.lower())
        if tables:
            result["computed_tables"] = tables
        result.update(pages=pages, errors=errors, extracted_characters=len(text))
        if len(text.encode()) > INLINE_BYTES:
            from services.orchestrator.content import hierarchical_summary

            text, _, sections = hierarchical_summary(text, question)
            result.update(hierarchical_synthesis=True, sections_examined=sections)
        result["text"] = text
    except Exception as exc:
        result["errors"] = [failure_reason(exc)]
    return result


def main(path: str, question: str) -> None:
    target = Path(path).resolve()
    root = Path.cwd().resolve()
    if (
        any(part.startswith(".") for part in target.parts)
        or not (target.is_relative_to(root) or target.is_relative_to("/home/user"))
        or (
            target.is_relative_to("/home/user/requests")
            and not target.is_relative_to(root)
        )
    ):
        result: dict[str, object] = {
            "filename": Path(path).name,
            "text": "",
            "errors": ["Attachment path is outside the permitted upload area"],
        }
    else:
        result = read_attachment(target, question)
    errors = result.get("errors")
    if isinstance(errors, list) and len(errors) > 8:
        result["errors"] = errors[:8] + [
            f"{len(errors) - 8} additional page extraction failures; those pages were not read"
        ]
    # Base64 bounds JSON expansion for arbitrary control characters in source text.
    result["text_base64"] = base64.b64encode(str(result.pop("text")).encode()).decode()
    print(json.dumps(result, ensure_ascii=False))


ODF_EXTENSIONS = {".odt", ".ods", ".odp"}


def odf_parts(raw: bytes, extension: str) -> list[str]:
    """Text of an OpenDocument file in document order: paragraphs, table rows, slides."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        expanded = sum(info.file_size for info in archive.infolist())
        if expanded > MAX_BYTES * 4:
            raise ValueError(
                f"Expanded document bytes: {expanded}; limit {MAX_BYTES * 4}"
            )
        root = ET.fromstring(archive.read("content.xml"))
    ns = {
        "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
        "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
        "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    }

    def flat(node: ET.Element) -> str:
        return "".join(node.itertext()).strip()

    parts: list[str] = []
    if extension == ".ods":
        for sheet in root.iter("{%s}table" % ns["table"]):
            parts.append(f"[Sheet: {sheet.get('{%s}name' % ns['table'], '')}]")
            for row in sheet.iter("{%s}table-row" % ns["table"]):
                cells = [
                    flat(cell) for cell in row.iter("{%s}table-cell" % ns["table"])
                ]
                if any(cells):
                    parts.append(" | ".join(cells).rstrip(" |"))
    elif extension == ".odp":
        for index, page in enumerate(root.iter("{%s}page" % ns["draw"]), 1):
            parts.append(f"[Slide {index}]")
            for paragraph in page.iter():
                if paragraph.tag in (
                    "{%s}p" % ns["text"],
                    "{%s}h" % ns["text"],
                ) and flat(paragraph):
                    parts.append(flat(paragraph))
    else:
        for node in root.iter():
            if node.tag in ("{%s}p" % ns["text"], "{%s}h" % ns["text"]) and flat(node):
                parts.append(flat(node))
    return parts


# Structured Work extraction is deliberately independent of server state and I/O.
MAX_UNITS = 100_000
WORK_TEXT = 24_000
WORK_UNITS = 200


def validate_package(archive: zipfile.ZipFile) -> None:
    """Check the whole package before reading parts; never extract archive paths."""
    import stat
    from pathlib import PurePosixPath

    entries = archive.infolist()
    if len(entries) > 20_000:
        raise ValueError(f"Package entries: {len(entries)}; limit 20000")
    expanded = sum(entry.file_size for entry in entries)
    if expanded > MAX_BYTES * 4:
        raise ValueError(f"Expanded document bytes: {expanded}; limit {MAX_BYTES * 4}")
    names: set[str] = set()
    for entry in entries:
        name = entry.filename
        path = PurePosixPath(name)
        if (
            name in names
            or name.rstrip("/") != path.as_posix()
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or "\x00" in name
            or stat.S_IFMT(entry.external_attr >> 16)
            not in (0, stat.S_IFREG, stat.S_IFDIR)
            or entry.flag_bits & 1
        ):
            raise ValueError("Invalid package entry")
        names.add(name)
        lowered = name.lower()
        if any(
            part in lowered
            for part in ("vbaproject", "activex", "embeddings/", "externallinks/")
        ):
            raise ValueError("Active package content is unsupported")
        if name.endswith((".xml", ".rels")) or name == "[Content_Types].xml":
            content = archive.read(entry)
            # XML parts must be UTF-8: reject DTD/entity declarations, including
            # encodings which could disguise them from this bounded preflight.
            decoded = content.decode("utf-8-sig")
            if "\x00" in decoded or re.search(
                r"<!\s*(?:DOCTYPE|ENTITY)\b", decoded, re.I
            ):
                raise ValueError("DTD and entities are forbidden")
            node = ET.fromstring(content)
            if name == "[Content_Types].xml" and any(
                "macroenabled" in child.attrib.get("ContentType", "").lower()
                for child in node
            ):
                raise ValueError("Macros are forbidden")
            if name.endswith(".rels") and any(
                child.attrib.get("TargetMode") == "External"
                and not child.attrib.get("Type", "").endswith("/hyperlink")
                for child in node
            ):
                raise ValueError("External document relationships are forbidden")


def structured_units(
    raw: bytes, extension: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Stable physical locators, formula text and cached values without evaluation."""
    if not 0 < len(raw) <= MAX_BYTES:
        raise ValueError(f"Document bytes: {len(raw)}; allowed 1-{MAX_BYTES}")
    units: list[dict[str, Any]] = []
    warnings: list[str] = []
    characters = 0

    def add(locator: dict[str, Any], text: str, **extra: object) -> None:
        nonlocal characters
        characters += len(text) + len(str(extra.get("formula") or ""))
        if characters > MAX_TEXT or len(units) >= MAX_UNITS:
            raise ValueError(
                f"Extraction characters: {characters}; limit {MAX_TEXT}; units: {len(units) + 1}; limit {MAX_UNITS}"
            )
        units.append(dict(locator=locator, text=text, **extra))

    if extension == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF is unsupported")
        if len(reader.pages) > 1000:
            raise ValueError(f"Document pages: {len(reader.pages)}; limit 1000")
        for index, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if not text.strip():
                warnings.append(
                    f"Page {index} has no text layer; OCR was not performed"
                )
            add(dict(page=index), text)
    elif extension in {".docx", ".xlsx"}:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            validate_package(archive)
            warnings.append(
                "External hyperlinks were not followed; embedded images and layout were not extracted"
            )
            if extension == ".docx":
                document = xml(archive, "word/document.xml")
                paragraph = 0
                table = 0
                body = next(node for node in document if tag(node) == "body")
                for block in body:
                    if tag(block) == "p":
                        paragraph += 1
                        add(dict(paragraph=paragraph), _paragraph_text(block))
                    elif tag(block) == "tbl":
                        table += 1
                        for row_index, row in enumerate(
                            (n for n in block if tag(n) == "tr"), 1
                        ):
                            for column, cell in enumerate(
                                (n for n in row if tag(n) == "tc"), 1
                            ):
                                for cell_paragraph, node in enumerate(
                                    (n for n in cell if tag(n) == "p"), 1
                                ):
                                    add(
                                        dict(
                                            table=table,
                                            row=row_index,
                                            column=column,
                                            paragraph=cell_paragraph,
                                        ),
                                        _paragraph_text(node),
                                    )
                                if any(tag(n) == "tbl" for n in cell):
                                    warnings.append("Nested tables were not extracted")
                warnings.append(
                    "DOCX coverage is body paragraphs and top-level tables; headers, footers, notes and text boxes are excluded"
                )
            else:
                relationships = {
                    n.attrib["Id"]: n.attrib["Target"]
                    for n in xml(archive, "xl/_rels/workbook.xml.rels")
                    if n.attrib.get("TargetMode") != "External"
                }
                shared = []
                if "xl/sharedStrings.xml" in archive.namelist():
                    shared = [
                        "".join(n.text or "" for n in item.iter() if tag(n) == "t")
                        for item in xml(archive, "xl/sharedStrings.xml")
                    ]
                seen_sheets: set[str] = set()
                for sheet in xml(archive, "xl/workbook.xml").iter():
                    if tag(sheet) != "sheet":
                        continue
                    name = sheet.attrib["name"]
                    if name in seen_sheets:
                        raise ValueError("Duplicate worksheet name")
                    seen_sheets.add(name)
                    relation = next(
                        v for k, v in sheet.attrib.items() if k.endswith("}id")
                    )
                    destination = relationships[relation]
                    path = (
                        destination.lstrip("/")
                        if destination.startswith("/")
                        else posixpath.normpath(posixpath.join("xl", destination))
                    )
                    if not path.startswith("xl/worksheets/"):
                        raise ValueError("Invalid worksheet relationship")
                    seen_cells: set[str] = set()
                    for cell in xml(archive, path).iter():
                        if tag(cell) != "c":
                            continue
                        address = cell.attrib["r"]
                        cell_position(address)
                        if address in seen_cells:
                            raise ValueError("Duplicate worksheet cell")
                        seen_cells.add(address)
                        value = "".join(
                            n.text or "" for n in cell.iter() if tag(n) in {"v", "t"}
                        )
                        if cell.attrib.get("t") == "s":
                            index = int(value)
                            if not 0 <= index < len(shared):
                                raise ValueError("Invalid shared string index")
                            value = shared[index]
                        formula_node = next((n for n in cell if tag(n) == "f"), None)
                        formula = (
                            None if formula_node is None else formula_node.text or ""
                        )
                        cached = next((n.text for n in cell if tag(n) == "v"), None)
                        add(
                            dict(sheet=name, cell=address),
                            value,
                            formula=formula,
                            cached_value=cached if formula_node is not None else None,
                            value_type=cell.attrib.get("t", "n"),
                        )
                warnings.append(
                    "Formulas were not evaluated; cached values may be absent or stale; only stored cells are covered"
                )
    elif extension in {".txt", ".md", ".csv"}:
        for index, text in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
            add(dict(line=index), text)
    else:
        raise ValueError("Unsupported structured document type")
    return units, warnings


def _paragraph_text(node: ET.Element) -> str:
    def walk(part: ET.Element) -> str:
        kind = tag(part)
        if kind in {"drawing", "pict", "txbxContent", "del"}:
            return ""
        if kind == "t":
            return part.text or ""
        if kind == "tab":
            return "\t"
        if kind in {"br", "cr"}:
            return "\n"
        return "".join(walk(child) for child in part)

    return walk(node)


def cell_position(address: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", address)
    if match is None:
        raise ValueError("Invalid cell address")
    column = 0
    for letter in match[1]:
        column = column * 26 + ord(letter) - 64
    row = int(match[2])
    if column > 16384 or row > 1048576:
        raise ValueError("Cell outside worksheet limits")
    return row, column


def select_units(
    units: list[dict[str, Any]], selector: dict[str, Any]
) -> list[dict[str, Any]]:
    allowed = {
        "page_start",
        "page_end",
        "paragraph_start",
        "paragraph_end",
        "sheet",
        "range",
    }
    if set(selector) - allowed:
        raise ValueError("Unknown document selector")
    for kind in ("page", "paragraph"):
        start, end = (
            selector.get(kind + "_start", 1),
            selector.get(kind + "_end", MAX_UNITS),
        )
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            raise ValueError("Invalid document range")
    bounds = None
    if "range" in selector:
        if not isinstance(selector.get("sheet"), str):
            raise ValueError("Cell range requires a sheet")
        parts = str(selector["range"]).split(":")
        if len(parts) not in (1, 2):
            raise ValueError("Invalid cell range")
        bounds = (cell_position(parts[0]), cell_position(parts[-1]))
        if any(a > b for a, b in zip(*bounds)):
            raise ValueError("Reversed cell range")
    selected = []
    for unit in units:
        locator = unit["locator"]
        if not isinstance(locator, dict):
            raise ValueError("Invalid locator")
        keep = True
        for kind in ("page", "paragraph"):
            if kind + "_start" in selector or kind + "_end" in selector:
                keep &= (
                    kind in locator
                    and "table" not in locator
                    and int(str(selector.get(kind + "_start", 1)))
                    <= int(locator.get(kind, 0))
                    <= int(str(selector.get(kind + "_end", MAX_UNITS)))
                )
        if "sheet" in selector:
            keep &= locator.get("sheet") == selector["sheet"]
        if bounds is not None:
            position = (
                cell_position(str(locator["cell"])) if "cell" in locator else (0, 0)
            )
            keep &= all(a <= v <= b for a, v, b in zip(bounds[0], position, bounds[1]))
        if keep:
            selected.append(unit)
    if selector and not selected:
        raise ValueError("Document selector matched no stored units")
    return selected


def read_structured(
    raw: bytes,
    extension: str,
    selector: dict[str, Any] | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    import hashlib

    selector = selector or {}
    units, warnings = structured_units(raw, extension)
    selected = select_units(units, selector)
    binding = hashlib.sha256(
        raw + json.dumps(selector, sort_keys=True).encode()
    ).hexdigest()[:24]
    offset = 0
    if cursor is not None:
        prefix, separator, value = cursor.partition(":")
        if prefix != binding or not separator or not value.isdigit():
            raise ValueError("Invalid document cursor")
        offset = int(value)
        if offset >= len(selected):
            raise ValueError("Document cursor out of range")
    segments: list[dict[str, Any]] = []
    characters = 0
    for unit in selected[offset : offset + WORK_UNITS]:
        size = len(json.dumps(unit, ensure_ascii=False))
        if characters + size > WORK_TEXT:
            if not segments:
                raise ValueError(
                    f"Located unit characters: {size}; limit {WORK_TEXT}; select a smaller unit or convert to text"
                )
            break
        segments.append(unit)
        characters += size
    end = offset + len(segments)
    return dict(
        segments=segments,
        coverage=dict(
            total_units=len(units),
            selected_units=len(selected),
            returned_units=len(segments),
            offset=offset,
            complete=offset == 0 and end == len(selected),
            scope="selected stored text units",
            character_limit=WORK_TEXT,
            unit_limit=WORK_UNITS,
        ),
        warnings=warnings[:20],
        next_cursor=f"{binding}:{end}" if end < len(selected) else None,
    )
