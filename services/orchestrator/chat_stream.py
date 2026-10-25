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

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.deadline import request_deadline
from services.orchestrator.interactions import Interaction, write_interaction
from services.orchestrator.stream_client import sink_context
from services.orchestrator.model import GatewayError
from services.retrieval.project_identity import ProjectError
from services.orchestrator.cancellation import (
    cancelled_by_user,
    register as register_cancellation,
    unregister as unregister_cancellation,
)
from packages.configuration import (
    configuration_names,
    guest_configuration_failure,
    temporarily_unavailable,
)


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
    language = payload.status_language
    labels = json.loads(Path("prompts/progress.json").read_text())
    progress = str(labels.get(language, labels["en"])).split(":", 1)[0].strip()
    activity_labels = json.loads(Path("prompts/activity-labels.json").read_text())
    activity_labels = activity_labels.get(language, activity_labels["en"])

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
        citation_numbers: dict[str, int] = {}
        base = {
            "id": item.request_id,
            "created": int(time.time()),
            "model": payload.model,
            "object": "chat.completion.chunk",
        }

        def status_event(done: bool = False) -> dict[str, object]:
            elapsed = round(time.monotonic() - started, 1)
            description = activity_description or str(
                activity_labels.get("done" if done else activity, activity)
            )
            if activity_urls:
                description += " — " + ", ".join(activity_urls)
            return {
                "type": "status",
                "data": {
                    "description": f"{description} ({elapsed:.1f}s / {display_deadline:.0f}s)",
                    "deadline_seconds": display_deadline,
                    "done": done,
                    "elapsed_seconds": elapsed,
                    "action": "atlas_activity",
                    "hidden": done and answer_started,
                    "urls": activity_urls,
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

        async def content(text: str, *, final: bool = False) -> None:
            # Retain only a possible citation suffix. Never expose an unresolved link.
            from services.orchestrator.chat_pipeline import render_citations
            from services.orchestrator.file_results import canonical_links

            if not text and not final:
                return
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
                    | drawing_references(item)
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
                # server's source details. Do not rewrite that footer as inline links.
                render_sources = (
                    provider_reference_markers
                    if pre_rendered
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

        async def sink(event: dict[str, object]) -> None:
            nonlocal pending, turn, activity, activity_urls, activity_description
            nonlocal received_content
            if isinstance(event.get("project"), dict):
                await queue.put(event)
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
                    activity = str(event.get("tool", "answering"))
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
                                        str(activity_labels.get(str(tool), tool))
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
                    if event.get("message"):
                        links = html.escape(str(event["message"])) + "<br>" + links
                    await queue.put(
                        {
                            "delta": {
                                "content": "\n\n<details>\n<summary>"
                                + html.escape(
                                    str(activity_labels.get(activity, activity))
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
            if file_request:
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
                trailing = re.search(
                    r"(?<!\w)h(?:t(?:t(?:ps?(?::[^\s)\]>]*)?)?)?)?$", pending
                )
                if trailing:
                    end = min(end, trailing.start())
                link = re.search(r"\[[^\]]*$|\[[^\]]*\](?:\([^)]*)?$", pending)
                if link:
                    end = min(end, link.start())
                image_link = re.search(
                    r"!$|!\[[^\]]*$|!\[[^\]]*\](?:\([^)]*|\[[^\]]*)?$", pending
                )
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
            try:
                async with request_deadline(
                    max(0, payload.timeout_seconds - (time.monotonic() - started))
                ):
                    from services.orchestrator.side_view import pdf_placeholders

                    opener = pdf_placeholders(payload)
                    if opener:
                        await sink({"delta": {"content": opener}})
                    await process(payload, item)
                    if item.state != "done":
                        raise RuntimeError("request_stopped")
                    if payload.code_mode:
                        await queue.put({"delta": {"content": item.reponse}})
                    else:
                        await content(
                            item.reponse
                            if file_request or not received_content
                            else pending
                            + protocol.feed(narration.finish())
                            + protocol.finish(),
                            final=True,
                        )
                    if (
                        not payload.code_mode
                        and item.files
                        and (not file_request or not item.reponse)
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
                            "message": detail,
                        }
                    }
                )
            except GatewayError as exc:
                item.provider_retries += exc.retries
                item.state = "error"
                item.erreurs.append(exc.code)
                detail = (
                    temporarily_unavailable(payload.lang)
                    if guest_configuration_failure(exc)
                    else exc.detail or exc.code
                )
                if guest_configuration_failure(exc):
                    logging.getLogger(__name__).error(
                        "Configuration failure; variables=%s", configuration_names(exc)
                    )
                item.rejection = {"code": exc.code, "message": detail}
                await queue.put(
                    {
                        "error": {
                            "code": exc.code,
                            "type": exc.code,
                            "message": detail,
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
                    detail = temporarily_unavailable(payload.lang)
                    logging.getLogger(__name__).error(
                        "Configuration failure; variables=%s", configuration_names(exc)
                    )
                else:
                    detail = (
                        "The request did not complete because of an unexpected failure "
                        f"({type(exc).__name__}), which is not a budget or deadline refusal. "
                        f"Elapsed {time.monotonic() - started:.3f}s "
                        f"(limit {payload.timeout_seconds:.0f}s); "
                        f"reserved cost {item.cout_eur:.6f} EUR (limit {ceiling} EUR)."
                    )
                item.rejection = {"code": "stream_error", "message": detail}
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
                sink_context.reset(token)
            await queue.put(None)

        task = asyncio.create_task(produce())
        cancel_id = str(payload.cancel_id) if payload.cancel_id is not None else None
        if cancel_id is not None:
            register_cancellation(cancel_id, task)
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
                        yield heartbeat()
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
                    output = event
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
                        output["atlas"] = {
                            "images": item.images,
                            "files": item.files,
                            "terminal_commands": item.terminal_commands,
                            "terminal_failed_commands": item.terminal_failed_commands,
                            "uploaded_images": item.uploaded_images,
                            "cost_eur": item.cout_eur,
                            "provider_model": item.provider_model,
                            "reasoning_effort": payload.reasoning_effort,
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
                    and event_phase not in {"tool_details", "image_start_notice"}
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
                    output["event"] = event.get(
                        "event", status_event(bool(event.get("finish")))
                    )
                    last_activity = time.monotonic()
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
