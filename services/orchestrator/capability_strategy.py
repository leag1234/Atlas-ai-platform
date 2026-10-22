"""Execute explicit visual requests before prose can deny an available capability."""

import asyncio
import json
import re
import time
import unicodedata

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call
from services.orchestrator.stream_client import sink_context
from services.orchestrator.visual_copy import copy, failure


def normalized(value: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", value.lower())
        if not unicodedata.combining(c)
    )


def relevant(question: str) -> bool:
    text = normalized(question)
    return bool(re.search(r"\bqr(?:[ -]?codes?)?\b", text)) or bool(
        re.search(r"\b(?:web|internet|online|en ligne)\b", text)
        and re.search(
            r"\b(?:images?|photos?|pictures?|posters?|bilder|fotos?|imagenes|immagini)\b",
            text,
        )
    )


def strategy(question: str) -> tuple[Call, ...] | None:
    text = normalized(question)
    if re.search(
        r"\b(?:do not|don't|never|ne .{0,30}pas|no generes|nicht|non generare)\b", text
    ):
        return None
    # Explanations and quoted instructions must stay in the normal conversation.
    if re.match(r"\s*(?:how|what|why|comment|pourquoi|explain|explique)\b", text):
        return None
    qr = bool(re.search(r"\bqr(?:[ -]?codes?)?\b", text))
    action = bool(
        re.search(
            r"\b(?:cherche|recherche|trouve|montre|affiche|genere|cree|fais|find|show|display|generate|create|make|fetch|suche|zeige|erstelle|genera|muestra|busca|mostra|trova)\b",
            text,
        )
    )
    if qr and action:
        color = (
            "blue" if re.search(r"\b(?:blue|bleu|blau|azul|blu)\b", text) else "black"
        )
        # Quoted text and standard QR URI formats are exact user payloads.
        quoted = re.findall(r'["“]([^"”]+)["”]', question)
        urls = re.findall(r"https?://[^\s<>]+", question)
        payloads = quoted or [url.rstrip(".,;") for url in urls]
        if not payloads:
            uri = re.search(
                r"(?:WIFI:|mailto:|SMSTO:|geo:|BEGIN:VCARD|BEGIN:VEVENT).+",
                question,
                re.S | re.I,
            )
            if uri:
                payloads = [uri[0]]
        if not payloads:
            precise = re.search(
                r"\b(?:pour|for|fur|para|per)\s*:?\s+(.+)$", question, re.I
            )
            if precise and not re.search(
                r"\b(?:test|tester|testing|tests|testen|pruebas|prova|provare|different|categories|categorias|kategorien|categorie)\b",
                normalized(precise[1]),
            ):
                payloads = [precise[1]]
        if payloads:
            if len(payloads) > 10:
                return None
            return tuple(
                Call(
                    f"visual-qr-{i}",
                    "make_qr",
                    json.dumps({"payload": payload, "foreground": color}),
                )
                for i, payload in enumerate(payloads)
            )
        if re.search(
            r"\b(?:varies|varied|various|different|differentes|differents|plusieurs|several|divers|test|tester|tests|testing|testen|pruebas|prova|provare|categories|categorias|kategorien|categorie)\b",
            text,
        ):
            many = bool(
                re.search(r"\b(?:many|beaucoup|nombreux|viele|muchos|molti)\b", text)
            )
            return tuple(
                Call(
                    f"visual-example-{category}",
                    "make_qr",
                    json.dumps(
                        {
                            "payload": payloads[0],
                            "category": category,
                            "additional_payloads": payloads[1:] if many else [],
                        }
                    ),
                )
                for category, payloads in qr_examples().items()
            )
    image = bool(
        re.search(
            r"\b(?:images?|photos?|pictures?|posters?|photographs?|bilder|fotos?|imagenes|immagini)\b",
            text,
        )
    )
    web = bool(re.search(r"\b(?:web|internet|online|en ligne)\b", text))
    search_action = bool(
        re.search(
            r"\b(?:cherche|recherche|trouve|montre|affiche|find|show|display|fetch|search|suche|zeige|muestra|busca|mostra|trova)\b",
            text,
        )
    )
    if search_action and image and web:
        count = 1
        numbers = {
            "one": 1,
            "une": 1,
            "two": 2,
            "deux": 2,
            "three": 3,
            "trois": 3,
            "four": 4,
            "quatre": 4,
            "five": 5,
            "cinq": 5,
            "six": 6,
        }
        match = re.search(r"\b([1-9][0-9]*|" + "|".join(numbers) + r")\b", text)
        if match:
            count = int(match[0]) if match[0].isdigit() else numbers[match[0]]
        return (
            Call(
                "visual-web",
                "web_images",
                json.dumps({"query": question, "count": min(count, 6)}),
            ),
        )
    return None


def qr_examples() -> dict[str, list[str]]:
    """Fictional, non-sensitive examples; each variant remains separately scannable."""
    return {
        "url": [f"https://example.org/visit/{i}" for i in (1, 2)],
        "text": [f"ATLAS 12345 / {i}" for i in (1, 2)],
        "wifi": [f"WIFI:T:WPA;S:Example-{i};P:example-only-{i};;" for i in (1, 2)],
        "contact": [
            f"BEGIN:VCARD\nVERSION:3.0\nFN:Alex Example {i}\nEMAIL:alex{i}@example.org\nTEL:+1202555010{i}\nEND:VCARD"
            for i in (1, 2)
        ],
        "email": [f"mailto:hello{i}@example.org" for i in (1, 2)],
        "sms": [f"SMSTO:+1202555010{i}:ATLAS {i}" for i in (1, 2)],
        "geo": [f"geo:48.858{i},2.2945" for i in (1, 2)],
        "event": [
            f"BEGIN:VEVENT\nDTSTART:2027010{i}T100000Z\nDTEND:2027010{i}T110000Z\nSUMMARY:ATLAS {i}\nEND:VEVENT"
            for i in (1, 2)
        ],
    }


async def finish(
    request: ChatRequest, item: Interaction, calls: tuple[Call, ...]
) -> None:
    from services.orchestrator.chat_pipeline import ChatTools

    runtime = ChatTools(item, request.user_text)
    runtime.daily_ledger = getattr(request, "daily_ledger", None)
    runtime.daily_user = getattr(request, "daily_user", "unknown")
    runtime.daily_lang = request.lang
    deadline = time.monotonic() + request.timeout_seconds
    examples = calls[0].id.startswith("visual-example-")
    sections = (
        [
            copy(request.lang, "categories")
            + ": "
            + ", ".join(copy(request.lang, category) for category in qr_examples()),
            copy(request.lang, "examples"),
        ]
        if examples
        else []
    )
    for call in calls:
        try:
            remaining = max(0, deadline - time.monotonic())
            async with asyncio.timeout(remaining):
                output = await runtime.execute(call, remaining)
        except TimeoutError:
            output = {"error": "visual_request_timeout"}
        if "error" in output:
            sections.append(
                failure(
                    request.lang, str(output["error"]), str(output.get("message", ""))
                )
                + (
                    f" ({output.get('provider', '')} HTTP {output['provider_status']})"
                    if output.get("provider_status")
                    else ""
                )
            )
            continue
        data = output.get("data")
        if isinstance(data, dict):
            sections.append(str(data.get("markdown", "")))
            if call.name == "web_images" and data.get("found") != data.get("requested"):
                sections.append(
                    copy(request.lang, "partial").format(
                        found=data.get("found", 0), requested=data.get("requested")
                    )
                )
    item.reponse = "\n\n".join(sections)
    item.state, item.task_type, item.cout_eur = "done", "visual_tools", 0.0
    if sink := sink_context.get():
        await sink({"delta": {"content": item.reponse}})
