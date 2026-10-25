"""Exact-name discovery: retrieved excerpts, never a substituted biography."""

import json
import re
import time
from html import escape
from typing import TYPE_CHECKING

from services.orchestrator.loop import Call
from services.orchestrator.model import GatewayError
from packages.turn_attachments import normalized

if TYPE_CHECKING:
    from services.orchestrator.chat_pipeline import ChatTools
    from services.orchestrator.chat_schema import ChatRequest
    from services.orchestrator.interactions import Interaction


_NAME = r"([^\W\d_][\w'’.-]*(?:\s+[^\W\d_][\w'’.-]*){1,3})"


def person_name(question: str) -> str | None:
    text = question.strip()
    patterns = (
        r"(?:who is|qui est|wer ist|quien es|chi e)\s+" + _NAME + r"\s*[?.!]*$",
        r"(?:tell me about|parle.moi de)\s+"
        + _NAME
        + r"\s*(?:,\s*who is this person\??)?[?.!]*$",
        _NAME + r"\s+(?:biography|biographie)\s*[?.!]*$",
        r"find\s+" + _NAME + r"['’]s\s+work[?.!]*$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text, re.I)
        if match:
            name = match[1]
            # Do not reinterpret ordinary topics/pronouns as people.
            if any(
                word.casefold()
                in {"the", "a", "an", "this", "that", "my", "your", "le", "la"}
                for word in name.split()
            ):
                return None
            return name
    return None


def queries(name: str, language: str) -> tuple[Call, ...]:
    context = {
        "en": "biography professional profile",
        "fr": "biographie profil professionnel",
        "de": "Biografie berufliches Profil",
        "es": "biografia perfil profesional",
        "it": "biografia profilo professionale",
    }
    return tuple(
        Call(
            f"person-{i}", "web_search", json.dumps({"query": query, "lang": language})
        )
        for i, query in enumerate(
            (f'"{name}"', f'"{name}" {context.get(language, context["en"])}')
        )
    )


async def lookup(
    request: "ChatRequest", item: "Interaction", tools: "ChatTools", name: str
) -> None:
    started = time.monotonic()
    evidence: dict[str, str] = {}
    searched = []
    failures = []
    for call in queries(name, request.lang):
        searched.append(json.loads(call.arguments)["query"])
        try:
            result = await tools.execute(
                call, max(0.01, request.timeout_seconds - (time.monotonic() - started))
            )
        except GatewayError as exc:
            failures.append(exc.code)
            continue
        data = result.get("data")
        rows = data.get("results", []) if isinstance(data, dict) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = row.get("link")
            # A full-name mention is evidence of a match, not proof that namesakes
            # are the same person. Quote only the retrieved passage containing it.
            passage = str(
                row.get("snippet") or row.get("content") or row.get("title") or ""
            )
            sentences = re.split(r"(?<=[.!?])\s+|\n", passage)
            matching = [
                s
                for s in sentences
                if re.search(
                    r"(?<!\w)" + re.escape(normalized(name)) + r"(?!\w)", normalized(s)
                )
            ]
            if matching and isinstance(url, str) and re.match(r"https?://", url):
                evidence[url] = " ".join(matching)[:1200]
    labels = {
        "en": (
            "I could not find reliable information identifying",
            "These retrieved excerpts mention the exact name",
            "People with similar names are not the same person. What profession, location, or other context should I use?",
            "Searched",
        ),
        "fr": (
            "Je n’ai pas trouvé d’information fiable permettant d’identifier",
            "Ces extraits trouvés mentionnent le nom exact",
            "Les personnes aux noms similaires ne sont pas la même personne. Quel métier, lieu ou autre contexte pouvez-vous préciser ?",
            "Recherches",
        ),
        "de": (
            "Ich konnte keine verlässlichen Informationen zur Identität finden",
            "Diese abgerufenen Auszüge nennen den genauen Namen",
            "Personen mit ähnlichen Namen sind nicht dieselbe Person. Welchen Beruf, Ort oder Kontext meinen Sie?",
            "Gesucht",
        ),
        "es": (
            "No encontré información fiable que identifique a",
            "Estos extractos recuperados mencionan el nombre exacto",
            "Las personas con nombres similares no son la misma persona. ¿Qué profesión, lugar o contexto puede indicar?",
            "Búsquedas",
        ),
        "it": (
            "Non ho trovato informazioni affidabili per identificare",
            "Questi estratti recuperati citano il nome esatto",
            "Le persone con nomi simili non sono la stessa persona. Quale professione, luogo o contesto intende?",
            "Ricerche",
        ),
    }
    missing, found, context, heading = labels[request.lang]
    text = f"{found if evidence else missing} **{escape(name)}**.\n\n"
    for number, (url, passage) in enumerate(evidence.items(), 1):
        # Escape active markup from untrusted snippets; the service owns links.
        text += (
            f"> {escape(passage).replace(chr(10), ' ')}\n\n[Source {number}]({url})\n\n"
        )
    text += (
        context
        + "\n\n"
        + heading
        + ":\n"
        + "\n".join(f"- `{query}`" for query in searched)
    )
    if failures:
        text += "\n\nSearch status: " + ", ".join(dict.fromkeys(failures)) + "."
    item.reponse, item.state, item.task_type = text, "done", "person_lookup"
    item.latence_ms["generation"] = (time.monotonic() - started) * 1000
    from services.orchestrator.stream_client import sink_context

    if sink := sink_context.get():
        await sink({"delta": {"content": text}})
