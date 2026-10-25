"""Exact-name discovery: retrieved excerpts, never a substituted biography."""

import json
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from html import escape
from typing import TYPE_CHECKING

from packages.ports import atlas_ports
from services.orchestrator.loop import Call, Limits, Query, run
from services.orchestrator.model import GatewayError, GatewayModel
from services.orchestrator.stream_client import sink_context
from packages.turn_attachments import normalized

if TYPE_CHECKING:
    from services.orchestrator.chat_pipeline import ChatTools
    from services.orchestrator.chat_schema import ChatRequest
    from services.orchestrator.interactions import Interaction


_NAME = r"([^\W\d_][\w'’.-]*(?:\s+[^\W\d_][\w'’.-]*){1,3})"


@dataclass(frozen=True)
class Evidence:
    url: str
    passage: str


_SYNTHESIS_INSTRUCTION = (
    "Answer briefly using only the exact-name evidence below. Cite its URLs. "
    "If it clearly describes different people with no dominant subject, ask for "
    "profession, place, or date; a directory/listing alone is not a person description."
)


async def _answer_from_evidence(
    request: "ChatRequest",
    item: "Interaction",
    tools: "ChatTools",
    name: str,
    evidence: list[Evidence],
    started: float,
) -> None:
    """Use the normal bounded answer loop after deterministic evidence filtering."""
    model = await GatewayModel.connect(
        os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway")),
        local_enabled=os.environ.get("GPU_LOCAL", "0") == "1",
    )
    model.tools = []
    model.observing = True
    model.sink = sink_context.get()
    model.configure_quality(request.model, request.max_tokens)
    model.local_enabled = os.environ.get("GPU_LOCAL", "0") == "1"
    tools.model = model
    item.retrieved_urls[:] = [source.url for source in evidence]
    history: list[dict[str, object]] = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "untrusted_exact_name_evidence": [
                        {"url": source.url, "passage": source.passage}
                        for source in evidence
                    ]
                },
                ensure_ascii=False,
            ),
        }
    ]
    try:
        result = await run(
            Query(question=request.user_text, lang=request.lang),
            model,
            tools,
            _SYNTHESIS_INSTRUCTION,
            Limits(
                profile=request.model,
                tool_calls=0,
                tokens=None,
                cost=Decimal("0.10"),
                wall_clock=max(
                    0, request.timeout_seconds - (time.monotonic() - started)
                ),
            ),
            history=history,
            retry_web=False,
        )
        item.reponse, item.state, item.task_type = (
            result.text,
            result.state,
            "person_lookup",
        )
        item.cout_eur = float(result.cost)
    finally:
        item.provider_retries += model.provider_retries
        item.tokens = {"in": model.input_tokens, "out": model.output_tokens}
        item.provider_model = model.provider_model
        item.reasoning = model.reasoning
        item.trace_tokens, item.answer_tokens = model.trace_tokens, model.answer_tokens
        item.token_split_estimated = model.token_split_estimated
        item.reasoning_retried = model.reasoning_retried
        item.latence_ms["generation"] = model.generation_ms
        if model.observations:
            item.modele_utilise = model.observations[-1].provider
            item.route_decision = model.observations[0].route


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
    similar_name_found = False
    surname = normalized(name).split()[-1]
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
            if not matching and re.search(
                r"(?<!\w)" + re.escape(surname) + r"(?!\w)", normalized(passage)
            ):
                similar_name_found = True
            if matching and isinstance(url, str) and re.match(r"https?://", url):
                evidence[url] = " ".join(matching)[:1200]
    labels = {
        "en": (
            "I could not find reliable information identifying",
            "I found retrieved sources that describe different people named",
            "What profession, location, or other context should I use?",
            "People with similar names are not the same person.",
            "Searched",
        ),
        "fr": (
            "Je n’ai pas trouvé d’information fiable permettant d’identifier",
            "Les sources trouvées décrivent différentes personnes nommées",
            "Quel métier, lieu ou autre contexte pouvez-vous préciser ?",
            "Les personnes aux noms similaires ne sont pas la même personne.",
            "Recherches",
        ),
        "de": (
            "Ich konnte keine verlässlichen Informationen zur Identität finden",
            "Die abgerufenen Quellen beschreiben verschiedene Personen namens",
            "Welchen Beruf, Ort oder Kontext meinen Sie?",
            "Personen mit ähnlichen Namen sind nicht dieselbe Person.",
            "Gesucht",
        ),
        "es": (
            "No encontré información fiable que identifique a",
            "Las fuentes recuperadas describen a distintas personas llamadas",
            "¿Qué profesión, lugar o contexto puede indicar?",
            "Las personas con nombres similares no son la misma persona.",
            "Búsquedas",
        ),
        "it": (
            "Non ho trovato informazioni affidabili per identificare",
            "Le fonti recuperate descrivono persone diverse chiamate",
            "Quale professione, luogo o contesto intende?",
            "Le persone con nomi simili non sono la stessa persona.",
            "Ricerche",
        ),
    }
    missing, _ambiguous, context, namesake, heading = labels[request.lang]
    exact_evidence = [Evidence(url, passage) for url, passage in evidence.items()]
    if exact_evidence:
        await _answer_from_evidence(request, item, tools, name, exact_evidence, started)
        return
    else:
        text = f"{missing} **{escape(name)}**."
        if similar_name_found:
            text += " " + namesake
        text += "\n\n" + context
    text += "\n\n" + heading + ":\n" + "\n".join(f"- `{query}`" for query in searched)
    if failures:
        text += "\n\nSearch status: " + ", ".join(dict.fromkeys(failures)) + "."
    item.reponse, item.state, item.task_type = text, "done", "person_lookup"
    item.latence_ms["generation"] = (time.monotonic() - started) * 1000
    if sink := sink_context.get():
        await sink({"delta": {"content": text}})
