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
    passages: tuple[str, ...]
    directory: bool


_LANGUAGE_NAMES = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
}

_SYNTHESIS_INSTRUCTION = (
    "Answer briefly using only the exact-name evidence below and cite its URLs. "
    "First identify distinct people: differences in profession, field, period, or "
    "place can mean separate identities. A directory or search listing alone is not "
    "an identity. When two or more notable identities are supported, name each briefly "
    "with sources, then ask which one is meant; if one is clearly dominant, answer about "
    "it and mention the others in one line. A namesake is supported only when a passage "
    "makes the asked full name, after case and accent folding, its subject; never infer a "
    "namesake from a surname page or a passage about a different full name."
)


def synthesis_instruction(language: str) -> str:
    """Keep person answers in the UI/question language, as on the normal chat path."""
    return (
        f"The user's question language is {_LANGUAGE_NAMES[language]} ({language}). "
        f"Reply in {_LANGUAGE_NAMES[language]}. " + _SYNTHESIS_INSTRUCTION
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
                        {
                            "url": source.url,
                            "passages": list(source.passages),
                            "source_kind": "directory"
                            if source.directory
                            else "profile",
                        }
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
            synthesis_instruction(request.lang),
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
    return (
        Call(
            "person-exact",
            "web_search",
            json.dumps({"query": f'"{name}"', "lang": language}),
        ),
    )


def _subject_passage(name: str, sentence: str) -> bool:
    """Accept an exact-name sentence with a subject, apposition, or dated heading."""
    folded_name = normalized(name)
    folded_sentence = normalized(sentence)
    subject = r"(?<!\w)" + re.escape(folded_name) + r"(?!\w)"
    predicates = (
        r"is|was|are|were|founded|cofounded|created|designs|designed|developed|"
        r"leads|led|works|worked|wrote|published|directs|served|joined|"
        r"est|etait|a fonde|dirige|travaille|cree|concu|"
        r"ist|war|grundete|leitet|arbeitet|entwickelte|"
        r"es|fue|fundo|dirige|trabaja|creo|"
        r"e|era|fondo|dirige|lavora|creo"
    )
    dated_subject = subject + r"\s*(?:\([^)]{1,80}\)\s*)?"
    return bool(
        re.search(
            r"(?:^|[;:]\s*|^.{0,48}?[·|:]\s*)"
            + dated_subject
            + r"(?:,\s*(?:an?\s+|the\s+|un[ae]?\s+|le\s+|la\s+|der\s+|die\s+|el\s+|il\s+)?[a-z]|\s+(?:"
            + predicates
            + r")\b)",
            folded_sentence,
        )
    )


def _subject_title(name: str, title: object) -> bool:
    """Titles may carry the identity when a provider truncates the snippet."""
    if not isinstance(title, str):
        return False
    folded_name = normalized(name)
    folded_title = normalized(title).strip()
    return bool(
        re.match(
            re.escape(folded_name) + r"(?:\s*\([^)]{1,80}\))?\s*(?:$|[-—–|:,])",
            folded_title,
        )
    )


def _distinctive_terms(name: str, passages: list[str]) -> tuple[str, ...]:
    """Choose ranked identity terms deterministically from the first exact-name evidence."""
    name_words = set(re.findall(r"[a-z0-9]+", normalized(name)))
    stop_words = {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "the",
        "to",
        "was",
        "who",
        "with",
        "le",
        "la",
        "les",
        "de",
        "des",
        "du",
        "un",
        "une",
        "und",
        "der",
        "die",
        "das",
        "el",
        "los",
        "las",
        "del",
        "il",
        "lo",
        "gli",
        "di",
        "da",
        "e",
        "founded",
        "cofounded",
        "created",
        "designs",
        "designed",
        "developed",
        "leads",
        "led",
        "works",
        "worked",
        "wrote",
        "published",
        "directs",
        "served",
        "joined",
        "founder",
        "creator",
    }
    terms: list[str] = []
    for passage in passages:
        for word in re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9'-]*", passage):
            folded = normalized(word)
            if len(folded) < 3 or folded in name_words or folded in stop_words:
                continue
            if folded not in terms:
                terms.append(folded)
            if len(terms) == 3:
                return tuple(terms)
    return tuple(terms)


def second_query(
    name: str, passages: list[str], language: str, provider: object
) -> Call | None:
    """Use the spare person-search slot to expose a potentially separate identity."""
    terms = _distinctive_terms(name, passages)
    if provider == "serpapi" and terms:
        query = " ".join([f'"{name}"', *(f"-{term}" for term in terms)])
    else:
        # Tavily does not document Boolean exclusions, and SearXNG delegates syntax to
        # a varying engine set, so use portable context words rather than pretending.
        query = f'"{name}" biography profile career'
    return Call(
        "person-other", "web_search", json.dumps({"query": query, "lang": language})
    )


def _is_directory_source(url: str, title: object, passage: str) -> bool:
    """Keep listings available to synthesis without letting them establish an identity."""
    source = f"{url} {title or ''} {passage}".casefold()
    return any(
        marker in source
        for marker in (
            "/search/",
            "/directory/",
            "/pub/dir/",
            "people search",
            "people directory",
            "profiles matching",
        )
    )


async def lookup(
    request: "ChatRequest", item: "Interaction", tools: "ChatTools", name: str
) -> None:
    started = time.monotonic()
    evidence: dict[str, tuple[list[str], bool]] = {}
    searched = []
    failures = []
    similar_name_found = False
    surname = normalized(name).split()[-1]

    def retain_results(result: object) -> tuple[list[str], object]:
        nonlocal similar_name_found
        data = result.get("data") if isinstance(result, dict) else None
        provider = data.get("provider") if isinstance(data, dict) else None
        rows = data.get("results", []) if isinstance(data, dict) else []
        retained: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = row.get("link")
            passage = str(
                row.get("snippet") or row.get("content") or row.get("title") or ""
            )
            title = row.get("title")
            sentences = re.split(r"(?<=[.!?])\s+|\n", passage)
            matching = [
                sentence for sentence in sentences if _subject_passage(name, sentence)
            ]
            if not matching and _subject_title(name, title):
                matching = [str(title) + (". " + passage if passage else "")]
            if not matching and re.search(
                r"(?<!\w)" + re.escape(surname) + r"(?!\w)", normalized(passage)
            ):
                similar_name_found = True
            if matching and isinstance(url, str) and re.match(r"https?://", url):
                excerpt = " ".join(matching)[:1200]
                passages, directory = evidence.setdefault(
                    url,
                    (
                        [],
                        _is_directory_source(url, row.get("title"), excerpt),
                    ),
                )
                if excerpt not in passages:
                    passages.append(excerpt)
                    retained.append(excerpt)
        return retained, provider

    initial_material: list[str] = []
    provider: object = None
    for call in queries(name, request.lang):
        searched.append(json.loads(call.arguments)["query"])
        try:
            result = await tools.execute(
                call, max(0.01, request.timeout_seconds - (time.monotonic() - started))
            )
        except GatewayError as exc:
            failures.append(exc.code)
            continue
        _retained, provider = retain_results(result)
        data = result.get("data") if isinstance(result, dict) else None
        rows = data.get("results", []) if isinstance(data, dict) else []
        for row in rows:
            if isinstance(row, dict):
                initial_material.append(
                    " ".join(
                        str(row.get(field) or "")
                        for field in ("title", "snippet", "content")
                    )
                )
    if follow_up := second_query(name, initial_material, request.lang, provider):
        searched.append(json.loads(follow_up.arguments)["query"])
        try:
            retain_results(
                await tools.execute(
                    follow_up,
                    max(0.01, request.timeout_seconds - (time.monotonic() - started)),
                )
            )
        except GatewayError as exc:
            failures.append(exc.code)
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
            "Las fuentes recuperadas describen a distintas personas con el nombre",
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
    exact_evidence = [
        Evidence(url, tuple(passages), directory)
        for url, (passages, directory) in evidence.items()
    ]
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
