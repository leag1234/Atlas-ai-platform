"""Six live public HTTP probes; fixture bytes and answers stay out of Git."""

import base64
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from docx import Document
import zipfile
from xml.etree import ElementTree as ET
from typing import Any
from collections.abc import Callable
from pypdf import PdfReader
from packages.profiles import PROFILES

OWNER_PDF = Path("/opt/atlas-src/private/fixtures/owner-174p.pdf")


def attachment(name: str, raw: bytes) -> dict[str, Any]:
    return {
        "type": "file",
        "file": {"filename": name, "file_data": base64.b64encode(raw).decode()},
    }


def cases() -> dict[str, tuple[str, list[dict[str, Any]]]]:
    document = Document()
    document.add_paragraph("Les resultat sont satisfesant. Le projet avance bien.")
    doc = io.BytesIO()
    document.save(doc)
    # Reuse the delivered public spreadsheet input; never inspect private cases.
    import gzip

    archive = json.loads(
        gzip.decompress(Path("tests/cassettes/m24.json.gz").read_bytes())
    )
    spreadsheet = None
    for case in archive.values():
        for message in case["request"]["messages"]:
            if isinstance(message["content"], list):
                for part in message["content"]:
                    if part.get("type") == "file" and part["file"]["filename"].endswith(
                        ".xlsx"
                    ):
                        spreadsheet = part
                        break
    if spreadsheet is None:
        raise ValueError("public_spreadsheet_fixture_missing")
    return {
        "presentation": (
            "fais-moi une présentation de /e/OS et Murena, pptx téléchargeable et pdf",
            [],
        ),
        "owner_pdf": (
            "fais moi un résumé structuré",
            [],
        ),
        "docx": (
            "Corrige les fautes dans cette pièce et fournis le document DOCX corrigé.",
            [attachment("notes.docx", doc.getvalue())],
        ),
        "xlsx": (
            "Analyse ce classeur et fournis un XLSX avec une nouvelle feuille et un graphique des ventes. Conserve les données.",
            [spreadsheet],
        ),
        "web": (
            "Quelle est la dernière version stable de Python aujourd’hui ? Cite une source consultée.",
            [],
        ),
        "plain": ("Traduis « Bonne journée » en anglais, sans outil.", []),
    }


def post(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = Request(
        "http://127.0.0.1:8020/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=310) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def download(url: str) -> bytes:
    prefix = "/api/v1/terminals/atlas-files/files/serve/"
    if not url.startswith(prefix):
        raise ValueError("invalid_download_url")
    # Download the same file resource served by the UI, via its local terminal API.
    request = Request(
        "http://127.0.0.1:8000/files/serve/" + url[len(prefix) :],
        headers={"Authorization": "Bearer " + os.environ["OPEN_TERMINAL_API_KEY"]},
    )
    with urlopen(request, timeout=15) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
    if not 0 < len(raw) <= 16 * 1024 * 1024:
        raise ValueError("invalid_download_size")
    return bytes(raw)


def validate(name: str, answer: dict[str, Any]) -> bool:
    content = answer.get("choices", [{}])[0].get("message", {}).get("content", "")
    files = answer.get("atlas", {}).get("files", [])
    if not content.strip():
        return False
    if name == "owner_pdf":
        return len(content) > 600 and content.count("\n") >= 5
    if name == "plain":
        return "good day" in content.lower() or "have a nice day" in content.lower()
    if name == "web":
        return "https://" in content and "Python" in content
    suffixes = {Path(url).suffix for url in files}
    required = {
        "presentation": {".pptx", ".pdf"},
        "docx": {".docx"},
        "xlsx": {".xlsx"},
    }[name]
    if not required <= suffixes:
        return False
    for url in files:
        raw = io.BytesIO(download(url))
        if url.endswith(".pdf"):
            if len(PdfReader(raw).pages) < 3:
                return False
            continue
        with zipfile.ZipFile(raw) as archive:
            names = archive.namelist()
            if (
                url.endswith(".pptx")
                and sum(
                    n.startswith("ppt/slides/slide") and n.endswith(".xml")
                    for n in names
                )
                < 3
            ):
                return False
            if url.endswith(".docx"):
                text = " ".join(
                    ET.fromstring(archive.read("word/document.xml")).itertext()
                )
                if "résultats" not in text or "satisfaisants" not in text:
                    return False
            if url.endswith(".xlsx"):
                if sum(
                    n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
                    for n in names
                ) < 2 or not any(n.startswith("xl/charts/chart") for n in names):
                    return False
    return True


def write_report(report: dict[str, Any], root: Path = Path("BRAIN")) -> None:
    day = datetime.now(timezone.utc).date().isoformat()
    path = root / "probe" / (day + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    failures = [
        name + ": " + json.dumps(row, ensure_ascii=False)
        for name, row in report.items()
        if row["status"] is not True
    ]
    marker = root / "PROBE_FAILED.txt"
    if failures:
        marker.write_text("\n".join(failures) + "\n")
    else:
        marker.unlink(missing_ok=True)


def run(
    request: Callable[[dict[str, Any]], tuple[int, dict[str, Any]]] = post,
    selected: set[str] | None = None,
    observer: Callable[[str, Any, Any], None] | None = None,
) -> dict[str, Any]:
    report = {}
    for name, (question, attachments) in cases().items():
        if selected and name not in selected:
            continue
        started = time.monotonic()
        if observer:
            observer(name, None, None)
        payload: dict[str, Any] = {
            "model": PROFILES[0],
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": question}, *attachments],
                }
            ],
        }
        answer: dict[str, Any] = {}
        try:
            if name == "owner_pdf":
                payload["messages"][0]["content"].append(
                    attachment("document.pdf", OWNER_PDF.read_bytes())
                )
            status, answer = request(payload)
            good = status == 200 and validate(name, answer)
            code = (
                ""
                if good
                else answer.get("error", {}).get("code", "content_validation_failed")
            )
        except Exception as exc:
            status, good, code = 0, False, type(exc).__name__
        row = {
            "status": good,
            "seconds": round(time.monotonic() - started, 3),
            "cost_eur": answer.get("atlas", {}).get("cost_eur"),
            "cost_kind": answer.get("atlas", {}).get(
                "cost_kind", "measured" if good else "unavailable"
            ),
            "http_status": status,
            "error_code": code,
            "error_message": answer.get("error", {}).get("message", ""),
        }
        report[name] = row
        if observer:
            observer(name, row, answer)
        write_report(report)
        print(name, good, row["seconds"], code, flush=True)
    return report


if __name__ == "__main__":
    raise SystemExit(0 if all(row["status"] for row in run().values()) else 1)
