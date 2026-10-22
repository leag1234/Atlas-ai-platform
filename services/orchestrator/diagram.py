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
    return bool(re.search(r"(?im)^\s*```mermaid\s*\n\s*\S[\s\S]+?\n\s*```", text))


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
    if has_diagram(result.text):
        return
    remaining = max(Decimal(0), cost_limit - result.cost)
    seconds = max(0, wall_clock_limit - (time.monotonic() - started))
    if remaining > 0 and seconds > 0:
        model.tools = []
        try:
            completion = await run(
                Query(question=request.user_text, lang=request.lang),
                model,
                tools,
                instruction(),
                Limits(
                    profile=request.model,
                    tokens=None,
                    tool_calls=0,
                    cost=remaining,
                    wall_clock=seconds,
                    has_attachments=request.has_attachments,
                    produces_files=model.produces_files,
                ),
                history=[*history, {"role": "assistant", "content": result.text}],
            )
            result.cost += completion.cost
            if completion.state == "done" and completion.text:
                result.text += "\n\n" + completion.text
        except (GatewayError, TimeoutError):
            # Unknown provider usage stays reserved; the first answer remains usable.
            result.cost += remaining
    if not has_diagram(result.text):
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
