"""Read announced sandbox files before generation using fixed repository code."""

import asyncio
import base64
from decimal import Decimal
from typing import TYPE_CHECKING, Awaitable, Callable

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.terminal_client import TerminalClient
from functools import lru_cache
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import time
import zipfile

OCR_PAGE_COST = Decimal(
    "0.0022"
)  # about 1000 input tokens at EUR 2.20 per million; output is free
OCR_PAGE_TOKEN_ESTIMATE = 1000


if TYPE_CHECKING:
    from services.orchestrator.documents import Attachment, Extracted


def _strip_markdown_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 2:
            return "\n".join(lines[1:-1]).strip()
    return value


def _has_image(page: object) -> bool:
    try:
        return bool(page.images)  # type: ignore[attr-defined]
    except Exception:
        return True


def _readable(text: str) -> bool:
    """OCR output counts only when it holds words; a blank page yields a notice or an empty fence."""
    words = re.findall(r"[^\W\d_]{2,}", text)
    return len(words) >= 3 and not re.fullmatch(
        r"(?is)\s*there is no (?:content|text)\b.*", text
    )


async def _scaleway_ocr(page: bytes, timeout: float) -> str:
    import os
    import aiohttp

    model = os.environ.get("ATLAS_OCR_MODEL", "")
    if not model:
        raise RuntimeError("ocr_disabled")
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "file",
                        "file": {
                            "file_data": "data:application/pdf;base64,"
                            + base64.b64encode(page).decode(),
                            "filename": "page.pdf",
                        },
                    }
                ],
            }
        ],
    }
    endpoint = os.environ["SCW_GENERATIVE_BASE_URL"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": "Bearer " + os.environ["SCW_GENERATIVE_API_KEY"]}
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
    ) as session:
        async with session.post(endpoint, json=payload, headers=headers) as response:
            if response.status >= 400:
                raise RuntimeError("ocr_http_error")
            body = await response.json()
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("ocr_response_invalid") from exc
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("ocr_response_invalid")
    return _strip_markdown_fence(text)


async def extract_attachment_with_ocr(
    attachment: "Attachment",
    *,
    deadline: float,
    remaining_cost: Decimal,
    ocr_client: Callable[[bytes, float], Awaitable[str]] = _scaleway_ocr,
) -> tuple["Extracted", Decimal, list[int]]:
    """Extract one uploaded attachment, filling image-only PDF pages when affordable."""
    from services.orchestrator.documents import Attachment, Extracted
    from packages.attachment_reader import MAX_BYTES, extract_bytes
    from pypdf import PdfReader, PdfWriter

    if not isinstance(
        attachment, Attachment
    ) or not attachment.filename.lower().endswith(".pdf"):
        from services.orchestrator.documents import extract_document

        return extract_document(attachment), Decimal(0), []
    try:
        raw = base64.b64decode(attachment.file_data.split(",", 1)[-1], validate=True)
        if not 0 < len(raw) <= MAX_BYTES:
            return (
                __import__(
                    "services.orchestrator.documents", fromlist=["extract_document"]
                ).extract_document(attachment),
                Decimal(0),
                [],
            )
        reader = PdfReader(io.BytesIO(raw), strict=True)
        missing = [
            i
            for i, page in enumerate(reader.pages, 1)
            if not (page.extract_text() or "").strip() and _has_image(page)
        ]
    except Exception:
        from services.orchestrator.documents import extract_document

        return extract_document(attachment), Decimal(0), []
    if (
        not os.environ.get("ATLAS_OCR_MODEL", "")
        or (
            ocr_client is _scaleway_ocr
            and not os.environ.get("SCW_GENERATIVE_BASE_URL")
        )
        or (
            ocr_client is _scaleway_ocr and not os.environ.get("SCW_GENERATIVE_API_KEY")
        )
        or not missing
    ):
        text, pages, errors = extract_bytes(raw, ".pdf")
        return (
            Extracted(attachment.filename, len(raw), pages, text, tuple(errors)),
            Decimal(0),
            [],
        )
    cap = max(0, int(os.environ.get("ATLAS_OCR_MAX_PAGES", "40") or "0"))
    allowed = min(len(missing), cap, int(remaining_cost / OCR_PAGE_COST))
    selected = missing[:allowed]
    skipped = len(missing) - allowed
    ocr: dict[int, str] = {}
    failures: list[str] = []
    semaphore = asyncio.Semaphore(4)

    async def one(index: int) -> None:
        nonlocal failures
        writer = PdfWriter()
        writer.add_page(reader.pages[index - 1])
        buffer = io.BytesIO()
        writer.write(buffer)
        try:
            async with semaphore:
                timeout = max(0.01, deadline - time.monotonic())
                if timeout <= 0.01:
                    raise TimeoutError("ocr_timeout")
                assert callable(ocr_client)
                value = await ocr_client(buffer.getvalue(), timeout)
            if _readable(_strip_markdown_fence(value)):
                ocr[index] = _strip_markdown_fence(value)
            else:
                failures.append(
                    f"PDF page {index}/{len(reader.pages)} has no text layer; OCR failed"
                )
        except TimeoutError:
            failures.append(
                f"PDF page {index}/{len(reader.pages)} has no text layer; OCR timeout"
            )
        except Exception:
            failures.append(
                f"PDF page {index}/{len(reader.pages)} has no text layer; OCR failed"
            )

    await asyncio.gather(*(one(index) for index in selected))
    text, pages, errors = extract_bytes(raw, ".pdf", ocr)
    errors = [
        error
        for error in errors
        if not any(f"PDF page {page}/" in error for page in ocr)
    ] + failures
    if skipped:
        reason = "page cap" if allowed == cap else "cost ceiling"
        errors.append(
            f"OCR stopped: {skipped} pages were not read; measured {len(missing) * OCR_PAGE_COST:.6f} EUR, limit {remaining_cost:.6f} EUR ({reason})"
        )
    return (
        Extracted(attachment.filename, len(raw), pages, text, tuple(errors)),
        OCR_PAGE_COST * len(selected),
        sorted(ocr),
    )


class FileTags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        path = attributes.get("url") or attributes.get("path")
        if tag == "file" and attributes.get("type") == "filesystem" and path:
            if path not in self.paths:
                self.paths.append(path)


def announced_paths(request: ChatRequest) -> list[str]:
    parser = FileTags()
    for message in request.messages:
        if message.role == "user":
            for block in re.findall(
                r"<attached_files>(.*?)</attached_files>", message.text, re.S
            ):
                parser.feed(block)
    return parser.paths


@lru_cache(maxsize=1)
def reader_archive() -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for package in ("packages", "services", "services/orchestrator"):
            archive.writestr(package + "/__init__.py", "")
        for name in (
            "packages/attachment_reader.py",
            "packages/evidence.py",
            "services/orchestrator/content.py",
        ):
            archive.writestr(name, Path(name).read_bytes())
    return base64.b64encode(buffer.getvalue()).decode()


def reader_command(path: str, question: str) -> str:
    # Only fixed code executes; filenames and the question are Python string data.
    code = (
        "import base64,sys,tempfile; from pathlib import Path; "
        "temp=tempfile.TemporaryDirectory(dir='.'); "
        "bundle=Path(temp.name)/'reader.zip'; "
        f"bundle.write_bytes(base64.b64decode({reader_archive()!r})); "
        "sys.path.insert(0,str(bundle)); "
        "from packages.attachment_reader import main; "
        f"main({path!r},{question!r}); temp.cleanup()"
    )
    return "python3 -c " + shlex.quote(code)


def decode_output(output: dict[str, object]) -> dict[str, object]:
    if output.get("error") or output.get("exit_code") != 0:
        return {
            "text": "",
            "errors": [str(output.get("error", "Attachment reader process failed"))],
        }
    chunks = output.get("output")
    if not isinstance(chunks, list):
        raise ValueError("invalid_attachment_reader_output")
    encoded = "".join(
        str(part.get("data", ""))
        for part in chunks
        if isinstance(part, dict) and part.get("type") == "output"
    )
    result = json.loads(encoded)
    if not isinstance(result, dict) or not isinstance(result.get("errors"), list):
        raise ValueError("invalid_attachment_reader_output")
    result["text"] = base64.b64decode(result.pop("text_base64"), validate=True).decode()
    return result


async def read_announced(
    request: ChatRequest,
    item: Interaction,
    terminal: TerminalClient | None,
    terminal_error: str = "",
    read_document_bytes: bool = False,
) -> list[dict[str, object]]:
    from packages.file_intent import requests_file_delivery
    from packages.language import user_prose

    from packages.attachment_reader import DOCUMENT_CONTEXT_BYTES
    from services.orchestrator.content import hierarchical_summary
    from services.orchestrator.documents import DocumentError

    deadline = time.monotonic() + request.timeout_seconds
    sources: list[dict[str, object]] = []
    if read_document_bytes:
        for attachment in request.documents:
            source: dict[str, object] = {
                "filename": attachment.filename,
                "text": "",
                "errors": [],
            }
            try:
                document, ocr_cost, ocr_pages = await extract_attachment_with_ocr(
                    attachment,
                    deadline=deadline,
                    remaining_cost=Decimal("0.30"),
                )
                text = document.text
                share = DOCUMENT_CONTEXT_BYTES // max(1, len(request.documents))
                hierarchical = len(text.encode()) > share
                if hierarchical:
                    text, _, _ = hierarchical_summary(
                        text,
                        user_prose(request.messages[-1].text),
                        budget=share,
                        coverage=True,
                    )
                source.update(
                    text=text,
                    errors=list(document.errors),
                    pages=document.pages,
                    size=document.size,
                    extracted_characters=len(document.text),
                    hierarchical_synthesis=hierarchical,
                    ocr_pages=ocr_pages,
                    ocr_cost_eur=float(ocr_cost),
                )
                item.cout_eur += float(ocr_cost)
            except DocumentError as exc:
                source["errors"] = [str(exc)]
            sources.append(source)
            record_source(item, source)
    paths = announced_paths(request)
    for index, path in enumerate(paths):
        announced_source: dict[str, object] = {
            "filename": PurePosixPath(path).name,
            "text": "",
            "errors": [],
        }
        if index + len(request.documents) >= 8:
            announced_source["errors"] = [
                "Attachment count exceeds the limit of 8; file was not read"
            ]
        elif terminal is None:
            announced_source["errors"] = [
                terminal_error or "Attachment sandbox is unavailable"
            ]
        elif not PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            announced_source["errors"] = ["Invalid attachment path"]
        else:
            try:
                announced_source.update(
                    decode_output(
                        await terminal.execute(
                            reader_command(path, user_prose(request.messages[-1].text)),
                            max(0.1, deadline - time.monotonic()),
                        )
                    )
                )
            except (ValueError, RuntimeError, OSError) as exc:
                from packages.attachment_reader import failure_reason

                announced_source["errors"] = [failure_reason(exc)]
        sources.append(announced_source)
        record_source(item, announced_source)
    if sources and not requests_file_delivery(request.messages[-1].text):
        item.task_type = "document"
    return sources


def record_source(item: Interaction, source: dict[str, object]) -> None:
    item.documents.append(
        {
            **{
                k: v
                for k, v in source.items()
                if k != "text" and isinstance(v, (str, int, bool))
            },
            "error": error_text(source),
        }
    )


def error_text(source: dict[str, object]) -> str:
    errors = source.get("errors")
    return "; ".join(str(e) for e in errors) if isinstance(errors, list) else ""


def failure_notice(sources: list[dict[str, object]], lang: str) -> str:
    """Return a short reader-facing note; extraction diagnostics remain in logs."""
    notes = [
        _failure_note(str(source.get("filename", "")), source.get("errors"), lang)
        for source in sources
        if source.get("errors")
    ]
    ocr = [_ocr_note(source, lang) for source in sources if source.get("ocr_pages")]
    return "\n\n".join(
        [*(note for note in notes if note), *(note for note in ocr if note)]
    )


def _ocr_note(source: dict[str, object], lang: str) -> str:
    pages = source.get("ocr_pages")
    if not isinstance(pages, list) or not all(isinstance(page, int) for page in pages):
        return ""
    locale = lang if lang in {"fr", "en", "de", "es", "it"} else "en"
    page_list = _page_list(pages, locale)
    single = len(pages) == 1
    templates = (
        {
            "fr": "{label} {pages} est une image scannée : son texte a été reconnu automatiquement et peut contenir des erreurs.",
            "en": "{label} {pages} is a scanned image: its text was recognized automatically and may contain errors.",
            "de": "{label} {pages} ist ein gescanntes Bild: ihr Text wurde automatisch erkannt und kann Fehler enthalten.",
            "es": "{label} {pages} es una imagen escaneada: su texto se reconoció automáticamente y puede contener errores.",
            "it": "{label} {pages} è un'immagine scansionata: il testo è stato riconosciuto automaticamente e può contenere errori.",
        }
        if single
        else {
            "fr": "{label} {pages} sont des images scannées : leur texte a été reconnu automatiquement et peut contenir des erreurs.",
            "en": "{label} {pages} are scanned images: their text was recognized automatically and may contain errors.",
            "de": "{label} {pages} sind gescannte Bilder: ihr Text wurde automatisch erkannt und kann Fehler enthalten.",
            "es": "{label} {pages} son imágenes escaneadas: su texto se reconoció automáticamente y puede contener errores.",
            "it": "{label} {pages} sono immagini scansionate: il testo è stato riconosciuto automaticamente e può contenere errori.",
        }
    )
    label = {
        "fr": "La page" if len(pages) == 1 else "Les pages",
        "en": "Page" if len(pages) == 1 else "Pages",
        "de": "Die Seite" if len(pages) == 1 else "Die Seiten",
        "es": "La página" if len(pages) == 1 else "Las páginas",
        "it": "La pagina" if len(pages) == 1 else "Le pagine",
    }[locale]
    return templates[locale].format(label=label, pages=page_list)


def _failure_note(filename: str, errors: object, lang: str) -> str:
    values = [str(value) for value in errors] if isinstance(errors, list) else []
    pages = _unreadable_pages(values)
    locale = lang if lang in {"fr", "en", "de", "es", "it"} else "en"
    if pages:
        page_list = _page_list(pages, locale)
        singular = len(pages) == 1
        templates = {
            "fr": "{page} {pages} de {filename} {verb} une image scannée : son texte n'a pas pu être lu.",
            "en": "{page} {pages} of {filename} {verb} a scanned image, so its text could not be read.",
            "de": "{page} {pages} von {filename} {verb} gescannte Bilder; ihr Text konnte nicht gelesen werden.",
            "es": "{page} {pages} de {filename} {verb} imágenes escaneadas: no se pudo leer su texto.",
            "it": "{page} {pages} di {filename} {verb}: non è stato possibile leggerne il testo.",
        }
        words = {
            "fr": ("La page", "Les pages", "est", "sont"),
            "en": ("Page", "Pages", "is", "are"),
            "de": ("Die Seite", "Die Seiten", "ist ein", "sind"),
            "es": ("La página", "Las páginas", "es una", "son"),
            "it": (
                "La pagina",
                "Le pagine",
                "è un'immagine scansionata",
                "sono immagini scansionate",
            ),
        }[locale]
        return templates[locale].format(
            page=words[0] if singular else words[1],
            pages=page_list,
            verb=words[2] if singular else words[3],
            filename=filename,
        )
    templates = {
        "fr": "Le document {filename} n'a pas pu être lu{reason}.",
        "en": "The document {filename} could not be read{reason}.",
        "de": "Das Dokument {filename} konnte nicht gelesen werden{reason}.",
        "es": "No se pudo leer el documento {filename}{reason}.",
        "it": "Non è stato possibile leggere il documento {filename}{reason}.",
    }
    return templates[locale].format(filename=filename, reason=_reason(values, locale))


# D5: the reader learns what failed, in plain words, with the short technical code.
_CAUSES = {
    "terminal_http_": {
        "fr": "l'environnement de lecture a refusé la requête",
        "en": "the reading environment refused the request",
        "de": "die Leseumgebung hat die Anfrage abgelehnt",
        "es": "el entorno de lectura rechazó la solicitud",
        "it": "l'ambiente di lettura ha rifiutato la richiesta",
    },
    "terminal_": {
        "fr": "l'environnement de lecture est indisponible",
        "en": "the reading environment is unavailable",
        "de": "die Leseumgebung ist nicht verfügbar",
        "es": "el entorno de lectura no está disponible",
        "it": "l'ambiente di lettura non è disponibile",
    },
    "Pdf": {
        "fr": "le fichier PDF est endommagé ou mal formé",
        "en": "the PDF file is damaged or malformed",
        "de": "die PDF-Datei ist beschädigt oder fehlerhaft",
        "es": "el archivo PDF está dañado o mal formado",
        "it": "il file PDF è danneggiato o malformato",
    },
    "BadZipFile": {
        "fr": "le fichier Office est endommagé",
        "en": "the Office file is damaged",
        "de": "die Office-Datei ist beschädigt",
        "es": "el archivo de Office está dañado",
        "it": "il file Office è danneggiato",
    },
}
_FALLBACK = {
    "fr": "erreur de lecture",
    "en": "reading error",
    "de": "Lesefehler",
    "es": "error de lectura",
    "it": "errore di lettura",
}


_PASSWORD = {
    "fr": "il est protégé par un mot de passe ; envoyez une version non chiffrée",
    "en": "it is password-protected; send an unencrypted copy",
    "de": "es ist passwortgeschützt; senden Sie eine unverschlüsselte Kopie",
    "es": "está protegido con contraseña; envíe una copia sin cifrar",
    "it": "è protetto da password; invia una copia non cifrata",
}


def _reason(errors: list[str], lang: str) -> str:
    text = " ".join(errors)
    if re.search(r"password-protected|encrypted pdf", text, re.I):
        return (" : " if lang == "fr" else ": ") + _PASSWORD[lang]
    match = re.search(
        r"terminal_(?:[a-z]+_)*(?:[0-9]{3}|[a-z]+)|\b[A-Z][A-Za-z]*(?:Error|File)\b",
        text,
    )
    if not match:
        return ""
    code = match.group(0)
    phrase = next(
        (labels[lang] for key, labels in _CAUSES.items() if code.startswith(key)),
        _FALLBACK[lang],
    )
    return f" : {phrase} ({code})" if lang == "fr" else f": {phrase} ({code})"


def _unreadable_pages(errors: list[str]) -> list[int]:
    pages = {
        int(match.group(1))
        for error in errors
        for match in [re.search(r"PDF page (\d+)/\d+ has no text layer", error)]
        if match
    }
    return sorted(pages)


def _page_list(pages: list[int], lang: str) -> str:
    groups: list[str] = []
    start = previous = pages[0]
    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        groups.append(str(start) if start == previous else f"{start}–{previous}")
        start = previous = page
    groups.append(str(start) if start == previous else f"{start}–{previous}")
    if len(groups) == 1:
        return groups[0]
    conjunction = {
        "fr": " et ",
        "en": " and ",
        "de": " und ",
        "es": " y ",
        "it": " e ",
    }[lang]
    return ", ".join(groups[:-1]) + conjunction + groups[-1]


def document_status(
    pages: int, failed_pages: int, lang: str, ocr_pages: int = 0
) -> str:
    """Describe completed document reading without exposing parser diagnostics."""
    locale = lang if lang in {"fr", "en", "de", "es", "it"} else "en"
    read = max(0, pages - failed_pages)
    if failed_pages:
        templates = {
            "fr": "Document lu : {read} pages sur {pages}",
            "en": "Document read: {read} pages out of {pages}",
            "de": "Dokument gelesen: {read} von {pages} Seiten",
            "es": "Documento leído: {read} páginas de {pages}",
            "it": "Documento letto: {read} pagine su {pages}",
        }
        result = templates[locale].format(read=read, pages=pages)
        return result + _ocr_status(ocr_pages, locale)
    templates = {
        "fr": "Document lu : {pages} pages",
        "en": "Document read: {pages} pages",
        "de": "Dokument gelesen: {pages} Seiten",
        "es": "Documento leído: {pages} páginas",
        "it": "Documento letto: {pages} pagine",
    }
    return templates[locale].format(pages=pages) + _ocr_status(ocr_pages, locale)


def _ocr_status(count: int, lang: str) -> str:
    if not count:
        return ""
    return {
        "fr": f" dont {count} par OCR",
        "en": f", including {count} by OCR",
        "de": f", davon {count} per OCR",
        "es": f", incluidas {count} por OCR",
        "it": f", di cui {count} tramite OCR",
    }[lang]


def reading_progress(sources: list[dict[str, object]]) -> tuple[int, int, int]:
    """Return total and unreadable PDF pages from extraction metadata."""
    pages = 0
    failed: set[int] = set()
    for source in sources:
        count = source.get("pages")
        if isinstance(count, int):
            pages += count
        errors = source.get("errors")
        if isinstance(errors, list):
            failed.update(_unreadable_pages([str(error) for error in errors]))
    ocr = sum(
        len(value)
        for source in sources
        if isinstance((value := source.get("ocr_pages")), list)
    )
    return pages, len(failed), ocr


def reduced_document_note(lang: str) -> str:
    """Plain note for a document too long to send whole; replaces the raw activity label."""
    locale = lang if lang in {"fr", "en", "de", "es", "it"} else "en"
    return {
        "fr": "Ce document est long : la réponse s'appuie sur des extraits couvrant toutes ses pages.",
        "en": "This document is long: the answer relies on excerpts covering all its pages.",
        "de": "Dieses Dokument ist lang: die Antwort stützt sich auf Auszüge aus allen Seiten.",
        "es": "Este documento es largo: la respuesta se basa en extractos de todas sus páginas.",
        "it": "Questo documento è lungo: la risposta si basa su estratti di tutte le pagine.",
    }[locale]
