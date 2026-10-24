"""Attachment-only generation, sharing the gateway's request and recovery ledger."""

from decimal import Decimal
import json
import os
from packages.ports import atlas_ports
from pathlib import Path
import time

import aiohttp

from packages.evidence import estimated_tokens
from packages.language import language_instruction
from services.orchestrator.chat_schema import ChatRequest
from packages.attachment_reader import DOCUMENT_CONTEXT_BYTES
from services.orchestrator.content import hierarchical_summary
from services.orchestrator.documents import DocumentError
from services.orchestrator.interactions import Interaction
from services.orchestrator.model import GatewayError, GatewayModel
from services.orchestrator.stream_client import sink_context


def vision_failure_notice(language: str, exc: BaseException) -> str:
    """Report a measured vision failure without discarding readable documents."""
    if isinstance(exc, GatewayError):
        reason = exc.code
        if exc.detail:
            reason += ": " + exc.detail
    elif isinstance(exc, TimeoutError):
        reason = "timeout"
    elif isinstance(exc, aiohttp.ClientError):
        reason = "network_error"
    else:
        reason = "invalid_vision_response"
    labels = {
        "fr": "Je n’ai pas pu voir l’image : l’appel de vision a échoué ({reason}).",
        "en": "I could not see the image: the vision call failed ({reason}).",
        "de": "Ich konnte das Bild nicht sehen: Der Vision-Aufruf ist fehlgeschlagen ({reason}).",
        "es": "No pude ver la imagen: falló el análisis visual ({reason}).",
        "it": "Non ho potuto vedere l'immagine: la chiamata di visione non è riuscita ({reason}).",
    }
    return labels[language].format(reason=reason)


async def process_documents(request: ChatRequest, item: Interaction) -> None:
    started = time.monotonic()
    item.task_type = "document"
    image_count = sum(len(message.images) for message in request.messages)
    sink = sink_context.get()
    if sink:
        await sink({"phase": "reading_document"})
    from services.orchestrator.attachment_reading import (
        document_status,
        failure_notice,
        reading_progress,
    )

    failures: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    for attachment in request.documents:
        try:
            from services.orchestrator.attachment_reading import (
                extract_attachment_with_ocr,
            )

            document, ocr_cost, ocr_pages = await extract_attachment_with_ocr(
                attachment,
                deadline=started + request.timeout_seconds,
                remaining_cost=Decimal("0.30") - Decimal(str(item.cout_eur)),
            )
            item.cout_eur += float(ocr_cost)
        except DocumentError as exc:
            item.documents.append(
                {
                    "filename": attachment.filename,
                    "size": exc.size,
                    "encoded_characters": len(attachment.file_data),
                    "pages": exc.pages,
                    "extracted_characters": exc.characters,
                    "hierarchical_synthesis": False,
                    "error": str(exc),
                }
            )
            failures.append({"filename": attachment.filename, "errors": [str(exc)]})
            sources.append(
                {
                    "filename": attachment.filename,
                    "text": "",
                    "pages": exc.pages,
                    "errors": [str(exc)],
                }
            )
            continue
        if document.errors:
            failures.append(
                {"filename": document.filename, "errors": list(document.errors)}
            )
        sources.append(
            {
                "filename": document.filename,
                "text": document.text,
                "pages": document.pages,
                "errors": list(document.errors),
                "ocr_pages": ocr_pages,
                "ocr_cost_eur": float(ocr_cost),
            }
        )
        if document.computed_tables:
            sources[-1]["computed_tables"] = document.computed_tables
        item.documents.append(
            {
                "filename": document.filename,
                "size": document.size,
                "pages": document.pages,
                "extracted_characters": len(document.text),
                "hierarchical_synthesis": False,
                "error": "; ".join(document.errors),
                "ocr_pages": len(ocr_pages),
                "ocr_cost_microeur": int(ocr_cost * 1000000),
            }
        )
    notice = failure_notice(failures, request.lang)
    image_notice = ""
    image_context: str | None = None
    if image_count:
        from services.orchestrator.vision import process_vision

        item.images = [
            image for message in request.messages for image in message.images
        ]
        vision_item = Interaction()
        try:
            await process_vision(request, vision_item, emit=False)
            image_context = vision_item.reponse
            item.cout_eur += vision_item.cout_eur
        except (GatewayError, TimeoutError, ValueError, aiohttp.ClientError) as exc:
            # The document remains useful when vision fails; disclose the actual
            # failed call in the caller's language instead of silently dropping it.
            item.cout_eur += vision_item.cout_eur
            image_notice = vision_failure_notice(request.lang, exc)
    pages, failed_pages, ocr_count = reading_progress(sources)
    if sink:
        await sink(
            {
                "phase": "document_read",
                "description": document_status(
                    pages, failed_pages, request.lang, ocr_count
                ),
            }
        )
    if not any(source["text"] for source in sources):
        item.reponse = "\n\n".join(
            part for part in (image_context, image_notice, notice) if part
        )
        item.state = "done"
        if sink:
            await sink({"delta": {"content": item.reponse}})
        return
    model = await GatewayModel.connect(
        os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway")),
        local_enabled=False,
    )
    model.configure_quality(request.model, request.max_tokens, has_attachments=True)
    model.spent = Decimal(str(item.cout_eur))
    model.tools = []
    model.observing = True
    model.sink = sink
    instructions = (
        Path("prompts/chat.txt").read_text()
        + Path("prompts/documents.txt").read_text()
        # Failed extraction needs a model-visible language cue for the remaining
        # readable attachments. Successful legacy requests retain their cassette bytes.
        + (language_instruction(request.lang) if notice else "")
    )
    from packages.table_arithmetic import arithmetic_instruction

    instructions += arithmetic_instruction(sources)
    history: list[dict[str, object]] = [
        {"role": m.role, "content": m.text} for m in request.messages
    ]
    model_sources: list[dict[str, object]] = [
        {
            "filename": str(source["filename"]),
            "text": str(source["text"]),
            **(
                {"computed_tables": source["computed_tables"]}
                if "computed_tables" in source
                else {}
            ),
        }
        for source in sources
    ]
    image_messages: list[dict[str, object]] = []
    if image_context is not None:
        image_messages.append(
            {
                "role": "user",
                "content": json.dumps(
                    {"untrusted_image_description": image_context},
                    ensure_ascii=False,
                ),
            }
        )
    messages: list[dict[str, object]] = [
        {"role": "system", "content": instructions},
        {
            "role": "user",
            "content": json.dumps(
                {"untrusted_attachments": model_sources}, ensure_ascii=False
            ),
        },
        *image_messages,
        *history,
    ]
    # Context estimation decides hierarchy only; it never authorizes spending.
    # The gateway independently reserves the conservative byte-based cost bound.
    measured = (
        estimated_tokens(
            json.dumps({"messages": messages, "tools": []}, ensure_ascii=False)
        )
        + 256
        + request.max_tokens
    )
    source_texts: list[str] = []
    for source in sources:
        text = source["text"]
        assert isinstance(text, str)
        source_texts.append(text)
    if measured > 262144 or sum(len(text.encode()) for text in source_texts) > 524288:
        if sink:
            await sink({"phase": "synthesizing_document"})
        for source, metadata in zip(model_sources, item.documents, strict=True):
            text = source["text"]
            assert isinstance(text, str)
            source["text"], _, _ = hierarchical_summary(
                text,
                request.messages[-1].text,
                budget=DOCUMENT_CONTEXT_BYTES // max(1, len(model_sources)),
                coverage=True,
            )
            metadata["hierarchical_synthesis"] = True
        messages[1]["content"] = json.dumps(
            {"hierarchical_attachment_evidence": model_sources}, ensure_ascii=False
        )
    if sink:
        await sink({"phase": "synthesizing_document"})
    item.cout_eur = 0.30
    try:
        if model.spent + model.estimate(messages).cost > Decimal("0.30"):
            raise GatewayError(
                "cost_budget", 413, "Document request reservation exceeds 0.30 EUR"
            )
        result = await model.complete(
            messages, max(0, request.timeout_seconds - (time.monotonic() - started))
        )
        if result.calls or not result.text.strip():
            raise GatewayError(
                "invalid_document_answer",
                502,
                "The provider returned no complete document answer",
            )
        from services.orchestrator.attachment_reading import reduced_document_note

        synthesis_notice = (
            "\n\n" + reduced_document_note(request.lang)
            if any(
                metadata.get("hierarchical_synthesis") for metadata in item.documents
            )
            else ""
        )
        item.reponse, item.state = (
            result.text
            + ("\n\n" + image_notice if image_notice else "")
            + ("\n\n" + notice if notice else "")
            + synthesis_notice,
            "done",
        )
        if image_notice and sink:
            await sink({"delta": {"content": "\n\n" + image_notice}})
        if notice and sink:
            await sink({"delta": {"content": "\n\n" + notice}})
        if synthesis_notice:
            if sink:
                await sink({"delta": {"content": synthesis_notice}})
        item.cout_eur = float(model.spent)
    finally:
        item.tokens = {"in": model.input_tokens, "out": model.output_tokens}
        item.provider_model = model.provider_model
        item.latence_ms["generation"] = model.generation_ms
        if model.observations:
            item.modele_utilise = model.observations[-1].provider
            item.route_decision = model.observations[0].route
