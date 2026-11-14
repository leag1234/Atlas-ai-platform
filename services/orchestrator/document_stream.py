"""A requested Word or PDF report written as streamed text, then published by code.

The provider sends a long tool argument as one final frame: a researched report
written as the content argument of publish_document showed nothing for 24 to
75 s, then arrived whole, and a call cut before its end lost all of it (staging
2026-11-13). Written as the answer of a call that may use no tool, with the
form rules of publish_document's description in the instruction, it streams
from the first second. Measured 2026-11-14 on the same model and research
material, 20 report requests (French and English), blind pairwise judge in
both orders: 5.5 against 5.3 out of 10, 9 wins, 9 losses, 2 ties; numbers
absent from the sources 4.5 % against 4.0 %; first visible text at 0.6 s
instead of 47 s; writing call 27 s instead of 48 s (documents about half as
long). The text then goes to publish_document unchanged: the same tool,
checks and verification as a call the model writes itself.

Code takes this step at one point only: the research of a file turn is done
and the agent has switched back to the documents group to publish the Word or
PDF file the request names, with no attached file to read. Anywhere else the
turn is unchanged, and so is everything when ATLAS_WORK_STREAMED_DOCUMENTS is
not "1" (the default).

The call keeps the Work tools with tool_choice "none": the gateway then sends
the provider no tool and the conversation as evidence (its answer-only path),
and still trims fetched pages like any Work step. A call offered no tool at all
would instead get the budget-answer instruction of the gateway.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import os
import re
import time
from typing import Any, Protocol
import unicodedata

from services.orchestrator.loop import Call, Message, Reservation, Turn

FLAG = "ATLAS_WORK_STREAMED_DOCUMENTS"
SUFFIXES = (".docx", ".pdf")
FORMATS = {".docx": "Word (DOCX)", ".pdf": "PDF"}
RESEARCH_TOOLS = frozenset({"web_search", "web_fetch"})
# Kept after the writing call for the publication and the closing answer.
PUBLISH_RESERVE_SECONDS = 30.0
# A cut call keeps its text when at least this much was written.
SALVAGE_MINIMUM_CHARACTERS = 1200
# The chat status shows the progress of the writing at most this often.
PROGRESS_INTERVAL_SECONDS = 2.0

CUT_NOTE = (
    "The document was cut by the time limit before its end: the published file "
    "holds the sections written before the cut. Say so plainly in your answer, "
    "and do not write or publish the document again."
)


def enabled() -> bool:
    return os.environ.get(FLAG) == "1"


def instruction(question: str, suffix: str) -> str:
    """The writing instruction: the form rules publish_document's description gives."""
    kind = FORMATS[suffix]
    return (
        f"Research is complete. Write the complete {kind} document requested now: "
        f"{question}\n"
        f"Your reply is the document itself: it is rendered as the {kind} file "
        "exactly as you write it, so write no sentence before or after it and no "
        "code fence around it.\n"
        "Form:\n"
        "- Markdown only. The first line is '# ' followed by the title; then '## ' "
        "and '### ' headings, paragraphs, bulleted or numbered lists and simple "
        "pipe tables.\n"
        "- Every heading is followed by text, a list or a table.\n"
        "- A table has a header row, then a separator row with exactly one --- cell "
        "per column, then rows with the same number of cells; at most 14 columns; "
        "a cell may be left empty.\n"
        "- Inline **bold**, *italic* and [text](address) are rendered. No images, "
        "no code blocks, no HTML, no LaTeX, no horizontal rules.\n"
        "Content:\n"
        "- Open with a short summary, then sections that answer what the request "
        "asks, then a last section headed Sources (in the language of the "
        "document) listing each web page used, with its title and address.\n"
        "- Cite web pages with their address, only pages retrieved in this "
        "conversation. Never cite a page, report or date of consultation that was "
        "not retrieved.\n"
        "- State only what the retrieved pages support; when they do not cover a "
        "point the request asks about, say so in one sentence instead of filling "
        "it.\n"
        "- Size the document to the request: a short report stays short.\n"
        "- Write in the language of the request."
    )


def retrieved_urls(output: Message) -> set[str]:
    """Pages a web tool result retrieved, read as the loop reads them."""
    if "error" in output:
        return set()
    data = output.get("data", output)
    if not isinstance(data, dict):
        return set()
    found = {str(data["url"])} if isinstance(data.get("url"), str) else set()
    rows = data.get("results")
    if isinstance(rows, list):
        found.update(
            str(row["link"])
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("link"), str)
        )
    return found


def file_name(text: str, suffix: str) -> str:
    """A file name from the title line: ASCII words joined by underscores."""
    title = next(
        (line.lstrip("#").strip() for line in text.splitlines() if line.strip()), ""
    )
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    stem = "_".join(re.findall(r"[A-Za-z0-9]+", folded))[:60].strip("_")
    return (stem or "Document") + suffix


def complete_part(text: str) -> str:
    """The text up to its last complete block, without a heading left empty."""
    end = text.rfind("\n\n")
    lines = (text[:end] if end > 0 else "").rstrip().splitlines()
    while lines and (not lines[-1].strip() or lines[-1].lstrip().startswith("#")):
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def switched_back_after_research(messages: list[Message]) -> bool:
    """Research results exist and the agent's last step switched tool groups."""
    names: dict[str, str] = {}
    last_calls: list[str] | None = None
    for message in messages:
        calls = message.get("tool_calls")
        if message.get("role") == "assistant":
            last_calls = []
        if isinstance(calls, list):
            for call in calls:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict):
                    name = str(function.get("name", ""))
                    names[str(call.get("id"))] = name
                    if last_calls is not None:
                        last_calls.append(name)
    researched = any(
        message.get("role") == "tool"
        and names.get(str(message.get("tool_call_id"))) in RESEARCH_TOOLS
        for message in messages
    )
    return (
        researched
        and bool(last_calls)
        and all(name == "switch_tool_group" for name in last_calls or ())
    )


Sink = Callable[[dict[str, object]], Awaitable[None]]


class WritingModel(Protocol):
    sink: Sink | None
    answer_only: bool
    progress_deadline: float | None

    def estimate(self, messages: list[Message]) -> Reservation: ...


class Progress:
    """Collects the streamed text and reports its size to the chat status."""

    def __init__(self, report: Sink | None) -> None:
        self.parts: list[str] = []
        self.report = report
        self.shown = 0.0

    @property
    def text(self) -> str:
        return "".join(self.parts)

    async def __call__(self, event: dict[str, object]) -> None:
        delta = event.get("delta")
        if not isinstance(delta, dict) or not isinstance(delta.get("content"), str):
            return
        self.parts.append(str(delta["content"]))
        now = time.monotonic()
        if self.report is None or now - self.shown < PROGRESS_INTERVAL_SECONDS:
            return
        self.shown = now
        text = self.text
        # Sizes only: never the text, a heading or anything the model wrote.
        await self.report(
            {
                "work_step": {
                    "writing": {
                        "sections": len(re.findall(r"(?m)^#{2,6} ", text)),
                        "words": len(text.split()),
                    }
                }
            }
        )


class DocumentWriter:
    """Writes the requested document as streamed text at the publication step."""

    def __init__(
        self,
        *,
        suffix: str,
        question: str,
        pending: Callable[[], bool],
        group: Callable[[], str],
        retrieved: Callable[[], set[str]],
        report: Sink | None = None,
    ) -> None:
        if suffix not in SUFFIXES:
            raise ValueError("Unsupported streamed document format")
        self.suffix = suffix
        self.question = question
        self.pending = pending
        self.group = group
        self.retrieved = retrieved
        self.report = report
        self.attempted = False
        # The published text ends where the call was cut.
        self.cut = False

    def due(self, messages: list[Message], offered: list[dict[str, Any]]) -> bool:
        if self.attempted or not self.pending() or self.group() != "documents":
            return False
        names = {
            str(function.get("name"))
            for tool in offered
            if isinstance(function := tool.get("function"), dict)
        }
        return "publish_document" in names and switched_back_after_research(messages)

    def annotate(self, messages: list[Message]) -> list[Message]:
        """After a cut, the following calls are told the file is incomplete."""
        if not self.cut:
            return messages
        return [*messages, {"role": "user", "content": CUT_NOTE}]

    async def write(
        self,
        model: WritingModel,
        messages: list[Message],
        timeout: float,
        complete: Callable[[list[Message], float], Awaitable[Turn]],
    ) -> Turn | None:
        """The publish_document call of the streamed document, or None to go on as before."""
        self.attempted = True
        progress = Progress(self.report)
        writing: list[Message] = [
            *messages,
            {"role": "user", "content": instruction(self.question, self.suffix)},
        ]
        saved = model.sink, model.answer_only, model.progress_deadline
        end = (
            model.progress_deadline - PUBLISH_RESERVE_SECONDS
            if model.progress_deadline is not None
            else None
        )
        started = time.monotonic()
        limit = timeout if end is None else min(timeout, end - started)
        if limit <= 0:
            return None
        model.sink, model.answer_only = progress, True
        if end is not None:
            model.progress_deadline = end
        log = logging.getLogger(__name__)
        try:
            # The gateway runs a call still receiving output up to end; this
            # bound only catches a transport that outlives it.
            bound = (end - started if end is not None else timeout) + 5.0
            async with asyncio.timeout(bound):
                turn = await complete(writing, limit)
        except Exception as exc:
            text = complete_part(progress.text)
            log.warning(
                json.dumps(
                    {
                        "event": "streamed_document_cut",
                        "error": type(exc).__name__,
                        "received_chars": len(progress.text),
                        "kept_chars": len(text),
                        "seconds": round(time.monotonic() - started, 1),
                    }
                )
            )
            if len(text) < SALVAGE_MINIMUM_CHARACTERS:
                raise
            self.cut = True
            # The cut call's usage is unknown: its whole reservation stays spent.
            return self.publication(text, model.estimate(messages))
        finally:
            model.sink, model.answer_only, model.progress_deadline = saved
        text = turn.text.strip()
        log.info(
            json.dumps(
                {
                    "event": "streamed_document_written",
                    "chars": len(text),
                    "seconds": round(time.monotonic() - started, 1),
                }
            )
        )
        if not text:
            return None
        return self.publication(text + "\n", turn.usage)

    def publication(self, text: str, usage: Reservation | None) -> Turn:
        from services.orchestrator.source_truth import without_unretrieved

        # D7: a page is cited only if this turn retrieved it.
        text = without_unretrieved(text, self.retrieved())
        arguments = json.dumps(
            {
                "path": file_name(text, self.suffix),
                "content": text,
                "sources": [],
            },
            ensure_ascii=False,
        )
        return Turn(
            "", (Call("streamed-document-1", "publish_document", arguments),), usage
        )
