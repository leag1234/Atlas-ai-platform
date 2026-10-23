"""Technical diagrams use text generation and the existing local Markdown viewer."""

import logging
import re
import time
from decimal import Decimal

from services.orchestrator.appearance import _normalized
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Limits, Message, Query, Result, Tools, run
from services.orchestrator.model import GatewayError, GatewayModel
from services.orchestrator.mermaid_check import FENCE, checked
from services.orchestrator.stream_client import Sink

_DIAGRAM = (
    r"(?:diagram\w*|schema\w*|flowchart\w*|schaubild\w*|esquema\w*|organigram\w*)"
)


def requested(question: str) -> bool:
    question = re.sub(r"<attached_files>.*?</attached_files>", "", question, flags=re.S)
    question = re.sub(r"^\[Attachment: [^\n]*\]$", "", question, flags=re.M)
    text = _normalized(question)
    if re.search(
        r"\b(?:picture|photo|image|illustration|portrait|poster|logo|bild|foto|imagen|immagine)\b",
        text,
    ):
        return False
    if re.search(
        r"\b(?:without|no|not|sans|pas de|ohne|kein\w*|sin|senza)\s+(?:a\s+|any\s+)?"
        + _DIAGRAM,
        text,
    ) or re.search(
        r"\b(?:do not|don't|ne .*pas|nicht|non)\s+(?:draw|dessine|zeichne|disegna)\b",
        text,
    ):
        return False
    if re.search(r"\b" + _DIAGRAM + r"\b", text):
        return True
    return bool(
        re.search(
            r"\b(?:draw|sketch|map|visually|dessine\w*|zeichne\w*|dibuja\w*|disegna\w*)\b",
            text,
        )
        and re.search(
            r"\b(?:works?|fonctionnement|fonctionne|function\w*|architecture|connections?|"
            r"flow|travel|cycle|process|workflow|ablauf|prozess|funciona\w*|funziona\w*)\b",
            text,
        )
    )


def instruction() -> str:
    return (
        "\n\nTechnical diagram delivery: explain the requested subject and include a "
        "complete fenced mermaid diagram with meaningful components and connections. "
        "The chat renders Mermaid locally. Use simple flowchart syntax and quote "
        "node labels. Do not call generate_image or link to an imagined image. "
        "A promise to draw is not a diagram."
    )


def has_diagram(text: str) -> bool:
    return bool(FENCE.search(text))


async def complete_diagram(
    request: ChatRequest,
    item: Interaction,
    result: Result,
    model: GatewayModel,
    tools: Tools,
    started: float,
    cost_limit: Decimal,
    wall_clock_limit: float,
    history: list[Message],
) -> None:
    """One bounded completion may add the missing diagram, never replace the prose."""
    from services.orchestrator.source_truth import (
        drawing_references,
        without_unretrieved,
        inline_source_markers,
        visible_sources,
    )

    def sourced(text: str) -> str:
        return inline_source_markers(
            without_unretrieved(
                text,
                set(item.retrieved_urls)
                | drawing_references(item)
                | {
                    str(chunk["source"])
                    for chunk in item.chunks_recuperes
                    if chunk.get("source")
                },
            ),
            visible_sources(item),
        )

    original = sourced(result.text)
    result.text, failures = await checked(original)
    if not failures and has_diagram(result.text):
        return
    remaining = max(Decimal(0), cost_limit - result.cost)
    seconds = max(0, wall_clock_limit - (time.monotonic() - started))
    if remaining > 0 and seconds > 0:
        model.tools = []
        model.sink = None
        try:
            completion = await run(
                Query(question=request.user_text, lang=request.lang),
                model,
                tools,
                instruction()
                + (
                    "\nRepair the invalid Mermaid blocks in the previous answer. Return only corrected fenced diagrams."
                    if failures
                    else ""
                ),
                Limits(
                    profile=request.model,
                    tokens=None,
                    tool_calls=0,
                    cost=remaining,
                    wall_clock=seconds,
                    has_attachments=request.has_attachments,
                    produces_files=model.produces_files,
                ),
                history=[*history, {"role": "assistant", "content": original}],
                retry_protocol=False,
            )
            result.cost += completion.cost
            if completion.state == "done" and completion.text:
                fixed, rejected = await checked(sourced(completion.text))
                if failures:
                    blocks = list(FENCE.finditer(fixed))
                    result.text += "\n\n" + "\n\n".join(m[0] for m in blocks)
                    failures = failures[len(blocks) :] + rejected
                else:
                    result.text += "\n\n" + fixed
                    failures = rejected
        except (GatewayError, TimeoutError):
            # Unknown provider usage stays reserved; the first answer remains usable.
            result.cost += remaining
    if failures or not has_diagram(result.text):
        item.erreurs.append("diagram_not_produced")
        notices = {
            "en": "The diagram could not be produced; the explanation above is preserved.",
            "fr": "Le diagramme n’a pas pu être produit ; l’explication ci-dessus est conservée.",
            "de": "Das Diagramm konnte nicht erstellt werden; die Erklärung oben bleibt erhalten.",
            "es": "No se pudo producir el diagrama; se conserva la explicación anterior.",
            "it": "Non è stato possibile produrre il diagramma; la spiegazione precedente è conservata.",
        }
        result.text += "\n\n" + notices.get(request.lang, notices["en"])
    logging.getLogger(__name__).info(
        "diagram_completion rendered=%s cost_eur=%s",
        has_diagram(result.text),
        result.cost,
    )


class DeliveryBuffer:
    """Hold diagrams and citation claims until delivery validation is complete."""

    def __init__(self, sink: Sink) -> None:
        self.sink = sink
        self.sent = ""
        self.held = False
        self.pending = ""

    async def __call__(self, event: dict[str, object]) -> None:
        delta = event.get("delta")
        content = delta.get("content") if isinstance(delta, dict) else None
        if not isinstance(delta, dict) or not isinstance(content, str):
            await self.sink(event)
            return
        if self.held:
            return
        self.pending += content
        match = re.search(
            r"[`~\[]|\b(?:consult|accessed|sources?\s*:)", self.pending, re.I
        )
        if match:
            self.held = True
            content = self.pending[: match.start()]
            self.pending = ""
        else:
            # Hold only a possible split provenance word; ordinary text streams immediately.
            tail = re.search(r"\b([A-Za-zé]+)[ \t]*$", self.pending)
            end = len(self.pending)
            if tail and any(
                word.startswith(tail[1].lower())
                for word in ("consult", "accessed", "source", "sources")
            ):
                end = tail.start()
            content, self.pending = self.pending[:end], self.pending[end:]
        self.sent += content
        if content:
            await self.sink({**event, "delta": {**delta, "content": content}})

    async def finish(self, text: str) -> None:
        if text.startswith(self.sent):
            await self.sink({"delta": {"content": text[len(self.sent) :]}})


async def complete_svg(
    request: ChatRequest,
    result: Result,
    model: GatewayModel,
    tools: Tools,
    started: float,
    cost_limit: Decimal,
    wall_clock_limit: float,
    *,
    require_legend: bool,
) -> None:
    """Repair one rejected drawing within the original request's remaining budget."""
    from services.orchestrator.appearance import (
        extract_svg,
        mechanical_svg_instruction,
        simple_drawing_instruction,
    )

    reasons: list[str] = []
    if extract_svg(result.text, require_legend=require_legend, reasons=reasons):
        return
    # Never retry active content. A prose-only mechanical answer may intentionally
    # delegate illustration to Commons; avoid an extra model call on that route.
    if reasons[-1] in {
        "forbidden_element",
        "forbidden_attribute",
        "external_reference",
        "byte_limit",
        "element_limit",
    } or (require_legend and reasons[-1] == "missing_svg" and result.text.strip()):
        return
    remaining = max(Decimal(0), cost_limit - result.cost)
    seconds = max(0, wall_clock_limit - (time.monotonic() - started))
    if remaining <= 0 or seconds <= 0:
        return
    model.tools, model.sink = [], None
    try:
        completion = await run(
            Query(question=request.user_text, lang=request.lang),
            model,
            tools,
            (
                mechanical_svg_instruction()
                if require_legend
                else simple_drawing_instruction()
            )
            + "\nCorrect the previous drawing. Validation category: "
            + reasons[-1]
            + ". Return a complete compact SVG and its explanation, with matching numbered legend when requested.",
            Limits(
                profile=request.model,
                tokens=None,
                tool_calls=0,
                cost=remaining,
                wall_clock=seconds,
                has_attachments=request.has_attachments,
                produces_files=model.produces_files,
            ),
            history=[{"role": "assistant", "content": result.text}],
            retry_protocol=False,
        )
        result.cost += completion.cost
        if completion.state == "done" and extract_svg(
            completion.text, require_legend=require_legend
        ):
            result.text = completion.text
            logging.getLogger(__name__).info("svg_repair rendered=true")
    except (GatewayError, TimeoutError):
        result.cost += remaining
