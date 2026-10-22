"""Bounded answer-only content recovery before deterministic file rendering."""

import asyncio
from decimal import Decimal
import logging
import re
import time

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.file_content import folded, presentable
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Message, Result
from services.orchestrator.model import GatewayError, GatewayModel
from services.orchestrator.source_truth import without_unretrieved


async def recover_content(
    model: GatewayModel,
    request: ChatRequest,
    item: Interaction,
    result: Result,
    formats: tuple[str, ...],
    started: float,
) -> bool:
    """Use at most two content turns, reserving time for rendering and publication."""
    original = result.text
    messages: list[Message] = [
        {"role": "user", "content": request.messages[-1].text},
        {"role": "assistant", "content": original},
    ]
    instruction = (
        "Write the FULL CONTENT of the requested deliverable now as plain structured "
        f"text in language {request.lang}. Use Markdown section titles and concrete "
        "bullet points: at least three substantive sections for a presentation. "
        "Write what the audience should read, not an outline of what you would write. "
        "Tools are disabled. Do not generate code, claim a file exists, apologize, "
        "or promise any future action. Use available knowledge cautiously; mark facts "
        "not verified by research as recalled and uncertain where appropriate. "
        "If earlier searches failed or were irrelevant, disclose that in the requested "
        "language without inventing a technical reason. Do not invent sources or URLs."
    )
    saved_tools, saved_choice, saved_sink = (
        model.tools,
        model.required_file_tool,
        model.sink,
    )
    model.tools, model.required_file_tool, model.sink = [], None, None
    try:
        for attempt in range(2):
            remaining = request.timeout_seconds - (time.monotonic() - started) - 30
            available = max(Decimal(0), Decimal("0.30") - result.cost)
            if remaining <= 0 or available <= 0:
                break
            model.spent = max(model.spent, result.cost)
            # Unknown usage can consume this turn's allowance, never the next one.
            model.delivery_reserve = available / 2 if attempt == 0 else Decimal(0)
            allowance = model.allowance
            messages.append({"role": "system", "content": instruction})
            logging.getLogger(__name__).info(
                "file_content_turn attempt=%s", attempt + 1
            )
            try:
                async with asyncio.timeout(remaining):
                    turn = await model.complete(
                        list(messages), remaining / (2 - attempt)
                    )
            except (GatewayError, RuntimeError, TimeoutError):
                result.cost += allowance
                model.spent = max(model.spent, result.cost)
                item.erreurs.append("file_content_turn_failed")
                break
            result.cost += turn.usage.cost if turn.usage else allowance
            text = without_unretrieved(turn.text, set(item.retrieved_urls))
            if not turn.calls and presentable(text, formats, structured=True):
                # Preserve a research limitation even if the content turn omits it.
                if re.search(
                    r"(?:recherch|search|suche|busqu|ricerc).{0,240}(?:echou|failed|irrelevant|non pertinent|no relevant|fehl|fall)",
                    folded(original),
                    re.S,
                ):
                    notices = {
                        "fr": "Les recherches n'ont pas permis de vérifier ce contenu. Les informations ci-dessus reposent sur des connaissances mémorisées, à confirmer.",
                        "en": "Searches did not verify this content. The information above relies on recalled knowledge and needs confirmation.",
                        "de": "Die Recherche konnte diesen Inhalt nicht bestätigen. Die Angaben beruhen auf gespeichertem Wissen und müssen überprüft werden.",
                        "es": "Las búsquedas no permitieron verificar este contenido. La información se basa en conocimientos previos y necesita confirmación.",
                        "it": "Le ricerche non hanno verificato questo contenuto. Le informazioni si basano su conoscenze pregresse e richiedono conferma.",
                    }
                    text += "\n\n" + notices[request.lang]
                result.text = text
                return True
            messages.append({"role": "assistant", "content": turn.text})
            instruction += (
                " The previous content was REJECTED. Replace it completely with finished "
                "content: distinct titled sections, each with at least two informative "
                "bullet points. No plan, apology, file claim, or future action is acceptable."
            )
        return False
    finally:
        model.tools, model.required_file_tool, model.sink = (
            saved_tools,
            saved_choice,
            saved_sink,
        )
        model.delivery_reserve = Decimal(0)
