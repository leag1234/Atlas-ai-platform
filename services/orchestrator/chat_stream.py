"""Bounded producer queue and a single journal for the lifetime of an SSE response."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import json
import logging
import html
import os
from packages.ports import atlas_ports
from pathlib import Path
import re
import time
import traceback

from fastapi.responses import StreamingResponse
from packages.file_intent import produces_file, requests_file_delivery
from packages.limits import LimitError
from packages.user_errors import (
    DEADLINE_STOPS,
    stop_answer,
    ui_failure,
    unexpected_failure,
    without_diagnostics,
)

from services.orchestrator.chat_schema import ChatRequest, markdown_tail_viable
from services.orchestrator.deadline import request_deadline
from services.orchestrator.interactions import Interaction, write_interaction
from services.orchestrator.stream_client import sink_context
from services.orchestrator.loop import OwnedServerAnswer
from services.orchestrator.model import GatewayError
from services.retrieval.project_identity import ProjectError
from services.orchestrator.cancellation import (
    cancelled_by_user,
    register as register_cancellation,
    unregister as unregister_cancellation,
)
from packages import linear_scan
from packages.configuration import (
    configuration_names,
    guest_configuration_failure,
    temporarily_unavailable,
)


# The interface declares a turn interrupted after 45 s without an event of its
# stream (guest-chat.js), and an SSE comment is not one. Before the answer the
# status line is sent every second; once the answer has started, a silence of
# this length is followed by the status the interface keeps hidden, so a turn
# the service is still working on is never shown as interrupted.
ANSWER_SILENCE_SECONDS = 15.0
# The plan of the turn is sent with the status after each change, and again at
# this interval, for a page opened while the turn runs. Not every second:
# Open WebUI stores every status with the message.
PLAN_REFRESH_SECONDS = 10.0
# Atlas's own link to an attached PDF, sent before the model is called.
OPENER_PHASE = "attachment_opener"


# Unfinished Markdown at the end of the streamed text. A URL tail holds no
# space, ")", "]" or ">", so it is searched after the last of them only; a
# failed "[" or "![" reads to its first "]", which every later opening before it
# reaches too (not retried). The former searches retried every opening to the
# end of the text: quadratic on each chunk.
_TRAILING_URL = re.compile(r"(?<!\w)h(?:t(?:t(?:ps?(?::[^\s)\]>]*)?)?)?)?$")
_LAST_STOP = re.compile(r"[\s)\]>](?=[^\s)\]>]*$)")
_TRAILING_LINK = linear_scan.Scan(
    re.compile(r"\[[^\]]*$|\[[^\]]*\](?:\([^)]*)?$"),
    re.compile(r"\["),
    linear_scan.attempted,
    markdown_tail_viable(target=True, bare=True),
)
_TRAILING_IMAGE = linear_scan.Scan(
    re.compile(r"!$|!\[[^\]]*$|!\[[^\]]*\](?:\([^)]*|\[[^\]]*)?$"),
    re.compile(r"!\[|!$"),
    linear_scan.attempted,
    markdown_tail_viable(target=True, bracket=True, bare=True),
)


def _after_last_stop(text: str) -> int:
    """Where a URL tail can start: after the last stop, a final newline aside
    ("$" matches before it)."""
    end = len(text) - 1 if text.endswith("\n") else len(text)
    stop = _LAST_STOP.search(text, 0, end)
    return stop.end() if stop else 0


def response(
    payload: ChatRequest,
    item: Interaction,
    started: float,
    process: Callable[[ChatRequest, Interaction], Awaitable[None]],
) -> StreamingResponse:
    file_request = not payload.code_mode and requests_file_delivery(
        payload.user_text, include_images=False
    )
    ceiling = (
        "0.30"
        if payload.has_attachments or produces_file(payload.user_text)
        else "0.10"
    )
    display_deadline = payload.timeout_seconds
    if payload.code_mode or payload.project_action == "work":
        from services.orchestrator.code_mode import settings

        ceiling = str(settings().cost)
        display_deadline = settings().wall_clock
    from services.orchestrator.request_router import decision

    def routed_file_request() -> bool:
        """Read again once the turn is routed by the model (ATLAS_MODEL_ROUTER=1)."""
        routed = decision(payload)
        if routed is None or payload.code_mode:
            return file_request
        return routed.kind == "file"

    def routed_deadline() -> float:
        # Only a routed plain chat turn (never code mode or a Work action) reads
        # its deadline again: the decision can make it a 300 s turn.
        if decision(payload) is None or payload.code_mode:
            return display_deadline
        return payload.timeout_seconds

    guest = os.environ.get("ATLAS_STABLE") == "1"
    language = payload.status_language
    labels = json.loads(Path("prompts/progress.json").read_text())
    progress = str(labels.get(language, labels["en"])).split(":", 1)[0].strip()
    activity_labels = json.loads(Path("prompts/activity-labels.json").read_text())
    activity_labels = activity_labels.get(language, activity_labels["en"])
    if guest:
        from services.orchestrator.guest_activity import ACTIVITIES, activity_label

        activity_labels = {
            key: activity_label(language, key)
            for key in set(activity_labels) | set(ACTIVITIES)
        }

    async def generate() -> AsyncIterator[str]:
        from services.orchestrator.narration import NarrationFilter, ProtocolFilter
        from services.orchestrator.image_truth import ImageVerifier

        narration = NarrationFilter()
        protocol = ProtocolFilter()
        image_verifier = ImageVerifier()
        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue(maxsize=8)
        pending = ""
        received_content = False
        turn = 0
        activity = "reading_document" if payload.has_attachments else "thinking"
        answer_started = False
        activity_urls: list[str] = []
        activity_description = ""
        last_activity = time.monotonic()
        # A strict turn waiting for the on-demand owned server (owned_wait.py):
        # when the wait began (monotonic) and the start's expected length.
        owned_wait_since: float | None = None
        owned_expected = 0.0
        citation_numbers: dict[str, int] = {}
        # A chat turn handed to Work: its steps become the status line.
        relay_work = payload.atlas_ui and not payload.org_id and not payload.code_mode
        from services.orchestrator.work_progress import WorkProgress

        work_progress = WorkProgress(language, routed_file_request)
        from services.orchestrator import work_plan

        # The plan of a turn handed to Work or of an app (work_plan.py): drawn
        # under the status line, sent again when it changes.
        chat_plan = (
            work_plan.ChatPlan(language, str(payload.model or ""))
            if relay_work
            else None
        )
        plan_sent = (-1, 0.0)

        def plan_data() -> dict[str, object] | None:
            nonlocal plan_sent
            if chat_plan is None or not chat_plan.shown:
                return None
            now = time.monotonic()
            # Every status is stored with the message: the plan goes with the
            # status that follows a change, then every PLAN_REFRESH_SECONDS.
            if (
                chat_plan.version == plan_sent[0]
                and now - plan_sent[1] < PLAN_REFRESH_SECONDS
            ):
                return None
            plan_sent = (chat_plan.version, now)
            return chat_plan.payload()

        def track_plan(project_event: dict[str, object]) -> None:
            if chat_plan is None:
                return
            kind = project_event.get("type")
            # update_plan was checked when it ran (code_tools.py); the code
            # engine's own list (todowrite) is checked here.
            if kind == "plan" and project_event.get("tool") != "update_plan":
                output = project_event.get("output")
                items = output.get("items") if isinstance(output, dict) else None
                if isinstance(items, list):
                    chat_plan.update([i for i in items if isinstance(i, dict)])
            elif kind == "tool":
                chat_plan.first_step()
                # The check the call ran, when it ran one (publish_document's
                # file check): a check step is done only when it passed.
                output = project_event.get("output")
                check = output.get("verification") if isinstance(output, dict) else None
                chat_plan.tool(
                    str(project_event.get("tool") or ""),
                    str(project_event.get("status") or ""),
                    str(check.get("status") or "") if isinstance(check, dict) else "",
                )

        base = {
            "id": item.request_id,
            "created": int(time.time()),
            "model": payload.model,
            "object": "chat.completion.chunk",
        }

        def status_event(done: bool = False) -> dict[str, object]:
            elapsed = round(time.monotonic() - started, 1)
            # A Work step label never outlives the step: the answer is "done".
            description = (
                "" if done and activity == "work_step" else activity_description
            ) or str(activity_labels.get("done" if done else activity, activity))
            owned_waiting = (
                activity == "owned_server_starting"
                and owned_wait_since is not None
                and not done
            )
            if owned_waiting:
                from services.orchestrator.owned_wait import status_text

                assert owned_wait_since is not None
                description = status_text(
                    language, time.monotonic() - owned_wait_since, owned_expected
                )
            if (
                guest
                and not owned_waiting
                and not (
                    activity in {"artifact_progress", "work_step"}
                    and activity_description
                    and not done
                )
            ):
                from services.orchestrator.guest_activity import activity_label

                description = activity_label(language, "done" if done else activity)
            if activity_urls and not guest:
                description += " — " + ", ".join(activity_urls)
            plan = None if done else plan_data()
            return {
                "type": "status",
                "data": {
                    "description": (
                        description
                        + (
                            f" {elapsed:.0f} s"
                            if elapsed >= 10 and not done and not owned_waiting
                            else ""
                        )
                        if guest
                        else f"{description} ({elapsed:.1f}s / {routed_deadline():.0f}s)"
                    ),
                    **({} if guest else {"deadline_seconds": routed_deadline()}),
                    "done": done,
                    "elapsed_seconds": elapsed,
                    "action": "atlas_activity",
                    "hidden": done and answer_started,
                    "urls": activity_urls,
                    **({} if plan is None else {"atlas_plan": plan}),
                },
            }

        def heartbeat() -> str:
            return (
                "data: "
                + json.dumps(
                    {
                        **base,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                        "atlas": {
                            "phase": activity,
                            "elapsed_seconds": round(time.monotonic() - started, 1),
                        },
                        "event": status_event(),
                    }
                )
                + "\n\n"
            )

        def still_answering() -> str:
            # After the answer started: the same frame with the hidden status.
            return (
                "data: "
                + json.dumps(
                    {
                        **base,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                        "atlas": {
                            "phase": "answer_keepalive",
                            "elapsed_seconds": round(time.monotonic() - started, 1),
                        },
                        "event": status_event(done=True),
                    }
                )
                + "\n\n"
            )

        async def content(text: str, *, final: bool = False) -> None:
            # Retain only a possible citation suffix. Never expose an unresolved link.
            from services.orchestrator.chat_pipeline import render_citations
            from services.orchestrator.file_results import canonical_links

            if not text and not final:
                return
            # A diagnostic written for the logs never reaches the person.
            text = without_diagnostics(text)
            # Scoped processing buffers and validates the canonical answer before
            # persistence. Its source authority is unrelated to legacy project IDs.
            if payload.org_id:
                if final:
                    await queue.put(
                        {
                            "delta": {"content": text},
                            "atlas": {"turn": turn, "phase": "generating"},
                        }
                    )
                return
            pre_rendered = final and text == item.reponse
            temporary = Interaction()
            from services.orchestrator.source_truth import (
                drawing_references,
                clean_citation_residue,
                inline_source_markers,
                provider_reference_markers,
                visible_sources,
                without_unretrieved,
            )

            text = image_verifier.feed(
                text,
                {
                    str(image["reference"])
                    for image in item.images
                    if "reference" in image
                },
                final=final,
            )
            if not text:
                return
            temporary.reponse = canonical_links(text, item.files)
            if item.task_type not in {"imagegen", "vision", "visual_tools"}:
                temporary.reponse = without_unretrieved(
                    temporary.reponse,
                    set(item.retrieved_urls)
                    # Canonical API answers already contain server-resolved RAG
                    # links. Keep exactly those validated citations on re-render.
                    | {
                        os.environ.get(
                            "ATLAS_PUBLIC_URL",
                            atlas_ports().url("adapter", "localhost"),
                        ).rstrip("/")
                        + "/sources/"
                        + str(citation["chunk_id"])
                        for citation in item.citations
                    }
                    | drawing_references(item)
                    | {
                        f"/api/atlas/artifacts/{artifact['id']}/versions/"
                        f"{artifact['version']}/{action}"
                        for artifact in item.artifacts
                        if "id" in artifact and "version" in artifact
                        for action in ("open", "download")
                    }
                    | {
                        str(chunk["source"])
                        for chunk in item.chunks_recuperes
                        if chunk.get("source")
                    }
                    | {
                        u
                        for m in payload.messages[:-1]
                        if m.role == "assistant"
                        for u in re.findall(
                            re.escape(
                                os.environ.get(
                                    "ATLAS_PUBLIC_URL",
                                    atlas_ports().url("adapter", "localhost"),
                                ).rstrip("/")
                            )
                            + r"/sources/[a-f0-9]{64}",
                            m.text,
                        )
                    },
                )
                temporary.reponse = clean_citation_residue(temporary.reponse)
                # JSON finalization already numbers links before appending the
                # server's source details. Nearby also supplies numbered, named
                # links: numeric-only link labels are misparsed by Open WebUI.
                render_sources = (
                    provider_reference_markers
                    if pre_rendered or item.task_type == "nearby"
                    else inline_source_markers
                )
                temporary.reponse = render_sources(
                    temporary.reponse, visible_sources(item)
                )
            temporary.chunks_recuperes = item.chunks_recuperes
            if payload.project_id:
                from services.orchestrator.project_chat import render_project_citations

                temporary.reponse = render_project_citations(
                    temporary, payload.project_id, text
                )
            elif item.task_type != "vision":
                await render_citations(temporary)
            for citation in temporary.citations:
                key = str(citation["chunk_id"])
                citation_numbers.setdefault(key, len(citation_numbers) + 1)
            labels = {
                str(index): citation_numbers[str(c["chunk_id"])]
                for index, c in enumerate(temporary.citations, 1)
            }
            temporary.reponse = re.sub(
                r"\[Source (\d+)\]",
                lambda match: f"[Source {labels.get(match[1], match[1])}]",
                temporary.reponse,
            )
            await queue.put(
                {
                    "delta": {"content": temporary.reponse},
                    "atlas": {"turn": turn, "phase": "generating"},
                }
            )

        async def work_status(event: dict[str, object]) -> None:
            # One Work step (work_progress.py): the kind of step, never its content.
            await show_work(work_progress.step(event))

        async def show_work(step: tuple[str, str] | None) -> None:
            nonlocal activity, activity_urls, activity_description
            if step is None or step == (activity, activity_description):
                return
            activity, activity_description = step
            activity_urls = []
            # Throttled: a skipped status still reaches the next heartbeat.
            if work_progress.due(time.monotonic()):
                await queue.put(
                    {"atlas": {"phase": "work_progress"}, "event": status_event()}
                )

        async def sink(event: dict[str, object]) -> None:
            nonlocal pending, turn, activity, activity_urls, activity_description
            nonlocal received_content, owned_wait_since, owned_expected
            if event.get("phase") == "connector_approval":
                await queue.put({"atlas": event})
                return
            announced = event.get("work_step")
            if isinstance(announced, dict):
                # A Work tool call starting, or the hand-over to Work (a phase):
                # status only, never persisted or shown as a step.
                if relay_work and "phase" in announced:
                    await show_work(work_progress.phase(str(announced["phase"])))
                elif relay_work and isinstance(announced.get("writing"), dict):
                    writing = announced["writing"]
                    assert isinstance(writing, dict)
                    await show_work(
                        work_progress.writing(
                            writing.get("sections"), writing.get("words")
                        )
                    )
                elif relay_work:
                    if chat_plan is not None:
                        chat_plan.first_step()
                    await work_status(
                        {
                            "type": "tool",
                            "tool": announced.get("tool"),
                            "status": "pending",
                        }
                    )
                return
            if not payload.atlas_ui and not isinstance(event.get("project"), dict):
                # A tool turn cannot be distinguished from a final turn until it
                # completes. Emit progress separately, then the canonical answer.
                if "phase" in event:
                    await queue.put({"atlas": {"steps": [event]}})
                return
            project_event = event.get("project")
            if isinstance(project_event, dict):
                await queue.put(event)
                if relay_work:
                    track_plan(project_event)
                    await work_status(project_event)
                    if chat_plan is not None and chat_plan.version != plan_sent[0]:
                        # A step checked or added: shown now, not at the next
                        # heartbeat (none comes while events keep arriving).
                        await queue.put(
                            {"atlas": {"phase": "work_plan"}, "event": status_event()}
                        )
                return
            if payload.code_mode and isinstance(event.get("code"), dict):
                code = event["code"]
                assert isinstance(code, dict)
                chunk: dict[str, object] = {
                    "atlas": {"code": code, "phase": "tool_details"}
                }
                if code.get("type") == "tool" and code.get("status") != "running":
                    chunk["delta"] = {
                        "content": "\n\n<details><summary>"
                        + html.escape(
                            str(code.get("tool")) + " — " + str(code.get("status"))
                        )
                        + "</summary><pre>"
                        + html.escape(
                            json.dumps(code.get("output"), ensure_ascii=False)
                        )
                        + "</pre></details>\n\n"
                    }
                await queue.put(chunk)
                return
            if event.get("image_start_notice") is True:
                # This is service-owned content for API clients, not a completed
                # answer. Keep the pending status visible until the image arrives.
                await queue.put(
                    {
                        "delta": event["delta"],
                        "atlas": {"phase": "image_start_notice"},
                        "event": status_event(),
                    }
                )
                return
            if event.get("phase") in {"owned_server_starting", "owned_server_ready"}:
                # Strict privacy: the turn waits for the owned server to start
                # (owned_wait.py); the status line counts the wait, and the turn
                # is answered once the server is up.
                if event["phase"] == "owned_server_ready":
                    owned_wait_since = None
                    activity = "answering"
                else:
                    waited = event.get("waited_seconds")
                    expected = event.get("expected_seconds")
                    if owned_wait_since is None:
                        owned_wait_since = time.monotonic() - (
                            float(waited) if isinstance(waited, (int, float)) else 0.0
                        )
                    if isinstance(expected, (int, float)):
                        owned_expected = float(expected)
                    activity = "owned_server_starting"
                activity_description = ""
                await queue.put(
                    {"atlas": {"phase": event["phase"]}, "event": status_event()}
                )
                return
            if event.get("phase") == "image_progress":
                step = str(event.get("step", "generating"))
                labels = {
                    "en": ("Starting the GPU", "Generating"),
                    "fr": ("Démarrage du GPU", "Génération en cours"),
                    "de": ("GPU wird gestartet", "Bild wird generiert"),
                    "es": ("Iniciando la GPU", "Generando"),
                    "it": ("Avvio della GPU", "Generazione in corso"),
                }
                words = labels.get(language, labels["en"])
                message = (
                    str(event.get("message", ""))
                    if step == "failed"
                    else words[0 if step == "starting" else 1]
                )
                activity = "generate_image"
                activity_description = message
                await queue.put({"atlas": event, "event": status_event()})
                return
            if "phase" in event:
                phase = str(event["phase"])
                raw_urls = event.get("urls")
                raw_tools = event.get("tools")
                named_tools = raw_tools if isinstance(raw_tools, list) else []
                if phase in {"tool_started", "tool_finished"}:
                    if activity == "work_step":
                        # The Work step label belongs to the step before this tool.
                        activity_description = ""
                    activity = str(event.get("tool", "answering"))
                    if activity in {"date_time", "unit_convert", "currency_convert"}:
                        activity = "calculator"
                    if activity in {
                        "encyclopedia",
                        "news_headlines",
                        "eu_statistics",
                        "french_commune",
                    }:
                        # A specialized source is read like a page.
                        activity = "web_fetch"
                    activity_urls = (
                        [str(url) for url in raw_urls]
                        if isinstance(raw_urls, list)
                        else []
                    )
                elif phase in {
                    "reasoning_fallback",
                    "provider_fallback",
                    "synthesized",
                    "source_truncated",
                    "reading_document",
                    "document_read",
                    "synthesizing_document",
                }:
                    activity = phase
                    description = event.get("description")
                    activity_description = (
                        str(description) if isinstance(description, str) else ""
                    )
                    if phase == "synthesizing_document":
                        synthesis = {
                            "fr": "Synthèse en cours…",
                            "en": "Synthesis in progress…",
                            "de": "Zusammenfassung läuft…",
                            "es": "Síntesis en curso…",
                            "it": "Sintesi in corso…",
                        }
                        activity_description = synthesis.get(language, synthesis["en"])
                elif phase == "narration":
                    activity = "thinking"
                elif phase == "artifact_progress":
                    # Service-owned progress of an app or drawing turn ("Writing
                    # the app… 120 lines"): the file itself is never streamed.
                    activity = "artifact_progress"
                    description = event.get("description")
                    activity_description = (
                        str(description) if isinstance(description, str) else ""
                    )
                elif phase == "generating":
                    activity = (
                        "thinking"
                        if payload.reasoning_effort == "high"
                        else "answering"
                    )
                    activity_urls = []
                    activity_description = ""
                if event["phase"] != "generating":
                    await content(pending)
                    pending = ""
                if event["phase"] == "intermediate":
                    await queue.put(
                        {
                            "delta": {
                                "content": "\n\n<details>\n<summary>"
                                + html.escape(
                                    progress
                                    + ": "
                                    + ", ".join(
                                        str(
                                            activity_labels.get(
                                                str(tool), activity_labels["thinking"]
                                            )
                                        )
                                        for tool in named_tools
                                        if isinstance(tool, str)
                                    )
                                )
                                + "</summary>\n\n</details>\n\n"
                            },
                            "atlas": {"turn": turn, "phase": "tool_details"},
                        }
                    )
                if phase == "tool_finished" and activity != "generate_image":
                    links = "<br>".join(html.escape(url) for url in activity_urls)
                    caveat: object = event.get("message")
                    outcome = event.get("outcome")
                    if caveat and isinstance(outcome, str):
                        from services.orchestrator.chat_pipeline import (
                            search_failure_notice,
                        )
                        from services.orchestrator.searxng import OUTCOMES

                        # The search service's own caveat is English: a French
                        # turn showed it untranslated (staging corpus 2026-11-11).
                        if outcome in OUTCOMES:
                            caveat = search_failure_notice(outcome, language)
                    if caveat:
                        links = html.escape(str(caveat)) + "<br>" + links
                    await queue.put(
                        {
                            "delta": {
                                "content": "\n\n<details>\n<summary>"
                                + html.escape(
                                    str(
                                        activity_labels.get(
                                            activity, activity_labels["thinking"]
                                        )
                                    )
                                )
                                + "</summary>\n"
                                + links
                                + "\n</details>\n\n"
                            },
                            "atlas": {"phase": "tool_details", "tool": activity},
                        }
                    )
                if event["phase"] == "generating":
                    citation_numbers.clear()
                turn = int(str(event.get("turn", turn)))
                await queue.put(
                    {
                        "atlas": {
                            **event,
                            "reasoning_effort": payload.reasoning_effort,
                        },
                        "event": status_event(),
                    }
                )
                return
            delta = event.get("delta")
            if not isinstance(delta, dict):
                raise ValueError("invalid_delta")
            if isinstance(delta.get("content"), str) and delta["content"]:
                received_content = True
            if "reasoning_content" in delta:
                activity = "thinking"
                await queue.put(event)
            if routed_file_request():
                return
            if isinstance(delta.get("content"), str):
                count = len(narration.removed)
                text = protocol.feed(narration.feed(delta["content"]))
                if len(narration.removed) > count:
                    activity = "thinking"
                    await queue.put(
                        {"atlas": {"phase": "narration"}, "event": status_event()}
                    )
                delta = {**delta, "content": text}
            if "content" in delta and (
                event.get("memory") is True
                or item.task_type
                in {
                    "imagegen",
                }
            ):
                await queue.put({"delta": {"content": delta["content"]}})
                return
            if "content" in delta:
                activity = "answering"
                pending += str(delta["content"])
                suffix = re.search(r"(?<!\w)[`\[]?[a-f0-9]{1,64}$|[`\[]$", pending)
                end = suffix.start() if suffix else len(pending)
                # A URL may cross provider chunks, including the scheme itself.
                trailing = _TRAILING_URL.search(pending, _after_last_stop(pending))
                if trailing:
                    end = min(end, trailing.start())
                link = _TRAILING_LINK.search(pending)
                if link:
                    end = min(end, link.start())
                image_link = _TRAILING_IMAGE.search(pending)
                if image_link:
                    end = min(end, image_link.start())
                # Web citations can start without a scheme and continue after a
                # typed reference frame. Validate complete lines before emitting
                # irreversible SSE text, using the same renderer as JSON.
                if item.retrieved_urls or "[[atlas-reference:" in pending:
                    end = min(end, pending.rfind("\n") + 1)
                await content(pending[:end])
                pending = pending[end:]

        async def produce() -> None:
            token = sink_context.set(sink)
            plan_token = work_plan.current.set(chat_plan)
            try:
                async with request_deadline(
                    max(0, payload.timeout_seconds - (time.monotonic() - started))
                ):
                    from services.orchestrator.side_view import pdf_placeholders

                    opener = pdf_placeholders(payload)
                    if opener:
                        # Atlas's own text, not the model's: it bypasses the answer
                        # filters, so an answer completed without streamed text (a
                        # document read in one call) is still sent in full. It is
                        # not the start of the answer either: the status line goes
                        # on until the model's text. Counted as the answer, it
                        # stopped every event of a turn whose document took more
                        # than 45 s to read, and the interface showed the turn as
                        # interrupted and stopped it (staging 2026-11-12).
                        await queue.put(
                            {
                                "delta": {"content": opener},
                                "atlas": {"turn": turn, "phase": OPENER_PHASE},
                            }
                        )
                    await process(payload, item)
                    # A Work turn that failed for a known reason carries its own
                    # localized explanation; it is a finished answer, not an outage.
                    work_failed = (
                        payload.project_action == "work"
                        and item.state == "failed"
                        and bool(item.work_failure)
                    )
                    limit_refusal = (
                        item.state != "done"
                        and bool(item.erreurs)
                        and any(
                            error in {"output_budget", "cost", "cost_budget"}
                            for error in item.erreurs
                        )
                    )
                    if limit_refusal:
                        from services.orchestrator.code_mode import WORK_FAILURE

                        words = WORK_FAILURE.get(language, WORK_FAILURE["en"])
                        item.reponse = str(words["budget"])
                    elif item.state != "done" and not work_failed:
                        from packages.user_errors import PROVIDER_FAILURES

                        # A model answer that failed is said as such (D5),
                        # not as an unexpected error (staging 2026-11-17).
                        # A Work turn keeps its outage wording
                        # (test_work_failure_message); a chat turn says the
                        # model answer failed.
                        failed = [e for e in item.erreurs if e in PROVIDER_FAILURES]
                        if failed and payload.project_action != "work":
                            raise GatewayError(
                                failed[-1], 502, item.reponse.strip() or failed[-1]
                            )
                        # A stop of the tool loop is not an unexpected error: the
                        # deadline is said as the deadline (D5), a research stop
                        # in one sentence, as a finished answer. The stop summary
                        # stays in the logs (staging -139, 2026-11-20).
                        stops = [
                            e
                            for e in item.erreurs
                            if e in DEADLINE_STOPS or stop_answer(e, "en")
                        ]
                        if stops and payload.project_action != "work":
                            logging.getLogger(__name__).warning(
                                json.dumps(
                                    {"event": "stopped_turn", "reason": stops[-1]}
                                )
                            )
                            if stops[-1] in DEADLINE_STOPS:
                                raise GatewayError("timeout", 504, "timeout")
                            item.reponse = str(
                                stop_answer(stops[-1], payload.answer_language)
                            )
                        else:
                            raise RuntimeError("request_stopped")
                    # The checked plan ends the answer (work_plan.py); the
                    # stored answer keeps it too.
                    plan_text = (
                        chat_plan.markdown()
                        if chat_plan is not None
                        and chat_plan.finished
                        and chat_plan.shown
                        else ""
                    )
                    item.reponse += plan_text
                    cleaned = without_diagnostics(item.reponse)
                    if cleaned != item.reponse:
                        logging.getLogger(__name__).warning(
                            json.dumps({"event": "diagnostic_withheld"})
                        )
                        item.reponse = cleaned or str(
                            stop_answer(
                                "protocol_completion_limit", payload.answer_language
                            )
                        )
                    if not payload.atlas_ui and not payload.code_mode:
                        await content(item.reponse, final=True)
                    elif payload.code_mode:
                        await queue.put({"delta": {"content": item.reponse}})
                    else:
                        await content(
                            item.reponse
                            if routed_file_request() or not received_content
                            else pending
                            + protocol.feed(narration.finish())
                            + protocol.finish()
                            + plan_text,
                            final=True,
                        )
                    if (
                        payload.atlas_ui
                        and not payload.code_mode
                        and item.files
                        and (not routed_file_request() or not item.reponse)
                    ):
                        from services.orchestrator.file_results import file_footer
                        from packages.language import conversation_language

                        await content(
                            file_footer(
                                item.files,
                                item.file_strategies,
                                conversation_language(
                                    payload.conversation_messages, payload.ui_locale
                                ),
                            )
                        )
                await queue.put({"finish": True})
            except asyncio.CancelledError:
                item.state = "cancelled"
                if cancelled_by_user(
                    str(payload.cancel_id) if payload.cancel_id is not None else None
                ):
                    item.state = "cancelled_by_user"
                    item.erreurs.append("cancelled_by_user")
                    item.rejection = {
                        "code": "cancelled_by_user",
                        "message": "Stopped by user.",
                    }
                else:
                    item.erreurs.append("cancelled")
                raise
            except ProjectError as exc:
                from services.orchestrator.project_transcript import failure_location

                logging.getLogger(__name__).warning(
                    "project_stream_failed code=%s status=%s at=%s",
                    exc.body["error"],
                    exc.status,
                    failure_location(exc),
                )
                item.state = "error"
                item.erreurs.append(str(exc.body["error"]))
                await queue.put({"error": exc.body})
            except TimeoutError:
                item.state = "error"
                item.erreurs.append("stream_error")
                detail = f"Timeout: {time.monotonic() - started:.3f} seconds elapsed, limit {payload.timeout_seconds + item.startup_seconds:.3f} seconds"
                item.rejection = {"code": "timeout", "message": detail}
                await queue.put(
                    {
                        "error": {
                            "code": "stream_error",
                            "type": "stream_error",
                            "message": (
                                ui_failure("timeout", payload.answer_language)
                                if payload.atlas_ui
                                else None
                            )
                            or detail,
                        }
                    }
                )
            except GatewayError as exc:
                item.provider_retries += exc.retries
                item.state = "error"
                item.erreurs.append(exc.code)
                detail = (
                    temporarily_unavailable(payload.answer_language)
                    if guest_configuration_failure(exc)
                    else exc.detail or exc.code
                )
                readable = (
                    ui_failure(exc.code, payload.answer_language, exc.measurements)
                    if payload.atlas_ui
                    else None
                )
                if guest_configuration_failure(exc):
                    logging.getLogger(__name__).error(
                        "Configuration failure; variables=%s", configuration_names(exc)
                    )
                item.rejection = {"code": exc.code, "message": detail}
                if readable and isinstance(exc, OwnedServerAnswer):
                    # The owned server's real outcome (refused, failed, or still
                    # starting after the wait) is the turn's answer, in the
                    # person's language: sent as an error, the interface showed
                    # it under "The response was interrupted." with a Retry
                    # button (staging 2026-11-20).
                    item.reponse = readable
                    await queue.put(
                        {
                            "delta": {"content": readable},
                            "atlas": {"turn": turn, "phase": "owned_server_answer"},
                        }
                    )
                    await queue.put({"finish": True})
                else:
                    await queue.put(
                        {
                            "error": {
                                "code": exc.code,
                                "type": exc.code,
                                "message": readable or detail,
                            }
                        }
                    )
            except LimitError as exc:
                item.state = "error"
                item.erreurs.append(exc.code)
                item.rejection = {"code": exc.code, "message": exc.detail}
                await queue.put(
                    {
                        "error": {
                            "code": exc.code,
                            "type": exc.code,
                            "message": exc.detail,
                        }
                    }
                )
            except Exception as exc:
                item.state = "error"
                item.erreurs.append("stream_error")
                if guest_configuration_failure(exc):
                    detail = temporarily_unavailable(payload.answer_language)
                    logging.getLogger(__name__).error(
                        "Configuration failure; variables=%s", configuration_names(exc)
                    )
                elif payload.project_id:
                    detail = temporarily_unavailable(payload.answer_language)
                else:
                    detail = (
                        "The request did not complete because of an unexpected failure "
                        f"({type(exc).__name__}), which is not a budget or deadline refusal. "
                        f"Elapsed {time.monotonic() - started:.3f}s "
                        f"(limit {payload.timeout_seconds:.0f}s); "
                        f"reserved cost {item.cout_eur:.6f} EUR (limit {ceiling} EUR)."
                    )
                item.rejection = {"code": "stream_error", "message": detail}
                if (
                    payload.atlas_ui
                    and not payload.project_id
                    and not guest_configuration_failure(exc)
                ):
                    # The diagnostic stays in the record and the logs; the
                    # person reads one sentence in their language.
                    detail = unexpected_failure(payload.answer_language)
                frames = traceback.extract_tb(exc.__traceback__)
                logging.getLogger(__name__).error(
                    "Unexpected stream exception %s; traceback:\n%s",
                    type(exc).__name__,
                    "\n".join(
                        f"  {frame.filename}:{frame.lineno} in {frame.name}"
                        for frame in frames
                    ),
                )
                await queue.put(
                    {
                        "error": {
                            "code": "stream_error",
                            "type": "stream_error",
                            "message": detail,
                        }
                    }
                )
            finally:
                work_plan.current.reset(plan_token)
                sink_context.reset(token)
            await queue.put(None)

        task = asyncio.create_task(produce())
        cancel_id = str(payload.cancel_id) if payload.cancel_id is not None else None
        if cancel_id is not None:
            register_cancellation(cancel_id, task)
        last_sent = time.monotonic()
        try:
            yield heartbeat()
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1)
                except TimeoutError:
                    # A direct user cancellation can interrupt the producer while
                    # it is finalizing, before it can enqueue the sentinel.
                    # Never leave the SSE response (and its budget finally) orphaned.
                    if task.done():
                        break
                    last_activity = time.monotonic()
                    if not answer_started:
                        last_sent = last_activity
                        yield heartbeat()
                    elif last_activity - last_sent >= ANSWER_SILENCE_SECONDS:
                        last_sent = last_activity
                        yield still_answering()
                    else:
                        yield ": keepalive\n\n"
                    continue
                if event is None:
                    break
                if isinstance(event.get("project"), dict):
                    project_event = event["project"]
                    assert isinstance(project_event, dict)
                    yield (
                        "event: atlas.project\nid: "
                        + str(project_event["seq"])
                        + "\ndata: "
                        + json.dumps(project_event)
                        + "\n\n"
                    )
                    continue
                if "error" in event:
                    metadata = event.get("atlas")
                    output = {
                        **event,
                        "atlas": {
                            **(metadata if isinstance(metadata, dict) else {}),
                            "context": item.context_samples[-1]
                            if item.context_samples
                            else None,
                            "context_samples": item.context_samples,
                            "context_manifest": item.context_manifest,
                        },
                    }
                else:
                    output = {
                        **base,
                        "choices": [
                            {
                                "index": 0,
                                "delta": event.get("delta", {}),
                                "finish_reason": "stop"
                                if event.get("finish")
                                else None,
                            }
                        ],
                        "atlas": event.get(
                            "atlas", {"turn": turn, "phase": "generating"}
                        ),
                    }
                    if event.get("finish"):
                        output["usage"] = {
                            "prompt_tokens": item.tokens["in"],
                            "completion_tokens": item.tokens["out"],
                            "total_tokens": sum(item.tokens.values()),
                        }
                        from services.orchestrator.source_truth import visible_sources

                        output["atlas"] = {
                            "steps": item.steps,
                            "sources": visible_sources(item),
                            "context": item.context_samples[-1]
                            if item.context_samples
                            else None,
                            "context_samples": item.context_samples,
                            "context_manifest": item.context_manifest,
                            "images": item.images,
                            "files": item.files,
                            "terminal_commands": item.terminal_commands,
                            "terminal_failed_commands": item.terminal_failed_commands,
                            "uploaded_images": item.uploaded_images,
                            **(
                                {"connector_approvals": payload._connector_approvals}
                                if payload._connector_approvals
                                else {}
                            ),
                            "cost_eur": item.cout_eur,
                            "provider_model": item.provider_model,
                            "reasoning_effort": item.reasoning_effort,
                            "max_output_tokens": payload.max_tokens,
                            "reasoning_retried": item.reasoning_retried,
                            "status": activity_labels["reasoning_fallback"]
                            if item.reasoning_retried
                            else "",
                            "reasoning": {
                                "collapsed": True,
                                "trace_tokens": item.trace_tokens,
                                "answer_tokens": item.answer_tokens,
                                "estimated": item.token_split_estimated,
                            },
                        }
                delta = event.get("delta")
                event_metadata = event.get("atlas")
                event_phase = (
                    event_metadata.get("phase")
                    if isinstance(event_metadata, dict)
                    else None
                )
                if event_phase in {"tool_started", "intermediate"}:
                    answer_started = False
                first_content = (
                    not answer_started
                    and isinstance(delta, dict)
                    and bool(delta.get("content"))
                    and event_phase
                    not in {"tool_details", "image_start_notice", OPENER_PHASE}
                )
                if first_content:
                    answer_started = True
                    output["event"] = status_event(done=True)
                elif event.get("finish"):
                    output["event"] = status_event(done=True)
                elif not answer_started and (
                    "atlas" in event
                    or event.get("finish")
                    or time.monotonic() - last_activity >= 1
                ):
                    # Built only when the event has none: building a status sends
                    # the plan along (plan_data).
                    output["event"] = (
                        event["event"]
                        if "event" in event
                        else status_event(bool(event.get("finish")))
                    )
                    last_activity = time.monotonic()
                last_sent = time.monotonic()
                yield "data: " + json.dumps(output) + "\n\n"
            yield "data: [DONE]\n\n"
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if cancel_id is not None:
                unregister_cancellation(cancel_id)
            item.latence_ms["total"] = (time.monotonic() - started) * 1000
            write_interaction(
                item,
                Path(os.environ.get("ATLAS_INTERACTION_DIR", "BRAIN/interactions")),
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
