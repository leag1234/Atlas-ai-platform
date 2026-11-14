"""App and drawing turns for the side panel.

The model writes the whole file as a fenced block (```html or ```svg) in its
ordinary, streamed answer. Atlas extracts the last complete block, validates
it, stores it as the artifact of the conversation and answers with a short
description and the panel link. No tool call carries the file and the model
never names an artifact: a follow-up changes the conversation's current
artifact, whose source Atlas passes to the model.

Measured on staging (2026-11-11) with the previous design, where the file was
the JSON argument of a create_artifact call: 8 of 18 corpus apps failed
(invalid arguments, invented artifact ids, technical text), and the user saw
only a status for a median 53 s.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import aiohttp
from pydantic import ValidationError

from packages.artifact_html import Problem
from packages.artifact_source import (
    LineCounter,
    drawing_source,
    filename_for,
    is_drawing,
    last_block,
    prepare,
    prose,
    title_of,
)
from packages.language import LANGUAGES, language_instruction
from packages.limits import LimitError
from packages.ports import atlas_ports
from packages.user_errors import app_failure, app_quota
from services.orchestrator import artifact_store
from services.orchestrator.artifact_store import ArtifactError
from services.orchestrator.artifact_tools import (
    PROBLEM_DETAILS,
    CurrentArtifact,
    compose_answer,
)
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import GatewayError, Message
from services.orchestrator.model import GatewayModel
from services.orchestrator.stream_client import Sink, sink_context

Kind = Literal["app", "drawing"]

# Output room of one app or drawing call. The gateway accepts up to
# APP_OUTPUT_CEILING tokens for a file turn (agent_provider.py); the per-call
# cost ceiling of a file turn (0.30 EUR) still bounds every attempt.
APP_OUTPUT_TOKENS = 16000
APP_OUTPUT_CEILING = 16000
# One generation and one repair (invalid file, no file, or a failed call).
MAX_CALLS = 2
# Time kept for validation, storage and the answer after the last call.
DEADLINE_MARGIN = 8.0
# A call that cannot write a file in less is not started.
MINIMUM_CALL_SECONDS = 25.0
PROGRESS_SECONDS = 1.0
HISTORY_MESSAGES = 12
HISTORY_CHARS = 6000
SUMMARY_CHARS = 700
MAX_PROBLEMS = 10
MAX_DETAIL_CHARS = 80
# D5: budget and quota refusals keep their measured values and their own path.
BUDGET_CODES = frozenset(
    {"cost_budget", "cost", "daily_budget", "quota_exhausted", "quota_exceeded"}
)

_FILE_RULES = """\
- Self-contained: inline all CSS and JavaScript. No CDN, no external URL, no font or
  image download, no network request of any kind. Draw with canvas, SVG or CSS and
  quote every attribute value (viewBox="0 0 400 300", stroke-width="2"); embed small
  assets as data: URIs. The preview blocks all network access.
- Nothing is kept: data is lost on reload and storage may be unavailable. Keep state
  in memory and wrap any storage access in try/catch. Never reload or navigate the
  page, never open windows; for forms, call preventDefault.
- Never use alert, confirm or prompt; show messages, game over and questions inside
  the page.
- Games end with a clear in-page message (the user-language equivalent of "Game over"
  with the score) and a visible restart button.
- Interface language: the user's language (lang attribute, <title> and every visible
  label). Code identifiers and comments stay in English.
- Usable at once: short on-screen instructions; keyboard controls when relevant and
  touch controls (buttons or swipe) for phones; layout from 360 px wide; visible
  focus; good contrast.
- Sound: generate it with the Web Audio API (oscillators, noise buffers), started by a
  user gesture.
- Compact code: loops and small functions instead of repeated markup, no dead code,
  few comments. Everything that is asked works.
- A requested language, framework or platform is always used; never substitute
  another one silently. If the result cannot run inside Atlas, say so in one sentence
  after the block and give the command to run it locally.
"""

APP_INSTRUCTIONS = (
    """\
App mode. You write small interactive web apps (game, tool, calculator, timer, quiz,
chart, music toy, small page) that run in the Atlas side panel.
Answer format, strictly:
1. The complete app as ONE self-contained HTML file in a single fenced code block that
   starts with ```html and ends with ```. Begin the block at once: no introduction,
   no plan and no code before it.
2. After the block, at most three short sentences in the user's language for a
   non-technical user: what the app does, how to use it, the main assumptions. No
   code terms, no milliseconds or frame rates. Never write a second block.
Atlas stores the file and shows it to the user; there is no tool to call.
The file:
- <!DOCTYPE html>, <html lang="..."> in the user's language, and a <title> of at
  most 60 characters in the user's language naming the app.
"""
    + _FILE_RULES
    + """\
- Do not ask questions you can settle yourself: make reasonable choices. Ask (one
  short question, without a block) only when an essential choice cannot be made.
"""
)

DRAWING_INSTRUCTIONS = """\
Drawing mode. You draw pictures as SVG code shown in the Atlas side panel.
Answer format, strictly:
1. The complete drawing as ONE SVG document in a single fenced code block that starts
   with ```svg and ends with ```. Begin the block at once: no introduction before it.
2. After the block, one or two short sentences in the user's language describing the
   drawing, without code terms. Never write a second block.
Atlas stores the drawing and shows it to the user; there is no tool to call.
The SVG:
- <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 W H"> with a <title> of at most
  60 characters in the user's language.
- Quote every attribute value (viewBox="0 0 400 300", stroke-width="2"). Escape & as
  &amp; in text.
- Shapes, paths, gradients and text only: no external image, font or URL, no script,
  no foreignObject.
- Draw with care: a recognizable subject, a composed scene (background first), pleasant
  colors, and every element and count the user asks for (legs, windows, people).
- Text inside the picture is in the user's language.
- Never refuse and never say that you cannot draw: if a photograph or a painting is
  asked for, draw the best SVG illustration of it. Do not ask questions; make
  reasonable choices.
"""

CURRENT_SOURCE = {
    "app": (
        "The current app of this conversation (version {version}) is below. To change "
        "it, apply the user's request and write the COMPLETE new file in one ```html "
        "block: all of it, not only the changed part. Keep everything the user did not "
        "ask to change. If the user only asks a question about the app, answer in a few "
        "sentences without any block.\n```html\n{source}\n```"
    ),
    "drawing": (
        "The current drawing of this conversation (version {version}) is below. To "
        "change it, apply the user's request and write the COMPLETE new SVG in one "
        "```svg block. Keep everything the user did not ask to change. If the user only "
        "asks a question about the drawing, answer in a few sentences without any "
        "block.\n```svg\n{source}\n```"
    ),
}
PREVIOUS_UNAVAILABLE = (
    "The previous {noun} of this conversation is no longer available (stored for 30 "
    "days, or too large to edit). Write a new complete file that does what the user "
    "asks."
)
NO_BLOCK_REPAIR = (
    "Your answer did not contain the file. Write the complete file now, in one fenced "
    "```{fence} block, then at most two short sentences."
)
INVALID_REPAIR = (
    "Atlas rejected this file:\n{problems}\nFix every listed problem and write the "
    "complete corrected file in one fenced ```{fence} block, then at most two short "
    "sentences."
)
EXTRA_DETAILS = {
    "svg_syntax": "the SVG is not well-formed XML here (unclosed element, unquoted "
    "attribute value or stray character)",
    "svg_root": "the block must contain one <svg> element",
    "svg_doctype": "remove the DOCTYPE and entity declarations",
    "not_a_document": "the file must be a complete HTML document with <html> and <body>",
    "external_reference": "remove this external URL or file reference",
    "network_api": "remove this network access",
    "navigation_api": "remove this page navigation",
    "forbidden_element": "remove this element",
    "too_large": "the file is too large; make it shorter",
}

PROGRESS = {
    "app": {
        "en": (
            "Writing the app…",
            "Writing the app… {lines} lines",
            "Checking the app…",
            "Fixing the app…",
        ),
        "fr": (
            "Écriture de l'application…",
            "Écriture de l'application… {lines} lignes",
            "Vérification de l'application…",
            "Correction de l'application…",
        ),
        "de": (
            "App wird geschrieben…",
            "App wird geschrieben… {lines} Zeilen",
            "App wird geprüft…",
            "App wird korrigiert…",
        ),
        "es": (
            "Escribiendo la aplicación…",
            "Escribiendo la aplicación… {lines} líneas",
            "Comprobando la aplicación…",
            "Corrigiendo la aplicación…",
        ),
        "it": (
            "Scrittura dell'app…",
            "Scrittura dell'app… {lines} righe",
            "Verifica dell'app…",
            "Correzione dell'app…",
        ),
    },
    "drawing": {
        "en": (
            "Drawing…",
            "Drawing… {lines} lines",
            "Checking the drawing…",
            "Fixing the drawing…",
        ),
        "fr": (
            "Dessin en cours…",
            "Dessin en cours… {lines} lignes",
            "Vérification du dessin…",
            "Correction du dessin…",
        ),
        "de": (
            "Zeichnung entsteht…",
            "Zeichnung entsteht… {lines} Zeilen",
            "Zeichnung wird geprüft…",
            "Zeichnung wird korrigiert…",
        ),
        "es": (
            "Dibujando…",
            "Dibujando… {lines} líneas",
            "Comprobando el dibujo…",
            "Corrigiendo el dibujo…",
        ),
        "it": (
            "Disegno in corso…",
            "Disegno in corso… {lines} righe",
            "Verifica del disegno…",
            "Correzione del disegno…",
        ),
    },
}
DEFAULT_TITLES = {
    "app": {
        "en": "App",
        "fr": "Application",
        "de": "App",
        "es": "Aplicación",
        "it": "App",
    },
    "drawing": {
        "en": "Drawing",
        "fr": "Dessin",
        "de": "Zeichnung",
        "es": "Dibujo",
        "it": "Disegno",
    },
}


def output_tokens() -> int:
    """The output room of an app call: ATLAS_ARTIFACT_MAX_TOKENS, within the ceiling."""
    try:
        value = int(os.environ.get("ATLAS_ARTIFACT_MAX_TOKENS", APP_OUTPUT_TOKENS))
    except ValueError:
        value = APP_OUTPUT_TOKENS
    return max(1000, min(value, APP_OUTPUT_CEILING))


def instructions(kind: Kind) -> str:
    return DRAWING_INSTRUCTIONS if kind == "drawing" else APP_INSTRUCTIONS


class Progress:
    """Model sink of an app turn: the file is never streamed, its progress is."""

    def __init__(self, sink: Sink | None, kind: Kind, language: str) -> None:
        self.sink = sink
        labels = PROGRESS[kind]
        self.labels = labels.get(language, labels["en"])
        self.counter = LineCounter()
        self.reported = 0
        self.last = 0.0
        self.started = time.monotonic()
        self.first_status: float | None = None
        self.first_lines: float | None = None

    async def status(self, index: int, lines: int = 0) -> None:
        if self.sink is None:
            return
        now = time.monotonic()
        if self.first_status is None:
            self.first_status = now - self.started
        if index == 1 and self.first_lines is None:
            self.first_lines = now - self.started
        self.last = now
        await self.sink(
            {
                "phase": "artifact_progress",
                "description": self.labels[index].format(lines=lines),
            }
        )

    def restart(self) -> None:
        self.counter = LineCounter()
        self.reported = 0

    async def __call__(self, event: dict[str, object]) -> None:
        delta = event.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if not isinstance(content, str) or not content:
                return
            lines = self.counter.feed(content)
            if (
                self.counter.inside
                and lines >= 2
                and lines > self.reported
                and time.monotonic() - self.last >= PROGRESS_SECONDS
            ):
                self.reported = lines
                await self.status(1, lines)
            return
        if event.get("phase") in {"provider_fallback", "reasoning_fallback"}:
            # The gateway restarts the call before any content: so does the count.
            self.restart()
            await self.status(0)


@dataclass
class Current:
    artifact_id: str
    version: int
    title: str
    filename: str
    source: str


def _current(current: CurrentArtifact | None) -> tuple[Current | None, bool]:
    """The artifact a follow-up changes, and whether a previous one was lost."""
    if current is None:
        return None, False
    artifact = current.artifact
    if not isinstance(artifact, dict):
        return None, True
    content = artifact.get("content")
    version = artifact.get("version")
    if not isinstance(content, str) or not isinstance(version, int):
        return None, True
    return (
        Current(
            str(artifact.get("artifact_id")),
            version,
            str(artifact.get("title") or ""),
            str(artifact.get("filename") or ""),
            content,
        ),
        False,
    )


def _history(request: ChatRequest) -> list[Message]:
    messages: list[Message] = []
    for message in request.messages[:-1][-HISTORY_MESSAGES:]:
        if message.role not in {"user", "assistant"} or not message.text.strip():
            continue
        text = message.text
        if len(text) > HISTORY_CHARS:
            text = text[:HISTORY_CHARS] + " […]"
        messages.append({"role": message.role, "content": text})
    return messages


def _summary(text: str) -> str:
    from services.orchestrator.artifact_tools import clean_summary

    summary = clean_summary(prose(text))
    if len(summary) <= SUMMARY_CHARS:
        return summary
    cut = summary[:SUMMARY_CHARS]
    end = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > 0 else cut).strip()


def _problem_lines(problems: list[Problem], source: str) -> str:
    lines = source.splitlines()
    rendered = []
    for problem in problems[:MAX_PROBLEMS]:
        detail = PROBLEM_DETAILS.get(problem.code) or EXTRA_DETAILS.get(problem.code)
        if detail is None and 1 <= problem.line <= len(lines):
            detail = lines[problem.line - 1].strip()[:MAX_DETAIL_CHARS]
        rendered.append(f"- line {problem.line}: {problem.code}: {detail or ''}")
    return "\n".join(rendered)


async def process_app_turn(
    request: ChatRequest,
    item: Interaction,
    *,
    intent: str,
    kind: Kind,
    current: CurrentArtifact | None,
    cost_limit: Decimal | None = None,
    wall_clock_limit: float | None = None,
) -> None:
    """Write, check and store the app or drawing of this turn; answer with its link."""
    started = time.monotonic()
    language = request.lang if request.lang in LANGUAGES else "en"
    previous, lost = _current(current) if intent != "create" else (None, False)
    if previous is not None:
        kind = "drawing" if is_drawing(previous.source) else "app"
    progress = Progress(sink_context.get(), kind, request.status_language)
    item.task_type = "artifact"
    deadline = started + (
        request.timeout_seconds if wall_clock_limit is None else wall_clock_limit
    )
    fence = "svg" if kind == "drawing" else "html"
    failure = "artifact_missing"
    summary = ""
    timed_out = False
    model: GatewayModel | None = None
    await progress.status(0)
    try:
        model = await GatewayModel.connect(
            os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway")),
            local_enabled=os.environ.get("GPU_LOCAL", "0") == "1",
        )
        model.tools = []
        model.observing = True
        model.local_enabled = os.environ.get("GPU_LOCAL", "0") == "1"
        model.configure_quality(request.model, output_tokens(), produces_files=True)
        # Reasoning would spend the output room before the file (and is not
        # streamed as progress); the file itself is the work.
        model.reasoning_ceiling = "none"
        model.cost_ceiling = cost_limit
        model.sink = progress
        messages: list[Message] = [
            {
                "role": "system",
                "content": instructions(kind)
                + "\n"
                + language_instruction(request.prose_lang),
            },
            *_history(request),
        ]
        if previous is not None:
            source = (
                drawing_source(previous.source)
                if kind == "drawing"
                else previous.source
            )
            messages.append(
                {
                    "role": "system",
                    "content": CURRENT_SOURCE[kind].format(
                        version=previous.version, source=source
                    ),
                }
            )
        elif lost:
            noun = "drawing" if kind == "drawing" else "app"
            messages.append(
                {"role": "system", "content": PREVIOUS_UNAVAILABLE.format(noun=noun)}
            )
        messages.append({"role": "user", "content": request.messages[-1].text})
        for attempt in range(MAX_CALLS):
            remaining = deadline - time.monotonic() - DEADLINE_MARGIN
            if remaining < MINIMUM_CALL_SECONDS:
                if attempt == 0:
                    failure = "timeout"
                timed_out = failure == "timeout"
                break
            if attempt:
                progress.restart()
                repair = failure in {"artifact_invalid", "artifact_missing"}
                await progress.status(3 if repair else 0)
            # The gateway accepts at most 300 s for a file turn.
            remaining = min(remaining, 290.0)
            try:
                async with asyncio.timeout(remaining):
                    turn = await model.complete(messages, remaining)
            except TimeoutError:
                failure, timed_out = "timeout", True
                break
            except GatewayError as exc:
                if exc.code in BUDGET_CODES:
                    raise
                failure = exc.code
                item.provider_retries += exc.retries
                logging.getLogger(__name__).warning(
                    "app_turn_call_failed code=%s attempt=%d", exc.code, attempt + 1
                )
                continue
            except LimitError as exc:
                if exc.code in BUDGET_CODES:
                    raise
                failure = exc.code
                continue
            except (RuntimeError, ValueError, ValidationError) as exc:
                failure = (
                    str(exc)
                    if str(exc).isidentifier() and len(str(exc)) <= 60
                    else type(exc).__name__
                )
                logging.getLogger(__name__).warning(
                    "app_turn_call_failed code=%s attempt=%d", failure, attempt + 1
                )
                continue
            text = turn.text
            block = last_block(text)
            if block is not None and not summary:
                # After a repair the model describes its fix; the person wants
                # the description of the app it first wrote.
                summary = _summary(text)
            if block is None:
                answer = prose(text)
                question = answer.rstrip().endswith("?") and len(answer) <= 800
                if answer and (previous is not None or question):
                    # A question about the app, or the one essential question.
                    item.reponse, item.state = answer, "done"
                    return
                failure = "artifact_missing"
                messages += [
                    {"role": "assistant", "content": text or "(empty)"},
                    {"role": "user", "content": NO_BLOCK_REPAIR.format(fence=fence)},
                ]
                continue
            await progress.status(2)
            title = (
                title_of(block.content)
                or (previous.title if previous is not None else "")
                or DEFAULT_TITLES[kind].get(language, DEFAULT_TITLES[kind]["en"])
            )
            prepared = prepare(block, title, language)
            if prepared.problems:
                failure = "artifact_invalid"
                logging.getLogger(__name__).warning(
                    "app_turn_invalid attempt=%d codes=%s",
                    attempt + 1,
                    ",".join(sorted({problem.code for problem in prepared.problems})),
                )
                messages += [
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": INVALID_REPAIR.format(
                            problems=_problem_lines(prepared.problems, prepared.source),
                            fence="svg" if block.kind == "svg" else "html",
                        ),
                    },
                ]
                continue
            stored = _store(request, item, prepared.document, title, kind, previous)
            if isinstance(stored, str):
                failure = stored
                break
            artifact_id, version, filename = stored
            item.reponse = compose_answer(
                "",
                summary or _summary(text),
                artifact_id,
                version,
                title,
                filename,
                language,
                # A drawing has no data to lose on reload.
                temporary=kind == "app",
            )
            item.state = "done"
            item.latence_ms["artifact_ready"] = (time.monotonic() - started) * 1000
            return
        item.erreurs.append(failure)
        item.reponse = (
            # D5: a storage quota is said with its limits, never as a failure
            # that a retry would fix.
            app_quota(language, *artifact_store.quota_limits())
            if failure == "artifact_quota"
            else app_failure(
                kind,
                language,
                timeout=timed_out,
                unchanged_version=previous.version if previous is not None else None,
            )
        )
        item.state = "done"
    except (GatewayError, LimitError) as exc:
        if exc.code in BUDGET_CODES:
            raise
        _fail(item, exc.code, kind, language, previous, timed_out=False)
    except TimeoutError:
        _fail(item, "timeout", kind, language, previous, timed_out=True)
    except (
        RuntimeError,
        ValueError,
        OSError,
        aiohttp.ClientError,
        ValidationError,
    ) as exc:
        logging.getLogger(__name__).warning(
            "app_turn_failed error=%s", type(exc).__name__
        )
        _fail(item, "provider_error", kind, language, previous, timed_out=False)
    finally:
        if model is not None:
            _record(item, model)
        item.latence_ms["first_progress"] = (progress.first_status or 0.0) * 1000
        if progress.first_lines is not None:
            item.latence_ms["first_lines"] = progress.first_lines * 1000


def _fail(
    item: Interaction,
    code: str,
    kind: Kind,
    language: str,
    previous: Current | None,
    *,
    timed_out: bool,
) -> None:
    item.erreurs.append(code)
    item.reponse = app_failure(
        kind,
        language,
        timeout=timed_out,
        unchanged_version=previous.version if previous is not None else None,
    )
    item.state = "done"


def _store(
    request: ChatRequest,
    item: Interaction,
    document: str,
    title: str,
    kind: Kind,
    previous: Current | None,
) -> tuple[str, int, str] | str:
    """The stored artifact id, version and filename, or the store error code."""
    owner = getattr(request, "artifact_owner", "")
    content = document.encode("utf-8")
    try:
        if previous is not None:
            filename = previous.filename or filename_for(title, kind)
            result = artifact_store.update(
                previous.artifact_id,
                owner,
                title,
                filename,
                content,
                item.request_id,
                previous.version,
            )
        else:
            filename = filename_for(title, kind)
            result = artifact_store.create(
                owner, title, filename, content, item.request_id
            )
    except ArtifactError as error:
        logging.getLogger(__name__).warning("app_turn_store_failed code=%s", error.code)
        return error.code
    artifact_id, version = str(result["id"]), int(result["version"])
    stored = artifact_store.read(artifact_id, owner, version)
    item.artifacts = [
        {
            "id": artifact_id,
            "version": version,
            "bytes": stored.size_bytes,
            "sha256": stored.sha256,
        }
    ]
    return artifact_id, version, filename


def _record(item: Interaction, model: GatewayModel) -> None:
    item.provider_retries += model.provider_retries
    item.tokens = {"in": model.input_tokens, "out": model.output_tokens}
    item.provider_model = model.provider_model
    item.reasoning = model.reasoning
    item.trace_tokens, item.answer_tokens = model.trace_tokens, model.answer_tokens
    item.token_split_estimated = model.token_split_estimated
    item.reasoning_retried = model.reasoning_retried
    item.reasoning_effort = model.reasoning_used
    item.latence_ms["generation"] = model.generation_ms
    item.cout_eur = float(model.spent)
    if model.observations:
        item.modele_utilise = model.observations[-1].provider
        item.route_decision = model.observations[0].route
