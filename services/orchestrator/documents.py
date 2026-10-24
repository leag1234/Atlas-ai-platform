"""Bounded extraction of complete attachments; no URL or corpus lookup."""

import base64
import binascii
from dataclasses import dataclass, field
import io
from pathlib import Path
import zipfile
from xml.etree.ElementTree import ParseError
from typing import Any

from docx import Document
from pydantic import BaseModel, ConfigDict, Field
from pypdf.errors import PdfReadError

MAX_BYTES = 16 * 1024 * 1024
MAX_TEXT = 4 * 1024 * 1024


class DocumentError(ValueError):
    def __init__(
        self, message: str, *, size: int = 0, pages: int = 0, characters: int = 0
    ) -> None:
        super().__init__(message)
        self.size, self.pages, self.characters = size, pages, characters


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    filename: str = Field(min_length=1, max_length=255, pattern=r"^[^/\\\x00-\x1f]+$")
    file_data: str = Field(min_length=1, max_length=22370000, repr=False)
    file_id: str | None = Field(
        default=None,
        pattern=r"^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$",
        exclude_if=lambda value: value is None,
    )


@dataclass(frozen=True)
class Extracted:
    filename: str
    size: int
    pages: int
    text: str
    errors: tuple[str, ...] = ()
    computed_tables: list[dict[str, Any]] = field(default_factory=list)


def extract_document(attachment: Attachment) -> Extracted:
    encoded = attachment.file_data
    if encoded.startswith("data:"):
        prefix, sep, encoded = encoded.partition(",")
        if not sep or not prefix.endswith(";base64"):
            raise DocumentError("Document must contain base64 bytes, not a URL")
    if len(encoded) > (MAX_BYTES + 2) // 3 * 4:
        raise DocumentError(
            f"Document encoded bytes: {len(encoded)}; limit {(MAX_BYTES + 2) // 3 * 4}"
        )
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise DocumentError("Document contains invalid base64 bytes") from None
    if not 0 < len(raw) <= MAX_BYTES:
        raise DocumentError(f"Document bytes: {len(raw)}; allowed 1–{MAX_BYTES}")
    extension = Path(attachment.filename).suffix.lower()
    errors: list[str] = []
    pages = characters = 0
    try:
        if extension == ".xls":
            raise DocumentError(
                "Legacy XLS computation is unavailable; export to XLSX or CSV"
            )
        elif extension == ".pdf":
            from packages.attachment_reader import extract_bytes

            text, pages, errors = extract_bytes(raw, extension)
            characters = len(text)
            if not text.strip():
                raise DocumentError("; ".join(errors))
        elif extension == ".docx":
            from packages.attachment_reader import validate_package

            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                validate_package(archive)
                expanded = sum(info.file_size for info in archive.infolist())
                if expanded > MAX_BYTES * 4:
                    raise DocumentError(
                        f"Expanded document bytes: {expanded}; limit {MAX_BYTES * 4}"
                    )
            document = Document(io.BytesIO(raw))
            # Walk paragraphs and tables in document order, retaining all cell text.
            text = "\n".join(
                "".join(element.text or "" for element in node.xpath(".//w:t"))
                for node in document.element.body
                if node.tag.endswith(("}p", "}tbl"))
            )
        elif extension in {".xlsx", ".pptx", ".potx"}:
            from packages.attachment_reader import office_parts

            parts = office_parts(raw, extension)
            text = "\n\n".join(parts)
            if extension in {".pptx", ".potx"}:
                pages = len(parts)
        elif extension in {".txt", ".md", ".csv"}:
            text = raw.decode("utf-8-sig")
        elif extension in {".odt", ".ods", ".odp"}:
            from packages.attachment_reader import odf_parts

            text = "\n".join(odf_parts(raw, extension))
        else:
            raise DocumentError(
                "Unsupported document type; allowed PDF, DOCX, XLSX, PPTX, POTX, ODT, ODS, ODP, TXT, MD, CSV"
            )
    except DocumentError as exc:
        exc.size, exc.pages, exc.characters = len(raw), pages, characters
        raise
    except (
        ValueError,
        OSError,
        KeyError,
        IndexError,
        StopIteration,
        zipfile.BadZipFile,
        PdfReadError,
        ParseError,
    ) as exc:
        from packages.attachment_reader import failure_reason

        raise DocumentError(
            failure_reason(exc),
            size=len(raw),
            pages=pages,
            characters=characters,
        ) from None
    if not text.strip():
        raise DocumentError(
            "Document contains 0 extracted characters; minimum 1 required",
            size=len(raw),
            pages=pages,
        )
    if len(text) > MAX_TEXT:
        raise DocumentError(f"Extracted characters: {len(text)}; limit {MAX_TEXT}")
    from packages.table_arithmetic import computed_tables

    return Extracted(
        attachment.filename,
        len(raw),
        pages,
        text,
        tuple(errors),
        computed_tables(raw, extension),
    )
