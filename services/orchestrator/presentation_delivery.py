"""Validated, deterministic PPTX delivery; the model never writes executable code."""

import asyncio
from decimal import Decimal
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from packages.doc_render.schema import Content
    from services.orchestrator.terminal_client import VerifiedDocument

from pydantic import ValidationError

from packages.doc_render import PresentationOutline, render, verify
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Message, Result
from services.orchestrator.model import GatewayError, GatewayModel
from services.orchestrator.terminal_client import TerminalClient


OUTLINE_INSTRUCTION = """
For this PPTX request, return ONLY a JSON object with this exact shape:
{"title":"...","slides":[{"title":"...","bullets":["..."],"notes":"...","table":{"headers":["..."],"rows":[["..."]]}]}
`notes` and `table` are optional. Include no Markdown, code, tool calls, file claims,
or extra keys. Write all user-visible text in the request language.
"""


def requested_topic_words(question: str) -> set[str]:
    ignored = {
        "about",
        "avec",
        "create",
        "cree",
        "crée",
        "dans",
        "downloadable",
        "faire",
        "fais",
        "from",
        "make",
        "pdf",
        "presentation",
        "présentation",
        "pptx",
        "slides",
        "sur",
        "the",
        "une",
        "with",
    }
    return {
        word.casefold()
        for word in re.findall(r"[\w/]+", question, flags=re.UNICODE)
        if len(word) >= 3 and word.casefold() not in ignored
    }


def outline_markdown(text: str) -> str:
    """Readable fallback for an outline that could not become a file (D4)."""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if not isinstance(data, dict) or not isinstance(data.get("slides"), list):
        return text
    lines = ["# " + str(data.get("title", "")).strip()]
    for slide in data["slides"]:
        if not isinstance(slide, dict):
            continue
        lines += ["", "## " + str(slide.get("title", "")).strip()]
        lines += ["- " + str(b).strip() for b in slide.get("bullets", []) or []]
    return "\n".join(lines).strip()


def parse_outline(text: str, question: str) -> PresentationOutline:
    """Validate untrusted model JSON and require the requested subject in the deck."""
    data = json.loads(text)
    outline = PresentationOutline.model_validate(data)
    deck_text = " ".join(outline.content().texts()).casefold()
    # Requests carry filler words (pronouns, "downloadable"); the deck only has
    # to be about the subject, so one requested topic word is enough.
    words = requested_topic_words(question)
    if words and not any(word in deck_text for word in words):
        raise ValueError("requested_topic_words_missing:" + ",".join(sorted(words)))
    return outline


async def repair_outline(
    model: GatewayModel,
    request: ChatRequest,
    result: Result,
    reason: str,
    started: float,
) -> Result | None:
    """Exactly one bounded answer-only repair call for invalid structured output."""
    remaining = request.timeout_seconds - (time.monotonic() - started) - 30
    available = max(Decimal(0), Decimal("0.30") - result.cost)
    if remaining <= 0 or available <= 0:
        return None
    saved_tools, saved_choice, saved_sink = (
        model.tools,
        model.required_file_tool,
        model.sink,
    )
    model.tools, model.required_file_tool, model.sink = [], None, None
    try:
        model.spent = max(model.spent, result.cost)
        model.delivery_reserve = Decimal(0)
        messages: list[Message] = [
            {"role": "user", "content": request.messages[-1].text},
            {"role": "assistant", "content": result.text},
            {
                "role": "system",
                "content": OUTLINE_INSTRUCTION
                + "\nYour prior output was invalid ("
                + reason
                + "). Replace it with valid JSON only.",
            },
        ]
        async with asyncio.timeout(remaining):
            turn = await model.complete(messages, remaining)
        cost = turn.usage.cost if turn.usage else available
        return Result(
            text=turn.text,
            state="done",
            cost=result.cost + cost,
            tool_calls=result.tool_calls,
        )
    except (GatewayError, RuntimeError, TimeoutError):
        return None
    finally:
        model.tools, model.required_file_tool, model.sink = (
            saved_tools,
            saved_choice,
            saved_sink,
        )
        model.delivery_reserve = Decimal(0)


async def deliver_presentation(
    model: GatewayModel,
    request: ChatRequest,
    item: Interaction,
    result: Result,
    terminal: TerminalClient,
    started: float,
    formats: tuple[str, ...],
) -> Result | None:
    """Build, reopen, check and publish a PPTX; return None for D5 inline fallback."""
    try:
        outline = parse_outline(result.text, request.messages[-1].text)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        repaired = await repair_outline(
            model, request, result, type(exc).__name__, started
        )
        if repaired is None:
            item.erreurs.append("presentation_outline_invalid")
            return None
        try:
            outline = parse_outline(repaired.text, request.messages[-1].text)
            result = repaired
        except (json.JSONDecodeError, ValidationError, ValueError):
            # The failed repair was still a provider call and must reach the ledger.
            result.cost = repaired.cost
            item.erreurs.append("presentation_outline_invalid_after_repair")
            return None
    remaining = request.timeout_seconds - (time.monotonic() - started)
    if remaining <= 0:
        item.erreurs.append("presentation_render_timeout")
        return None
    try:
        with TemporaryDirectory(prefix="atlas-pptx-") as directory:
            content = outline.content()
            for suffix in ("pptx", "pdf"):
                if suffix not in formats:
                    continue
                path = Path(directory) / ("presentation." + suffix)
                render(content, path, image_root=Path(directory), slides=True)
                verified = verify(
                    path,
                    expected=content,
                    expected_count=len(outline.slides),
                    require_pagination=True,
                )
                if verified.count != len(outline.slides) or any(
                    not slide.strip() for slide in verified.text
                ):
                    raise ValueError("presentation_verification_failed")
                uploaded = await terminal.upload_generated(
                    path.name, path.read_bytes(), min(30, remaining)
                )
                published = await terminal.publish(
                    uploaded, "created", min(30, remaining)
                )
                if "error" in published:
                    raise ValueError("presentation_publish_failed")
    except (OSError, ValueError, TimeoutError):
        item.erreurs.append("presentation_build_or_publish_failed")
        return None
    return result


# A fixed program, not worker/model code. Its environment contains no credentials.
PROJECT_RENDER_PROGRAM = r"""
import base64, hashlib, json, resource, sys, tempfile
from pathlib import Path
resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
resource.setrlimit(resource.RLIMIT_AS, (536870912, 536870912))
resource.setrlimit(resource.RLIMIT_FSIZE, (16777216, 16777216))
from packages.doc_render import render, verify
from packages.doc_render.schema import Content
payload = json.load(sys.stdin)
content = Content.model_validate(payload["content"])
if any(section.images for section in content.sections):
    raise ValueError("Scoped image resolution is required")
suffix = payload["suffix"]
if suffix not in (".docx", ".xlsx", ".pptx", ".pdf"):
    raise ValueError("Unsupported format")
with tempfile.TemporaryDirectory(prefix="atlas-deliverable-") as directory:
    path = Path(directory) / ("artifact" + suffix)
    render(content, path, image_root=Path(directory))
    checked = verify(path, expected=content)
    raw = path.read_bytes()
preview = "\n\n".join(checked.text).encode("utf-8")
normalized = "".join(preview.decode().split())
if suffix == ".xlsx":
    # A cell typed "12.50" is stored as the number 12.5, which verify() matched
    # to it: a source excerpt found in the typed text is in the workbook.
    normalized += "\n" + "".join("".join(content.texts()).split())
# Inline markers are read, never displayed: "the **first** clause" is in the
# file as "the first clause", which verify() matched to it. A source excerpt
# is accepted as it was typed.
normalized += "\n" + "".join("".join(content.typed()).split())
for source in payload["source_texts"]:
    if not source.strip() or "".join(source.split()) not in normalized:
        raise ValueError("Source coverage verification failed")
if not 0 < len(raw) <= payload["max_bytes"] or len(preview) > 1048576:
    raise ValueError("Generated artifact exceeds delivery limits")
print(json.dumps(dict(content=base64.b64encode(raw).decode(), preview=base64.b64encode(preview).decode(), sha256=hashlib.sha256(raw).hexdigest())))
"""


# The renderer raises named codes ("unsupported_formula", "table_row_width"). Only
# such a code, or one of the fixed messages of the program above, reaches the
# model: never a traceback, a path or content. A failure without its reason
# left the agent retrying the same formula until it gave up (staging 2026-11-11).
RENDER_REASON = re.compile(
    r"[A-Za-z_][\w.]*(?:Error|Exception): "
    r"([a-z][a-z0-9_]{2,60}(?::[A-Za-z0-9_]{1,40}){0,3})"
)
RENDER_MESSAGES = (
    "Source coverage verification failed",
    "Generated artifact exceeds delivery limits",
    "Scoped image resolution is required",
    "Unsupported format",
)
FORMULA_HELP = (
    " (formulas support numbers, text, TRUE/FALSE, cell and range references,"
    " + - * / ^ & %, = <> < > <= >=, parentheses and SUM, SUMIF, AVERAGE, MIN,"
    " MAX, COUNT, ROUND, ABS, IF, AND, OR, NOT, IFERROR; arithmetic needs numbers"
    " or empty cells: leave a cell to fill in empty, it counts as 0, and put no"
    " dash or placeholder text in cells a formula computes with; a division or"
    ' AVERAGE over cells still empty needs IFERROR(..., ""); in a workbook'
    " every table cell that starts with = is a formula: a cell that must show"
    " a leading = as text starts with an apostrophe, as in '=A1)"
)


NUMERIC_HELP = (
    " (the code ends with the cell and its sheet: that cell holds text that"
    " begins like a number, which a formula would skip or misread; write a plain"
    " number there, such as 1250.50, 1 250,50 € or 45 %, with no unit and no"
    " ambiguous separator like 1,250, and keep header and label cells out of"
    " formula ranges)"
)
OUTSIDE_HELP = (
    " (the code ends with the cell that holds the formula and its sheet: that"
    " formula refers to a cell outside the table. In the file the table starts"
    " at cell A1 of its sheet: its header is row 1, its first data row is row"
    " 2, and nothing above it shifts the rows, so a total under four data rows"
    " is =SUM(B2:B5); count the rows from the header of the table, and write a"
    " cell of another sheet as 2!B2)"
)
NOT_FIRST_HELP = (
    " (the code ends with the sheet: only the first table of a section holds"
    " formulas, because it starts at cell A1 of its sheet; give each table with"
    " formulas its own section, under its own heading, and write a cell of"
    " another sheet as 2!B2; a cell that must show a leading = as text starts"
    " with an apostrophe, as in '=A1)"
)
COLUMNS_HELP = (
    " (the number is the most columns a table may have in this format; a wider"
    " table fits an .xlsx file, up to 60 columns, or is split in two)"
)


def render_failure_reason(diagnostic: str) -> str:
    """The safe reason of a failed render, or "" when there is none."""
    lines = [line.strip() for line in diagnostic.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    message = next((m for m in RENDER_MESSAGES if last.endswith(": " + m)), None)
    if message is not None:
        return message
    match = RENDER_REASON.fullmatch(last)
    if match is None:
        return ""
    reason = match[1]
    if reason.startswith("formula_requires_numeric_cells:"):
        reason += NUMERIC_HELP
    elif reason.startswith("formula_reference_outside_table:"):
        reason += OUTSIDE_HELP
    elif reason.startswith("formula_table_not_first:"):
        reason += NOT_FIRST_HELP
    elif "formula" in reason or reason == "unsupported_sumif_criteria":
        reason += FORMULA_HELP
    elif reason.startswith("table_columns_limit:"):
        reason += COLUMNS_HELP
    return reason


def build_project_document(
    content: "Content",
    suffix: str,
    source_texts: list[str],
    *,
    max_bytes: int,
    timeout: float,
) -> "VerifiedDocument":
    """Render and reopen in a bounded process; no caller-supplied validation exists."""
    import base64
    import hashlib
    import subprocess
    import sys

    from services.orchestrator.terminal_client import VerifiedDocument

    if not 0 < max_bytes <= 16 * 1024 * 1024 or timeout <= 0:
        raise ValueError("Invalid document verification budget")
    try:
        result = subprocess.run(
            [sys.executable, "-c", PROJECT_RENDER_PROGRAM],
            input=json.dumps(
                dict(
                    content=content.model_dump(mode="json"),
                    suffix=suffix,
                    source_texts=source_texts,
                    max_bytes=max_bytes,
                )
            ).encode(),
            capture_output=True,
            timeout=min(timeout, 30),
            cwd=Path(__file__).resolve().parents[2],
            env={},
        )
    except subprocess.TimeoutExpired:
        raise ValueError("Document verification timed out") from None
    if result.returncode != 0:
        reason = render_failure_reason(result.stderr.decode("utf-8", errors="replace"))
        raise ValueError(
            "Document rendering or requested-content/source verification failed"
            + (": " + reason if reason else "")
        )
    value = json.loads(result.stdout)
    raw = base64.b64decode(value["content"], validate=True)
    preview = base64.b64decode(value["preview"], validate=True)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != value["sha256"] or not 0 < len(raw) <= max_bytes:
        raise ValueError("Document verification integrity failed")
    return VerifiedDocument(raw, preview, digest)
