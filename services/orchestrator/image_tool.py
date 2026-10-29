"""Model-selected generation finishes on the same request ledger and deadline."""

from packages.gpu_ownership import gpu_allowed, image_worker_address
from decimal import Decimal
import os
from pathlib import Path
import re
from time import monotonic

from pydantic import BaseModel, ConfigDict, Field, field_validator
from packages.imagegen import ImagePrompt
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.followup import (
    image_dialogue,
    image_explanation,
    visual_image_prompt,
)
from services.orchestrator.imagegen import process_image
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Message, Result
from services.orchestrator.stream_client import sink_context
from services.orchestrator.appearance import (
    drawing_subject,
    extract_svg,
    mechanical_copy,
    mechanical_prose,
    mechanical_legend,
    numbered_legend,
    technical_prompt,
)
from services.orchestrator.commons import find_drawing
from services.orchestrator.image_store import store_svg


def images_enabled() -> bool:
    """Stable guests get image generation only after an explicit owner opt-in."""
    return (gpu_allowed() or bool(image_worker_address())) and (
        os.environ.get("ATLAS_STABLE") != "1"
        or os.environ.get("ATLAS_STABLE_IMAGES") == "1"
    )


_EXPLICIT_IMAGE_REQUEST = re.compile(
    r"\b(?:generate|create|make|draw)\s+(?:me\s+)?(?:an?\s+)?"
    r"(?:(?:very|realistic|photorealistic|artistic|detailed)\s+)*(?:portrait|picture|drawing|illustration|image|photo|poster|logo)\b|"
    r"\b(?:g[ée]n[èe]re|cr[eé]e|dessine|fais)(?:[ -]moi)?\s+(?:une?\s+)?(?:(?:tr[eè]s|r[eé]aliste|photor[eé]aliste)\s+)*(?:portrait|image|photo|illustration|affiche|dessin|logo)\b|"
    r"\b(?:erzeuge|generiere|zeichne|mach(?:e)?)\s+(?:mir\s+)?(?:ein(?:e[nsrm]?)?\s+)?"
    r"(?:\w+\s+){0,4}(?:portr[aä]t|bild|zeichnung|illustration|foto|plakat|logo)\b|"
    r"\b(?:genera|crea|dibuja|haz(?:me)?)\s+(?:una?\s+)?(?:retrato|imagen|dibujo|foto|ilustraci[oó]n|cartel|p[oó]ster|logo)\b|"
    r"\b(?:genera|crea|disegna|fammi)\s+(?:un['’ ]|una\s+)?(?:ritratto|immagine|disegno|foto|illustrazione|manifesto|poster|logo)\b",
    re.IGNORECASE,
)
_IMAGE_HOW_TO_QUESTION = re.compile(
    r"\b(?:how\s+(?:do\s+i|to)|comment|wie|c[oó]mo|come)\s+"
    r"(?:do\s+i\s+|to\s+)?(?:draw|make|create|dessiner|faire|zeichne(?:n)?|mache(?:n)?|dibujar|hacer|disegnare|fare)\b",
    re.IGNORECASE,
)
_IMAGE_SHOW_REQUEST = re.compile(
    r"\b(?:je voudrais voir|i would like to see)\s+(?:an?\s+|un(?:e)?\s+)?\S",
    re.IGNORECASE,
)

_DRAW_REQUEST = re.compile(
    r"\b(?:dessine(?:[ -]moi)?|draw(?:\s+me)?|zeichne(?:\s+mir)?|dibuja|disegna)\s+\S",
    re.IGNORECASE,
)
_DIAGRAM_SUBJECT = re.compile(
    r"\b(?:diagram\w*|sch[ée]ma\w*|chart\w*|graph\w*|grafi\w*|"
    r"organigram\w*|flowchart\w*|esquema\w*|schaubild\w*)\b",
    re.IGNORECASE,
)


def mechanical_svg_enabled() -> bool:
    """The model drawing is off for guests until the owner opts in; Commons and the legend stay."""
    return (
        os.environ.get("ATLAS_STABLE") != "1"
        or os.environ.get("ATLAS_STABLE_MECHANICAL_SVG") == "1"
    )


def explicit_image_request(question: str) -> bool:
    return bool(
        not _IMAGE_HOW_TO_QUESTION.search(question)
        and (
            _EXPLICIT_IMAGE_REQUEST.search(question)
            or _IMAGE_SHOW_REQUEST.search(question)
            or (
                (images_enabled() or os.environ.get("ATLAS_STABLE") != "1")
                and _DRAW_REQUEST.search(question)
                and not _DIAGRAM_SUBJECT.search(question)
            )
        )
    )


def unavailable_reply(language: str) -> str:
    labels = {
        "fr": "La génération d’images n’est pas disponible sur cette instance. Je peux proposer un schéma, un diagramme Mermaid ou une description textuelle.",
        "en": "Image generation is not available on this instance. I can provide a diagram, a Mermaid diagram, or a textual description instead.",
        "de": "Die Bildgenerierung ist auf dieser Instanz nicht verfügbar. Ich kann stattdessen ein Diagramm, ein Mermaid-Diagramm oder eine textliche Beschreibung anbieten.",
        "es": "La generación de imágenes no está disponible en esta instancia. Puedo ofrecer un diagrama, un diagrama Mermaid o una descripción textual.",
        "it": "La generazione di immagini non è disponibile su questa istanza. Posso invece fornire un diagramma, un diagramma Mermaid o una descrizione testuale.",
    }
    return labels[language]


def guest_capability_instruction() -> str:
    """State stable guest limits in pipeline-owned system text, not prompt artifacts."""
    if os.environ.get("ATLAS_STABLE") != "1":
        return "\n\nImage generation is unavailable on this instance. Do not offer it."
    return (
        "\n\nStable guest capability limit: image generation, file publishing, and "
        "code execution are unavailable here. Do not offer any of them."
    )


def simple_drawing_copy(language: str) -> tuple[str, str, str, str, str]:
    labels = {
        "fr": (
            "Avec plaisir — voici un petit dessin.",
            "Illustration simplifiée",
            "Télécharger le SVG",
            "Je n’ai pas pu valider ce dessin. Voulez-vous que je réessaie ?",
            "Ce SVG est une illustration simplifiée dessinée par un modèle.",
        ),
        "en": (
            "Gladly — here is a little drawing.",
            "Simplified illustration",
            "Download SVG",
            "I could not validate this drawing. Would you like me to try again?",
            "This SVG is a simplified illustration drawn by a model.",
        ),
        "de": (
            "Gern — hier ist eine kleine Zeichnung.",
            "Vereinfachte Illustration",
            "SVG herunterladen",
            "Ich konnte diese Zeichnung nicht validieren. Soll ich es noch einmal versuchen?",
            "Dieses SVG ist eine vereinfachte, von einem Modell gezeichnete Illustration.",
        ),
        "es": (
            "Con gusto — aquí tienes un pequeño dibujo.",
            "Ilustración simplificada",
            "Descargar SVG",
            "No pude validar este dibujo. ¿Quieres que lo intente de nuevo?",
            "Este SVG es una ilustración simplificada dibujada por un modelo.",
        ),
        "it": (
            "Volentieri — ecco un piccolo disegno.",
            "Illustrazione semplificata",
            "Scarica SVG",
            "Non sono riuscito a convalidare questo disegno. Vuoi che ci riprovi?",
            "Questo SVG è un’illustrazione semplificata disegnata da un modello.",
        ),
    }
    return labels.get(language, labels["en"])


class GenerateImage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    # This public tool declaration is recorded in the chat cassettes.  The
    # follow-up renderer has its own tighter bound; do not change this contract.
    prompt: str = Field(min_length=1, max_length=2000, pattern=r"\S")

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        prompt = ImagePrompt(prompt=value).prompt
        conversational = re.compile(
            r"\b(?:sorry|apologi[sz]e|cannot|can['’]t|would you|could you|"
            r"do you want|try again|r[eé]essa(?:i|y)|pouvez-vous)\b|\?",
            re.IGNORECASE,
        )
        if len(prompt) > 280 and len(conversational.findall(prompt)) >= 2:
            raise ValueError("image_prompt_conversational")
        return prompt


def declaration() -> Message:
    return {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": Path("prompts/image-tool.txt").read_text(),
            "parameters": GenerateImage.model_json_schema(),
        },
    }


async def finish(
    request: ChatRequest, item: Interaction, result: Result, prompt: str, started: float
) -> None:
    # Selection is model-owned; the original request is user-owned. A model
    # paraphrase must not silently discard constraints before the image rewriter.
    if image_explanation(request.user_text):
        item.reponse, item.state = result.text, result.state
        item.cout_eur += float(result.cost)
        return
    dialogue = image_dialogue(request.messages)
    # A first request is already the user's image instruction.  Rewriting it as a
    # visual-only prompt is reserved for an established image dialogue, where the
    # renderer must combine the original scene with a correction or retry.
    prompt = (
        request.messages[-1].text
        if len(dialogue) == 1
        else visual_image_prompt(dialogue)
    )
    try:
        await process_image(
            prompt,
            item,
            request.seed,
            language=request.lang,
            dialogue=dialogue,
            budget=max(
                Decimal(0),
                min(
                    Decimal("0.05"),
                    Decimal("0.30") - result.cost,
                ),
            ),
            timeout=max(0, min(120, request.timeout_seconds - (monotonic() - started))),
        )
        sink = sink_context.get()
        if sink:
            await sink({"phase": "tool_finished", "tool": "generate_image", "ok": True})
    finally:
        item.cout_eur += float(result.cost)


async def finish_appearance(
    request: ChatRequest, item: Interaction, result: Result, started: float
) -> None:
    """Generate first, then attach a text-model legend without preserving diagrams."""
    await process_image(
        technical_prompt(request.messages[-1].text),
        item,
        request.seed,
        language=request.lang,
        # The request-level EUR 0.10 reservation covers the bounded EUR 0.05
        # text legend plus the independent EUR 0.05 image operation.
        budget=Decimal("0.05"),
        timeout=max(0, min(120, request.timeout_seconds - (monotonic() - started))),
    )
    legend = numbered_legend(result.text, request.lang)
    item.reponse += legend
    if sink := sink_context.get():
        await sink({"delta": {"content": legend}})
    item.cout_eur += float(result.cost)


async def finish_mechanical_drawing(
    request: ChatRequest,
    item: Interaction,
    result: Result,
    *,
    fallback_only: bool = False,
) -> None:
    """Deliver a model SVG and optional retrieved Commons drawing without imagegen."""
    drawing = await find_drawing(
        drawing_subject(result.text),
        item.retrieved_urls.append,
        record_response=lambda url, response: item.commons_requests.append(
            {"url": url, "response": response}
        ),
    )
    # Quick start prefers retrieved imagery and uses a model SVG only as fallback.
    # Preserve the stable guest's explicit opt-in to a supplemental model drawing.
    # A drawing without a matching, useful legend is less honest than no drawing.
    show_svg = mechanical_svg_enabled() and (drawing is None or not fallback_only)
    svg = extract_svg(result.text, require_legend=True) if show_svg else None
    fallback_intro, alt, failure, note, source = mechanical_copy(request.lang)
    intro, _ = mechanical_prose(result.text)
    sections = [intro or fallback_intro]
    if drawing:
        item.images.append({"reference": drawing.image_url, "kind": "web_image"})
        sections.append(
            f"![{drawing.title}]({drawing.image_url})\n\n"
            f"{source}: [{drawing.title}]({drawing.page_url}) — {drawing.author}; {drawing.license}."
        )
    if svg:
        reference = store_svg(svg)
        item.images.append({"reference": reference, "kind": "mechanical_svg"})
        sections.append(f"![{alt}]({reference})")
    elif show_svg:
        sections.append(failure)
    sections.append(mechanical_legend(result.text, request.lang))
    if svg:
        sections.append(f"_{note}_")
    item.reponse = "\n\n".join(sections)
    item.state = "done"
    if sink := sink_context.get():
        await sink({"delta": {"content": item.reponse}})


async def finish_simple_drawing(
    request: ChatRequest, item: Interaction, result: Result
) -> None:
    """Deliver a guest SVG illustration without retrieval, legends, or image generation."""
    from services.orchestrator.appearance import is_spatial_drawing_request

    spatial = is_spatial_drawing_request(request.user_text)
    intro, alt, download, failure, note = simple_drawing_copy(request.lang)
    svg = extract_svg(result.text, canvas=(0.0, 0.0, 400.0, 300.0))
    if svg is None:
        item.reponse = failure
    else:
        reference = store_svg(svg)
        item.images = [{"reference": reference, "kind": "drawing_svg"}]
        if spatial:
            # Preserve the model's legend outside the validated passive SVG.
            legend = re.sub(
                r"<svg\b[^>]*>.*?</svg\s*>", "", result.text, flags=re.I | re.S
            ).strip()
            legend = re.sub(r"```(?:svg|xml)?", "", legend).strip()
            intro = legend
        item.reponse = (
            f"{intro}\n\n![{alt}]({reference})\n\n"
            f"[{download}]({reference}?download=1)\n\n_{note}_"
        )
    item.state, item.task_type = "done", "drawing"
    if sink := sink_context.get():
        await sink({"delta": {"content": item.reponse}})
