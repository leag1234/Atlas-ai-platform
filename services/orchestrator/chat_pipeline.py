"""Chat uses the bounded harness and resolves citations through retrieval HTTP."""

from packages import linear_scan
from packages.limits import LimitError
from packages.validation import describe_validation
from pydantic import ValidationError

import asyncio
from typing import Any
import json
import logging
import copy
import os
from packages.ports import atlas_ports
import re
import time
from decimal import Decimal
from pathlib import Path

import aiohttp
from pydantic import BaseModel, Field

from services.orchestrator.cache import Cache
from services.orchestrator.searxng import NOTICES, OUTCOMES, search_language
from packages import privacy
from services.orchestrator import news_feeds, source_tools
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call, Limits, Message, Query, run
from services.orchestrator.model import GatewayError, GatewayModel
from services.orchestrator.tools import Rag, Runtime, search_providers
from services.orchestrator.stream_client import Sink, sink_context
from services.orchestrator import (
    document_quotes,
    grounding,
    number_provenance,
    source_faithfulness,
    travel_claims,
    unsourced_details,
)
from services.orchestrator.vision import process_vision
from services.orchestrator.search_policy import required_research, corpus_allowed
from services.orchestrator.followup import (
    is_followup,
    image_iteration,
    image_dialogue,
    image_medium,
    image_explanation,
    image_retry_confirmation,
    raw_scene_continuation,
    visual_image_prompt,
)
from services.orchestrator.imagegen import process_image
from services.orchestrator import diagram
from packages.content_policy import (
    extended_enabled,
    extended_instruction,
    extended_profiles,
    extended_request,
    short_refusal,
    text_limit,
    text_refusal,
)
from packages.profiles import PROFILES
from services.orchestrator.image_truth import (
    image_topic,
    policy_instruction,
    short_correction,
    unsupported_image_block,
    wants_generation,
)
from packages.language import (
    detected_language,
    conversation_language,
    language_instruction,
    requested_language,
)
from services.orchestrator.image_tool import (
    GenerateImage,
    declaration,
    explicit_image_request,
    finish,
    finish_appearance,
    finish_mechanical_drawing,
    mechanical_svg_enabled,
    finish_simple_drawing,
    guest_capability_instruction,
    images_enabled,
    unavailable_reply,
)
from services.orchestrator.daily_budget import BudgetRefusal, DailyLedger
from packages.artifact_intent import app_intent, svg_requested
from services.orchestrator.artifact_tools import current_artifact_message
from services.orchestrator.app_turn import process_app_turn
from services.orchestrator.appearance import (
    is_appearance_request,
    is_simple_drawing_request,
    legend_instruction,
    mechanical_svg_instruction,
    simple_drawing_language,
    simple_drawing_instruction,
    is_spatial_drawing_request,
    spatial_drawing_instruction,
)


def search_failure_notice(outcome: str, language: str) -> str:
    """Keep application-generated search caveats in the conversation language."""
    notices = {
        "en": NOTICES,
        "fr": {
            "upstream_blocked": "La recherche web a été bloquée par les moteurs en amont ; aucun résultat vérifié n’a été obtenu.",
            "upstream_timeout": "La recherche web a expiré ; aucun résultat vérifié n’a été obtenu.",
            "empty_results": "Les moteurs de recherche n’ont renvoyé aucun résultat correspondant. Cela ne prouve pas qu’aucune information n’existe.",
            "partial_results": "La recherche a renvoyé des résultats partiels ; certaines sources étaient indisponibles ou trop peu de résultats web ont été trouvés.",
        },
        "de": {
            "upstream_blocked": "Die Websuche wurde von vorgeschalteten Suchmaschinen blockiert; es wurden keine verifizierten Ergebnisse abgerufen.",
            "upstream_timeout": "Bei der Websuche ist eine Zeitüberschreitung aufgetreten; es wurden keine verifizierten Ergebnisse abgerufen.",
            "empty_results": "Die Suchmaschinen lieferten keine passenden Ergebnisse. Das beweist nicht, dass keine Informationen existieren.",
            "partial_results": "Die Suche lieferte unvollständige Ergebnisse; einige Quellen waren nicht verfügbar oder es wurden zu wenige Webergebnisse gefunden.",
        },
        "es": {
            "upstream_blocked": "Los motores de búsqueda ascendentes bloquearon la búsqueda web; no se obtuvieron resultados verificados.",
            "upstream_timeout": "La búsqueda web agotó el tiempo de espera; no se obtuvieron resultados verificados.",
            "empty_results": "Los motores de búsqueda no devolvieron resultados coincidentes. Esto no demuestra que no exista información.",
            "partial_results": "La búsqueda devolvió resultados parciales; algunas fuentes no estaban disponibles o se encontraron muy pocos resultados web.",
        },
        "it": {
            "upstream_blocked": "La ricerca web è stata bloccata dai motori a monte; non sono stati ottenuti risultati verificati.",
            "upstream_timeout": "La ricerca web è scaduta; non sono stati ottenuti risultati verificati.",
            "empty_results": "I motori di ricerca non hanno restituito risultati corrispondenti. Questo non dimostra che non esistano informazioni.",
            "partial_results": "La ricerca ha restituito risultati parziali; alcune fonti non erano disponibili oppure sono stati trovati troppo pochi risultati web.",
        },
    }
    return notices.get(language, NOTICES).get(outcome, NOTICES[outcome])


def partial_search_failure_notice(outcome: str, language: str) -> str:
    """Disclose a later search failure without denying already cited evidence.

    The reason is said in words: an outcome code such as "partial_results" in the
    answer is raw technical text for the person (staging, 2026-11-12).
    """
    reasons = {
        "en": {
            "upstream_blocked": "the search engines blocked the request",
            "upstream_timeout": "the search timed out",
            "empty_results": "the search found nothing",
            "partial_results": "some sources were unavailable",
        },
        "fr": {
            "upstream_blocked": "les moteurs de recherche ont bloqué la requête",
            "upstream_timeout": "la recherche a dépassé le délai",
            "empty_results": "la recherche n'a rien trouvé",
            "partial_results": "certaines sources étaient indisponibles",
        },
        "de": {
            "upstream_blocked": "die Suchmaschinen haben die Anfrage blockiert",
            "upstream_timeout": "die Suche hat das Zeitlimit überschritten",
            "empty_results": "die Suche hat nichts gefunden",
            "partial_results": "einige Quellen waren nicht verfügbar",
        },
        "es": {
            "upstream_blocked": "los motores de búsqueda bloquearon la solicitud",
            "upstream_timeout": "la búsqueda superó el tiempo límite",
            "empty_results": "la búsqueda no encontró nada",
            "partial_results": "algunas fuentes no estaban disponibles",
        },
        "it": {
            "upstream_blocked": "i motori di ricerca hanno bloccato la richiesta",
            "upstream_timeout": "la ricerca ha superato il tempo limite",
            "empty_results": "la ricerca non ha trovato nulla",
            "partial_results": "alcune fonti non erano disponibili",
        },
    }
    notices = {
        "en": "Part of the web search failed ({reason}); this answer relies only on the sources retrieved before it.",
        "fr": "Une partie de la recherche web a échoué ({reason}) ; cette réponse repose uniquement sur les sources récupérées avant cet échec.",
        "de": "Ein Teil der Websuche ist fehlgeschlagen ({reason}); diese Antwort stützt sich nur auf die zuvor abgerufenen Quellen.",
        "es": "Parte de la búsqueda web falló ({reason}); esta respuesta se basa únicamente en las fuentes recuperadas antes del fallo.",
        "it": "Una parte della ricerca web non è riuscita ({reason}); questa risposta si basa solo sulle fonti recuperate prima dell'errore.",
    }
    language = language if language in notices else "en"
    words = reasons[language]
    reason = words.get(outcome, words["partial_results"])
    return notices[language].format(reason=reason)


class Source(BaseModel):
    chunk_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    doc_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=32000)


class Passage(Source):
    score: float = Field(allow_inf_nan=False)


class Passages(BaseModel):
    passages: list[Passage] = Field(max_length=8)


SOURCE_TOOLS = frozenset(source_tools.SCHEMAS)


def without_model_code_tools(tools: list[dict[str, object]]) -> list[dict[str, object]]:
    """Keep read-only tools while stable mode removes model-authored code paths."""
    return [
        tool
        for tool in tools
        if not (
            isinstance(function := tool.get("function"), dict)
            and function.get("name") in {"terminal_command", "publish_document"}
        )
    ]


def select_passages(
    passages: list[Passage], query: str = "", token_budget: int = 1000
) -> list[dict[str, str]]:
    from packages.evidence import whole_chunks

    return whole_chunks(
        [
            {"chunk_id": p.chunk_id, "source": p.source, "text": p.text}
            for p in sorted(passages, key=lambda p: p.score, reverse=True)
        ],
        token_budget,
    )


def retrieval_url() -> str:
    return os.environ.get("ATLAS_RETRIEVAL_URL", atlas_ports().url("retrieval")).rstrip(
        "/"
    )


async def source(key: str) -> dict[str, object]:
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=5), trust_env=False
    ) as client:
        async with client.get(
            retrieval_url() + "/sources/" + key, allow_redirects=False
        ) as response:
            if response.status != 200:
                raise ValueError("invalid_citation")
            body = await response.read()
            if len(body) > 800000:
                raise LimitError("source_response_limit", len(body), 800000, "bytes")
    return Source.model_validate(json.loads(body)).model_dump()


_SOURCE_END = re.compile(r"/sources/[a-f0-9]{64}")
# "(?<!\[)": in a run of "[", only the first can start a label; the others
# reach the same "]" and rescanning from each is quadratic. Openings that share
# a failed target run are not retried either (many "[a](" before one far ")").
_SOURCE_LINKS = linear_scan.Scan(
    re.compile(r"(?<!\[)\[[^\]]*\]\([^)]*/sources/[a-f0-9]{64}\)"),
    re.compile(r"(?<!\[)\["),
    linear_scan.bracket_link_failure(
        r"\]",
        r")",
        empty_label=True,
        run_end_fails=lambda text, end: text[end : end + 1] != ")"
        or _SOURCE_END.fullmatch(text, max(0, end - 73), end) is None,
    ),
)


async def render_citations(item: Interaction) -> None:
    # Without "/sources/" nothing can match.
    uncited = (
        _SOURCE_LINKS.sub("", item.reponse)
        if "/sources/" in item.reponse
        else item.reponse
    )
    keys = list(dict.fromkeys(re.findall(r"\b[a-f0-9]{64}\b", uncited)))
    retrieved = {str(p["chunk_id"]): p for p in item.chunks_recuperes}
    if not set(keys) <= retrieved.keys():
        raise ValueError("invalid_citation")
    for number, key in enumerate(keys, 1):
        resolved = await source(key)
        if any(
            resolved[k] != retrieved[key][k]
            for k in ("chunk_id", "doc_id", "source", "text")
        ):
            raise ValueError("citation_changed")
        item.citations.append(resolved)
        url = os.environ.get(
            "ATLAS_PUBLIC_URL", atlas_ports().url("adapter", "localhost")
        ).rstrip("/")
        item.reponse = re.sub(
            r"[`\[]?" + key + r"[`\]]?",
            f"[Source {number}]({url}/sources/{key})",
            item.reponse,
        )


class ChatTools(Runtime):
    def __init__(
        self, item: Interaction, question: str = "", allow_image: bool = False
    ) -> None:
        super().__init__(
            Cache(Path(os.environ.get("ATLAS_WEB_CACHE", "BRAIN/web-cache.sqlite"))),
            Decimal(0),
            question,
        )
        self.item = item
        self.skills: Any = None
        self.daily_ledger: DailyLedger | None = None
        self.daily_user = "unknown"
        self.daily_lang = "en"
        self.allow_image = allow_image
        self.image_prompt: str | None = None
        self.terminal_code_disabled = False
        self.source_truncated = False
        self.fetched_pages: dict[str, str] = {}
        self.search_snippets: dict[str, str] = {}
        self.shopping_searches: dict[str, list[Message]] = {}
        self.model: GatewayModel | None = None
        self.terminal = None
        if os.environ.get("ATLAS_TERMINAL_ENABLED") == "1":
            from services.orchestrator.terminal_client import TerminalClient

            self.terminal = TerminalClient(item.request_id)

    async def execute(self, call: Call, timeout: float) -> Message:
        return document_quotes.observe(
            number_provenance.observe(
                await self._execute(call, timeout, trusted_render=False)
            )
        )

    async def execute_trusted_render(self, call: Call, timeout: float) -> Message:
        return document_quotes.observe(
            number_provenance.observe(
                await self._execute(call, timeout, trusted_render=True)
            )
        )

    async def _execute(
        self, call: Call, timeout: float, *, trusted_render: bool
    ) -> Message:
        if self.skills is not None:
            if not await self.skills.authorized():
                return {"error": "skill_authority_revoked"}
            if call.name == "load_skill":
                return dict(await self.skills.execute(call, timeout))
            if self.skills.blocks(call.name):
                return {"error": "skill_action_not_permitted"}
        if not privacy.tool_allowed(call.name):
            # A tool the level does not offer can still be named by a model that
            # knows it (an owned model calling web_search in a strict turn, staging
            # 2026-11-16). It never runs and never ends the turn: the model is told
            # to answer with what it has, like for any unavailable tool.
            self.item.erreurs.append("tool_not_offered_at_privacy_level")
            return {
                "error": "tool_unavailable",
                "message": f"{call.name} is not available at the {privacy.current()} "
                "privacy level. Answer with the tools offered and say what could not "
                "be checked.",
            }
        sink = sink_context.get()
        urls: list[str] = []
        try:
            arguments = json.loads(call.arguments)
            if call.name == "web_fetch" and isinstance(arguments.get("url"), str):
                urls.append(arguments["url"])
        except (ValueError, AttributeError):
            pass
        if sink:
            await sink({"phase": "tool_started", "tool": call.name, "urls": urls})
        if call.name in {"terminal_command", "publish_document"}:
            from services.orchestrator.terminal_client import Command, Publish

            if self.terminal_code_disabled or (
                os.environ.get("ATLAS_STABLE") == "1" and not trusted_render
            ):
                return {"error": "tool_unavailable"}
            if self.terminal is None:
                return {"error": "terminal_unavailable"}
            try:
                if call.name == "terminal_command":
                    output = await self.terminal.execute(
                        Command.model_validate_json(call.arguments).command, timeout
                    )
                    if self.model is not None and self.model.has_attachments:
                        output = self.document_evidence(output)
                else:
                    publication = Publish.model_validate_json(call.arguments)
                    output = await self.terminal.publish(
                        publication.path, publication.strategy, timeout
                    )
                    self.item.files = list(self.terminal.files)
                    self.item.file_strategies = list(self.terminal.strategies)
                if call.name == "terminal_command" and (
                    output.get("error") or output.get("exit_code", 0) != 0
                ):
                    self.item.erreurs.append("terminal_operation_failed")
                    return {"error": "terminal_operation_failed", "data": output}
                if self.model is not None and self.model.required_file_tool:
                    if call.name == "terminal_command":
                        self.model.required_file_tool = "publish_document"
                    elif all(
                        any(url.endswith("." + ext) for url in self.terminal.files)
                        for ext in self.model.required_file_formats
                    ):
                        self.model.required_file_tool = None
                return {"trust": "untrusted", "data": output}
            except LimitError as exc:
                return {"error": exc.code, "message": exc.detail}
            except ValidationError as exc:
                return {
                    "error": "invalid_arguments",
                    "message": describe_validation(exc),
                }
            except (ValueError, RuntimeError, aiohttp.ClientError, TimeoutError):
                self.item.erreurs.append("terminal_operation_failed")
                return {"error": "terminal_operation_failed"}
        if call.name == "generate_image":
            if not self.allow_image:
                return {"error": "tool_unavailable"}
            try:
                self.image_prompt = GenerateImage.model_validate_json(
                    call.arguments
                ).prompt
                return {"selected": "generate_image"}
            except ValidationError as exc:
                return {
                    "error": "invalid_image_arguments",
                    "message": describe_validation(exc),
                }
            except ValueError:
                return {"error": "invalid_image_arguments"}
        if call.name != "rag_search":
            had_verified_results = bool(self.item.retrieved_urls)
            try:
                output = await super().execute(call, timeout)
            except BudgetRefusal as exc:
                from services.orchestrator.search_guard import refusal_text

                # A limit stays a limit for the model and the user (D5).
                return {"error": "daily_search_limit", "message": refusal_text(exc)}
            data = output.get("data")
            if (
                call.name == "journey_plan"
                and self.model is not None
                and isinstance(data, dict)
                and data.get("rendered")
            ):
                # A plan is the answer's core: the next call writes the answer.
                # Live on the branch (2026-11-16) a search after the plan left the
                # answer too little time and the turn timed out.
                self.model.answer_only = True
            if (
                call.name == "transit_plan"
                and isinstance(data, dict)
                and data.get("rendered")
                and isinstance(data.get("source_urls"), list)
            ):
                # D7: the planner computed this plan for the turn; its
                # attribution pages may be cited like a page read.
                urls = [str(u) for u in data["source_urls"] if isinstance(u, str)]
                self.item.retrieved_urls.extend(urls)
                if data.get("doubts") and privacy.tool_allowed("web_search"):
                    # A doubtful plan: code reads the operators' direct services
                    # (staging -129: the model kept the night chain to Geneva).
                    data = await self.direct_services(data, timeout)
                    output = {**output, "data": data}
            if call.name in {"web_images", "make_qr"} and isinstance(data, dict):
                images = data.get("images", [data] if call.name == "make_qr" else [])
                if isinstance(images, list):
                    for image in images:
                        if isinstance(image, dict) and isinstance(
                            image.get("reference"), str
                        ):
                            self.item.images.append(
                                {
                                    "reference": image["reference"],
                                    "kind": str(image.get("kind", "web_image")),
                                }
                            )
                            if image.get("kind") == "qr" and isinstance(
                                image.get("payload"), str
                            ):
                                self.item.images[-1]["payload"] = image["payload"]
                            if isinstance(image.get("credit_url"), str):
                                self.item.retrieved_urls.append(image["credit_url"])
            if call.name == "currency_convert" and isinstance(data, dict):
                source = data.get("source_url")
                if isinstance(source, str):
                    # D7: the rate feed was fetched for this turn, so the answer
                    # may cite it like a fetched page.
                    self.item.retrieved_urls.append(source)
                    urls = [source]
            if call.name == "web_search" and isinstance(data, dict):
                data = self.search_evidence(data)
                output = {**output, "data": data}
                query = json.loads(call.arguments).get("query", "")
                if " (site:" in query:
                    rows = data.get("results", [])
                    self.shopping_searches[query] = (
                        [row for row in rows if isinstance(row, dict)]
                        if isinstance(rows, list)
                        else []
                    )
            if call.name == "web_search" and str(output.get("error")) in OUTCOMES:
                outcome = str(output["error"])
                self.item.erreurs.append(outcome)
                if had_verified_results:
                    self.item.erreurs.append(f"partial_search_failure:{outcome}")
                if not had_verified_results:
                    raise GatewayError(outcome, 503, NOTICES[outcome])
            if (
                call.name == "web_search"
                and isinstance(data, dict)
                and str(data.get("outcome")) in OUTCOMES
            ):
                outcome = str(data["outcome"])
                # Partial results that still hold pages are a search that
                # worked: its caveat stays in the search details, not under the
                # answer (staging 2026-11-12, a sourced answer ended with
                # "the search returned partial results").
                if not (outcome == "partial_results" and data.get("results")):
                    self.item.erreurs.append(outcome)
                if had_verified_results and not data.get("results"):
                    self.item.erreurs.append(f"partial_search_failure:{outcome}")
                if not data.get("results") and not had_verified_results:
                    raise GatewayError(outcome, 503, NOTICES[outcome])
            if isinstance(data, dict):
                self.source_truncated |= data.get("truncated") is True
                if isinstance(data.get("url"), str):
                    urls = [data["url"]]
                results = data.get("results")
                if isinstance(results, list):
                    urls = [
                        str(r["link"])
                        for r in results
                        if isinstance(r, dict) and isinstance(r.get("link"), str)
                    ]
            if call.name in SOURCE_TOOLS and isinstance(data, dict):
                # D7: every URL a specialized source names was fetched for this
                # turn (the page, the feed, the dataset), so it may be cited.
                urls = source_tools.cited_urls(data)
                self.item.retrieved_urls.extend(urls)
                if (check := grounding.current.get()) is not None:
                    check.collect(
                        {"text": json.dumps(data, ensure_ascii=False)[:20_000]}
                    )
                travel_claims.collect_tool_output(call.name, data)
            if call.name in {"web_search", "web_fetch"} and "error" not in output:
                self.item.retrieved_urls.extend(urls)
                if (check := grounding.current.get()) is not None:
                    check.collect(data)
                travel_claims.collect_tool_output(call.name, data)
                travel = travel_claims.current.get()
                if travel is not None and travel.journey_offered:
                    from services.orchestrator import journey

                    travel.results += 1
                    # A trip turn researches within a bounded share of the chat
                    # deadline, then writes (staging 2026-11-16: a forced plan
                    # call and a long answer passed the 120 s cut; a model that
                    # answered instead of the forced call failed the turn). The
                    # plan stays the model's choice; the answer is post-checked.
                    closing = journey.research_closed(travel.results, travel.elapsed())
                    output = {
                        **output,
                        "next_step": journey.WRITE_NOW
                        if closing or travel.journey_called
                        else journey.NEXT_STEP,
                    }
                    if closing and self.model is not None:
                        self.model.answer_only = True
                if call.name == "web_search" and isinstance(data, dict):
                    for row in data.get("results", []):
                        if isinstance(row, dict) and isinstance(row.get("link"), str):
                            self.search_snippets[row["link"]] = str(
                                row.get("snippet", "")
                            )
                if (
                    call.name == "web_fetch"
                    and isinstance(data, dict)
                    and isinstance(data.get("text"), str)
                ):
                    self.fetched_pages.update({url: str(data["text"]) for url in urls})
            if "error" in output:
                self.item.erreurs.append(str(output["error"]))
            sink = sink_context.get()
            if sink:
                await sink(
                    {
                        "phase": "tool_finished",
                        "tool": call.name,
                        "urls": urls,
                        "ok": "error" not in output,
                        "outcome": data.get("outcome")
                        if isinstance(data, dict)
                        else None,
                        "message": data.get("message")
                        if isinstance(data, dict)
                        else None,
                    }
                )
            if sink and isinstance(data, dict):
                if data.get("synthesis"):
                    await sink(
                        {"phase": "synthesized", "tool": call.name, "urls": urls}
                    )
                if data.get("truncated"):
                    await sink(
                        {"phase": "source_truncated", "tool": call.name, "urls": urls}
                    )
            if (
                call.name == "web_search"
                and output.get("error") != "daily_search_limit"
                and (
                    any(
                        provider in {"tavily", "searxng"}
                        for provider in search_providers()
                    )
                    or output.get("error") in {"quota_exceeded", "search_rate_limited"}
                )
                and (
                    "error" in output
                    or not isinstance(data, dict)
                    or not data.get("results")
                )
                # Sources already retrieved in this turn are kept: a later
                # quota or rate refusal closes the research (closing.py) and the
                # answer is written from them, naming the limit (D5). Only a
                # turn without any retrieved source is refused here (staging
                # 2026-11-14: a second search reached an exhausted provider and
                # the whole sourced answer was refused with 503).
                and not self.item.retrieved_urls
            ):
                # Stop before another inference can silently replace failed research
                # with remembered facts, including during streamed responses.
                reason = (
                    "quota exhausted"
                    if output.get("error") == "quota_exceeded"
                    else "hourly limit reached"
                    if output.get("error") == "search_rate_limited"
                    else "provider failed or returned no results"
                )
                raise GatewayError(
                    str(output["error"])
                    if output.get("error") in {"quota_exceeded", "search_rate_limited"}
                    else "search_unavailable",
                    503,
                    f"Web search unavailable: {reason}. "
                    + (
                        f"Provider HTTP {output['provider_status']}. "
                        if type(output.get("provider_status")) is int
                        else ""
                    )
                    + "No verified result was obtained "
                    "(required: at least 1). I cannot verify this answer from current sources. "
                    "Search limits: 3 calls per request and 900 local reservations per month.",
                )
            return output
        started = time.monotonic()
        if not corpus_allowed(self.question):
            return {"error": "corpus_excluded_by_user"}
        succeeded = False
        try:
            request = Rag.model_validate_json(call.arguments)
            result = Passages.model_validate(
                await GatewayModel.post(
                    retrieval_url() + "/search", request.model_dump(), timeout
                )
            )
            succeeded = True
            passages = [p.model_dump() for p in result.passages]
            self.item.chunks_recuperes.extend(passages)
            for passage in result.passages:
                document_quotes.add_document(passage.text, retrieved=True)
            return {
                "trust": "untrusted",
                "data": {
                    "passages": select_passages(result.passages, request.query, 8000)
                },
            }
        except LimitError as exc:
            return {"error": exc.code, "message": exc.detail}
        except ValidationError as exc:
            self.item.erreurs.append("retrieval_unavailable")
            if any(
                error.get("ctx", {}).get("max_length") is not None
                for error in exc.errors()
            ):
                return {
                    "error": "retrieval_unavailable",
                    "message": describe_validation(exc),
                }
            return {"error": "retrieval_unavailable"}
        except (ValueError, RuntimeError, TimeoutError):
            self.item.erreurs.append("retrieval_unavailable")
            return {"error": "retrieval_unavailable"}
        finally:
            if sink:
                await sink(
                    {
                        "phase": "tool_finished",
                        "tool": call.name,
                        "urls": [],
                        "ok": succeeded,
                    }
                )
            self.item.latence_ms["retrieval"] += (time.monotonic() - started) * 1000

    def _reserve_search(self, provider: str) -> None:
        if self.daily_ledger is not None:
            self.daily_ledger.reserve_search(self.daily_user, self.daily_lang, provider)
        super()._reserve_search(provider)

    async def direct_services(self, data: Message, timeout: float) -> Message:
        """A doubtful plan with the pages that one web search for the trip's
        direct services finds and reads, through the turn's own web tools (D7,
        limits and checks as for the model's calls)."""
        from services.orchestrator import transit_planner

        query = str(data.get("direct_query") or "")
        if not query:
            return transit_planner.with_direct_pages(data, [])
        travel = travel_claims.current.get()
        language = travel.language if travel is not None else "en"
        if language not in {"fr", "de", "es", "it", "en"}:
            language = "en"
        try:
            found = await self.execute(
                Call(
                    "doubtful-plan-search",
                    "web_search",
                    json.dumps({"query": query, "lang": language, "n": 5}),
                ),
                timeout,
            )
        except GatewayError:
            return transit_planner.with_direct_pages(data, [])
        links = (
            []
            if "error" in found
            else transit_planner.direct_candidates(found.get("data"))
        )
        read = await asyncio.gather(
            *(
                self.execute(
                    Call(
                        f"doubtful-plan-fetch-{number}",
                        "web_fetch",
                        json.dumps({"url": link, "query": query}),
                    ),
                    timeout,
                )
                for number, link in enumerate(links)
            ),
            return_exceptions=True,
        )
        pages: list[dict[str, str]] = []
        for link, page in zip(links, read, strict=True):
            if not isinstance(page, dict) or "error" in page:
                continue
            body = page.get("data")
            text = body.get("text") if isinstance(body, dict) else None
            if isinstance(text, str) and text.strip():
                pages.append({"url": link, "text": text})
        return transit_planner.with_direct_pages(data, pages)

    def search_evidence(self, data: Message) -> Message:
        """Budget evidence for the next inference; every later step rechecks cost."""
        from services.orchestrator.content import select_passages as select_text

        rows = data.get("results")
        if self.model is None or not isinstance(rows, list) or not rows:
            return data
        price = self.model.configuration.input_eur_per_mtok
        if price <= 0:
            return data
        # Account for JSON escaping and retain half the allowance for output
        # and other context. Do not reserve ten hypothetical future calls.
        budget = max(1, int(self.model.allowance * 1000000 / (price * 2 * 2)))
        if self.model.produces_files:
            budget = min(budget, 8000)
        per_source = max(1, budget // len(rows))
        result = copy.deepcopy(data)
        copied = result["results"]
        assert isinstance(copied, list)
        for row in copied:
            if not isinstance(row, dict) or not isinstance(row.get("content"), str):
                continue
            if self.model.produces_files and isinstance(row.get("snippet"), str):
                row["snippet"] = row["snippet"][:per_source]
            text = row["content"]
            measured = len(text.encode())
            if measured <= per_source:
                continue
            parts = select_text(text, self.question, per_source)
            selected = "\n".join(part.text for part in parts)
            if self.model.produces_files:
                selected = selected.encode()[:per_source].decode("utf-8", "ignore")
            row.update(
                content=selected,
                source_bytes=measured,
                preview_limit_bytes=per_source,
                truncated=True,
                detail=f"Selected {len(selected.encode())} of {measured} preview bytes; limit {per_source} bytes. Use web_fetch on the source link for full evidence.",
            )
        return result

    def document_evidence(self, output: Message) -> Message:
        """Apply M10 to complete output before reserving another inference."""
        from services.orchestrator.content import hierarchical_summary

        entries = output.get("output")
        if (
            self.model is None
            or output.get("exit_code") != 0
            or not isinstance(entries, list)
        ):
            return output
        text = "\n".join(
            entry["data"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("data"), str)
        )
        size = len(text.encode())
        reservation = size * 2 * self.model.configuration.input_eur_per_mtok / 1_000_000
        # Retain room for history, escaped tool results and the final answer.
        if size <= 131072 and reservation <= self.model.allowance / 2:
            return output
        summary, _, sections = hierarchical_summary(text, self.question)
        metadata: dict[str, str | int | bool] = {
            "hierarchical_synthesis": True,
            "source_characters": len(text),
            "source_bytes": size,
            "selected_characters": len(summary),
            "sections_examined": sections,
            "remaining_budget_eur": str(self.model.allowance),
            "estimated_evidence_reservation_eur": str(reservation),
        }
        self.item.documents.append(metadata)
        return {
            **output,
            "output": [{"type": "output", "data": summary}],
            "truncated": True,
            "detail": (
                f"Selected {len(summary)} of {len(text)} characters within the "
                f"remaining {self.model.allowance} EUR budget. These extracts are "
                "not a complete reading. For an exhaustive summary, read all "
                "remaining sections in bounded outputs; otherwise state the gap."
            ),
            "synthesis": metadata,
        }


async def dispatch_project(request: ChatRequest, item: Interaction) -> bool:
    """Resolve compatibility scope before any attachment or follow-up route."""
    if request.project_id is None:
        from services.orchestrator.project_commands import select

        if await select(request, item, retrieval_url()):
            return True
    if request.project_id is None:
        return False
    if request.code_mode:
        raise ValueError("selector_conflict")
    from services.orchestrator.project_chat import process_project

    await process_project(request, item)
    return True


async def process(request: ChatRequest, item: Interaction) -> None:
    from services.orchestrator.image_truth import verified_images

    from packages import context_usage

    capture = context_usage.samples.set(item.context_samples)
    components = context_usage.parts.set({})
    # Truthfulness V1 (owner decision 2026-10-31): a factual question is answered in
    # one piece after its precise claims are checked against the sources fetched in
    # this turn; other turns keep streaming.
    # Off by default until it is tuned: the first live measure (2026-10-31) showed it
    # marked 28 of 58 correct factual answers as unverified. ATLAS_GROUNDING=on enables it.
    check = (
        grounding.Grounding()
        if os.environ.get("ATLAS_GROUNDING", "off") == "on"
        and (
            grounding.needs_grounding(request.user_text)
            or (
                unsourced_details.wants_review(request.user_text)
                and not request.has_attachments
                and not request.project_id
            )
        )
        else None
    )
    checking = grounding.current.set(check)
    # The person's own question and language, before any service rewrite of the
    # last message (location_route adds a position note in English): grounding
    # notes are checked against what was asked and written in the answer language.
    asked = request.user_text
    asked_language = (
        conversation_language(request.conversation_messages, request.ui_locale)
        if check is not None
        else ""
    )
    # Number provenance stage 0: counts computed numbers without evidence; log only.
    provenance = number_provenance.Provenance() if number_provenance.enabled() else None
    tracking = number_provenance.current.set(provenance)
    sink = sink_context.get()
    # Travel durations, distances and times come from a source or from code
    # (travel_claims): a travel turn is held like a checked factual one. Whether
    # a turn is about travel is known after routing, before any answer text.
    travel = travel_claims.Turn(
        asked=[m.text for m in request.messages if m.role == "user"],
        zone=request.ui_timezone,
        # The person's language, read before any service note joins the message.
        language=conversation_language(
            request.conversation_messages, request.ui_locale
        ),
    )
    traveling = travel_claims.current.set(travel)
    holder = _HeldAnswer(sink, holding=bool(check)) if sink else None
    held = sink_context.set(holder) if holder is not None else None
    if holder is not None:
        travel.hold = holder.hold
    from services.orchestrator import relative_dates

    # Relative dates and weekdays in the answer are checked against the calendar
    # (relative_dates): a near miss is repaired in the stream and the stored answer.
    # The gate sits inside the quotation gate, so the stream applies the checks
    # in the stored order (quotes, then dates). A held answer (travel, grounding)
    # is sent whole at the end; the date gate then only feeds the holder.
    answer_language = request.prose_lang
    today = relative_dates.local_today(request.ui_timezone)
    asked_days = relative_dates.asked_weekdays(request.user_text, answer_language)
    # A stray character of another script glued to a word (stray_script): it
    # leaves the stream and the stored answer. Innermost gate: it reads the
    # text after the quotation and date checks, in the stored order.
    from services.orchestrator import stray_script

    own_scripts = stray_script.scripts_in(
        m.text for m in request.messages if m.role == "user"
    )
    script_gate: stray_script.Gate | None = None
    scripting = None
    if (current_sink := sink_context.get()) is not None:
        script_gate = stray_script.Gate(
            current_sink, stray_script.Rewriter(answer_language, own_scripts)
        )
        scripting = sink_context.set(script_gate)
    date_gate: relative_dates.Gate | None = None
    dating = None
    if (current_sink := sink_context.get()) is not None:
        date_gate = relative_dates.Gate(
            current_sink,
            relative_dates.Rewriter(answer_language, today, asked_days),
        )
        dating = sink_context.set(date_gate)
    # Quotations attributed to the turn's documents are checked word for word
    # (document_quotes): the gate holds an open quotation until it can be checked.
    # The same collected sources feed the faithfulness check (source_faithfulness).
    quote_mode = document_quotes.mode()
    # The interface sends its own tasks (title, tags, follow-up suggestions)
    # through the same endpoint: a "### Task:" prompt quoting the conversation,
    # answered by raw JSON the interface parses. The prompt is longer than a
    # pasted text but is not one, and its answer restates no source (staging
    # 2026-11-16 to 11-19: the quotation check stripped the quotes of the title
    # value, the faithfulness check judged the broken title: 83 of 89 checks).
    interface_task = request.user_text.lstrip().startswith("### Task:")
    quotes = (
        document_quotes.Sources()
        if not interface_task and (quote_mode != "off" or source_faithfulness.enabled())
        else None
    )
    quoting = document_quotes.current.set(quotes)
    quote_gate: document_quotes.Gate | None = None
    gating = None
    quote_language = ""
    if quotes is not None:
        for message in request.messages:
            if message.role == "user":
                quotes.add_conversation(message.text)
        quote_language = conversation_language(
            request.conversation_messages, request.ui_locale
        )
        if quote_mode != "off" and (current_sink := sink_context.get()) is not None:
            quote_gate = document_quotes.Gate(
                current_sink,
                document_quotes.rewriter(quotes, quote_language, request.user_text),
            )
            gating = sink_context.set(quote_gate)
    from services.orchestrator.source_truth import urls as text_urls, user_urls

    supplied = user_urls.set(
        frozenset(
            url
            for message in request.messages
            if message.role == "user"
            for url in text_urls(message.text)
        )
    )
    from services.orchestrator.deadline import answer_recovery

    def trip_recovery() -> float:
        # A trip that read pages can be answered from them when its answer
        # call fails: that call leaves the time to do it (loop.answer_share).
        recoverable = (
            travel.active
            and bool(travel.evidence.evidence)
            and item.task_type not in {"work", "nearby"}
        )
        return TRIP_RECOVERY_SECONDS if recoverable else 0.0

    try:
        try:
            with answer_recovery(trip_recovery):
                await _process_request(request, item)
        except GatewayError as exc:
            if not await _recover_trip(request, item, travel, asked, exc.code):
                raise
        else:
            if (
                item.state != "done"
                and item.erreurs
                and not await _recover_trip(
                    request, item, travel, asked, item.erreurs[-1]
                )
                and item.erreurs[-1] == "answer_time"
            ):
                # Not answered from the pages either: the person reads the
                # deadline it ran into (D5).
                item.erreurs.append("timeout")
        if quote_gate is not None:
            await quote_gate.flush()
        if date_gate is not None:
            await date_gate.flush()
        if script_gate is not None:
            await script_gate.flush()
    finally:
        if gating is not None:
            sink_context.reset(gating)
        if dating is not None:
            sink_context.reset(dating)
        if scripting is not None:
            sink_context.reset(scripting)
        document_quotes.current.reset(quoting)
        user_urls.reset(supplied)
        # The routing call is part of this turn's cost.
        routing_cost = request.__dict__.get("model_route_cost_eur")
        if isinstance(routing_cost, Decimal) and routing_cost > 0:
            item.cout_eur = float(Decimal(str(item.cout_eur)) + routing_cost)
        if held is not None:
            sink_context.reset(held)
        grounding.current.reset(checking)
        travel_claims.current.reset(traveling)
        number_provenance.current.reset(tracking)
        context_usage.parts.reset(components)
        context_usage.samples.reset(capture)
    if interface_task:
        # Raw JSON for the interface: no answer check applies to it.
        return
    from services.orchestrator.source_truth import clean_citation_residue

    item.reponse = clean_citation_residue(item.reponse)
    item.reponse = verified_images(
        item.reponse,
        {str(image["reference"]) for image in item.images if "reference" in image},
    )
    if (
        quotes is not None
        and quote_mode != "off"
        and quotes.active()
        and item.state == "done"
    ):
        item.reponse, quote_counts = document_quotes.checked(
            quotes, item.reponse, quote_language, request.user_text
        )
        document_quotes.log(quote_counts, quotes, item.task_type)
    # An answer that restates a provided source (an attached document, a pasted
    # text) is checked against it (source_faithfulness.py): unsupported
    # statements are rewritten once when the answer has not been sent yet, and
    # what remains is noted. A project turn's own path reads retrieved
    # passages, not a provided source (a file uploaded in a project comes here
    # through project_chat's scoped request); a Work file is checked by
    # deliverable_check. A turn that shows a picture is not checked: the answer
    # reads the picture, which the text the judge receives does not hold, so
    # every fact taken from it would be called unsupported.
    if (
        quotes is not None
        and source_faithfulness.enabled()
        and item.state == "done"
        and item.task_type not in {"work", "nearby"}
        and not request.project_id
        and not request.code_mode
        and not any(message.images for message in request.messages)
        and (provided := source_faithfulness.provided(quotes)) is not None
    ):
        from services.orchestrator.deadline import remaining as time_left

        # Text already sent to the interface cannot be replaced: only a note
        # can follow it. A held answer, an API answer and a JSON answer are
        # sent after the checks.
        streamed = bool(request.atlas_ui and holder is not None and holder.passed)
        checked_answer, addition, faith_report = await source_faithfulness.review(
            item.reponse,
            provided,
            request=request.user_text,
            profile=request.model,
            # The note follows the answer's language, not the pasted
            # document's.
            language=detected_language(item.reponse) or quote_language,
            streamed=streamed,
            seconds=time_left(),
        )
        item.reponse = source_faithfulness.joined(checked_answer, addition)
        if addition and streamed and (current_sink := sink_context.get()):
            await current_sink({"delta": {"content": "\n\n" + addition}})
        item.cout_eur = float(Decimal(str(item.cout_eur)) + faith_report.cost_eur)
        item.steps.append({"phase": "source_check", **faith_report.metadata()})
    # A Work answer describes the file it delivered: its sentences are not factual
    # claims to match against the pages read ("no source mentions this entity" was
    # put in front of delivered documents, staging 2026-11-10). A nearby answer is
    # rendered by the service from map data; its verification searches only feed
    # the opening status (nearby_chat), they are not sources of its sentences.
    # Post-checks, in order: document quotes (above), unsourced details, travel
    # values, relative dates, grounding. Travel durations, distances and times belong to the
    # travel check alone: the details review sees them as the person's own, so
    # one value is never rewritten twice.
    reviewable = (
        check is not None
        and item.state == "done"
        and item.task_type not in {"work", "nearby"}
    )
    travel_checked = (
        travel.active
        and (travel.held or sink is None)
        and item.state == "done"
        and item.task_type not in {"work", "nearby"}
    )
    if (
        reviewable
        and check is not None
        and unsourced_details.mode() != "off"
        and not request.has_attachments
        and not request.project_id
    ):
        # Precise details no retrieved text holds: looked up in the subject's
        # encyclopedia article, then corrected or removed by one bounded call
        # (unsourced_details.py). Grounding then checks the final text.
        item.reponse, details_report = await unsourced_details.review(
            item.reponse,
            # The passages of the instance's own corpus are retrieved texts too.
            check.evidence
            + [str(chunk.get("text", "")) for chunk in item.chunks_recuperes],
            # The conversation's own words (earlier answers included) are not
            # added details; an attachment's or a project's files are never in
            # the evidence read here, so such a turn is left to grounding alone.
            [m.text for m in request.messages]
            + ([travel.owned_values(item.reponse)] if travel_checked else []),
            profile=request.model,
            language=asked_language,
            question=asked,
            urls=item.retrieved_urls,
        )
        item.retrieved_urls.extend(details_report.reference_urls)
        item.cout_eur = float(Decimal(str(item.cout_eur)) + details_report.cost_eur)
    if travel_checked:
        # What no source gives is left out; the answer is then rewritten from
        # the sourced facts by one bounded call and checked again, or built by
        # code (travel_rebuild).
        from services.orchestrator import travel_rebuild

        item.reponse, rebuild_report = await travel_rebuild.review(
            travel,
            item.reponse,
            asked_language or travel.language,
            question=asked,
            profile=request.model,
        )
        item.cout_eur = float(Decimal(str(item.cout_eur)) + rebuild_report.cost_eur)
        item.off_trip_urls = sorted(
            travel_rebuild.unrelated(travel, asked, item.reponse)
        )
        from services.orchestrator import transit_planner

        # A doubtful plan is a fallback after what the pages read support.
        item.reponse = transit_planner.doubtful_answer(
            item.reponse, asked_language or travel.language
        )
        # A plan's attribution stays visible (Transitous usage policy).
        item.reponse = transit_planner.with_attribution(
            item.reponse, travel.computed, asked_language or travel.language
        )
    # A relative date or weekday that disagrees with the calendar (relative_dates).
    item.reponse = relative_dates.repair(
        item.reponse, answer_language, today, asked=asked_days
    )
    item.reponse, script_counts = stray_script.clean(
        item.reponse, answer_language, own_scripts
    )
    stray_script.log(script_counts, item.task_type)
    if reviewable and check is not None and check.evidence:
        # What the person gave (their messages, the files of the turn) is a
        # source of the answer, never a missing entity (grounding.provided).
        item.reponse = check.check(
            item.reponse,
            asked,
            asked_language,
            messages=[m.text for m in request.messages if m.role == "user"],
            documents=[text for text, _ in quotes.provided]
            if quotes is not None
            else [],
        )
    if provenance is not None and item.state == "done" and item.task_type != "work":
        number_provenance.record(
            provenance,
            item.reponse,
            [m.text for m in request.messages],
            item.task_type,
        )
    if item.state == "done" and not item.reponse.strip():
        item.erreurs.append("empty_answer")
        notices = {
            "en": "I could not produce an answer for this request.",
            "fr": "Je n’ai pas pu produire de réponse à cette demande.",
            "de": "Ich konnte keine Antwort auf diese Anfrage erstellen.",
            "es": "No pude producir una respuesta a esta solicitud.",
            "it": "Non ho potuto produrre una risposta a questa richiesta.",
        }
        language = conversation_language(
            request.conversation_messages, request.ui_locale
        )
        item.reponse = notices.get(language, notices["en"])
        if sink := sink_context.get():
            await sink({"delta": {"content": item.reponse}})


# Failures of the model's answer itself, not refusals: budget, quota, privacy
# and deadline refusals keep their own explanation (D5).
_ANSWER_FAILURES = frozenset(
    {
        "provider_error",
        "empty_completion",
        "empty_content_after_retry",
        "incomplete_provider_answer",
        "promise_only_final_answer",
        "malformed_tool_call",
        "unexpected_tool_history_answer",
        # The answer call ran past its share of the time (loop.answer_share);
        # the turn's own deadline ("timeout") is a refusal, never recovered.
        "answer_time",
    }
)
# What a trip's answer call leaves for the checks and, when it fails, for the
# answer written from the pages read (measured: 1 to 3 s per rewrite).
TRIP_RECOVERY_SECONDS = 20.0


async def _recover_trip(
    request: ChatRequest,
    item: Interaction,
    travel: travel_claims.Turn,
    asked: str,
    reason: str,
) -> bool:
    """A trip answer that failed after pages were read is written from them.

    Staging -102: the answer model wrote its source record instead of an
    answer (9453 characters, cut at the output cap), its recovery attempt
    failed the same way, and the person read "an unexpected error" although
    the pages read gave the trains. True when the answer was recovered.
    """
    if (
        reason not in _ANSWER_FAILURES
        or not travel.active
        or item.task_type in {"work", "nearby"}
    ):
        return False
    from services.orchestrator import travel_rebuild

    outcome = await travel_rebuild.recover(
        travel,
        travel.language,
        question=asked,
        profile=request.model,
    )
    if outcome is None:
        return False
    item.reponse, report = outcome
    item.state = "done"
    if reason not in item.erreurs:
        item.erreurs.append(reason)
    item.cout_eur = float(Decimal(str(item.cout_eur)) + report.cost_eur)
    return True


class _HeldAnswer:
    """Withhold answer text while a turn is checked; pass every other event.

    A factual turn is held from the start. A travel turn is known only after
    routing: ``hold`` starts holding then, and succeeds only while no answer
    text has gone out yet.
    """

    def __init__(self, sink: Sink, *, holding: bool = True) -> None:
        self.sink = sink
        self.holding = holding
        self.passed = False

    def hold(self) -> bool:
        if not self.passed:
            self.holding = True
        return self.holding

    async def __call__(self, event: dict[str, object]) -> None:
        delta = event.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            if self.holding:
                # The text waits for the check; a phase it carries still goes.
                rest = {key: value for key, value in event.items() if key != "delta"}
                if rest:
                    await self.sink(rest)
                return
            if delta["content"]:
                self.passed = True
        await self.sink(event)


class _RoutedStreamBuffer:
    def __init__(self, sink: Sink) -> None:
        self.sink = sink
        self.events: list[dict[str, object]] = []
        self.characters = 0
        self.released = False

    async def __call__(self, event: dict[str, object]) -> None:
        delta = event.get("delta")
        content = delta.get("content") if isinstance(delta, dict) else None
        if not isinstance(content, str) or self.released:
            await self.sink(event)
            return
        self.events.append(event)
        self.characters += len(content)
        if self.characters >= 400:
            await self.release()

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        for event in self.events:
            await self.sink(event)
        self.events.clear()

    def discard(self) -> None:
        self.events.clear()


async def _process_request(request: ChatRequest, item: Interaction) -> None:
    from services.orchestrator.closing import LIMIT_CLOSED

    LIMIT_CLOSED.set("")
    if await dispatch_project(request, item):
        return
    from services.orchestrator.user_memory import prepare as prepare_user_memory

    if await prepare_user_memory(request, item):
        return
    if request.code_mode:
        from services.orchestrator.code_mode import process_code

        await process_code(
            request.model_copy(
                update={
                    "lang": conversation_language(
                        request.conversation_messages, request.ui_locale
                    )
                }
            ),
            item,
        )
        return
    from services.orchestrator.narration import clean_answer
    from services.orchestrator.title import is_title_task, title_answer

    # Open WebUI sends its title task to the same OpenAI-compatible endpoint.
    # It is auxiliary UI work, outside the agent loop: one bounded call to the
    # fast profile (ATLAS_MODEL_TITLES=1), or the question's own words.
    if is_title_task(request.user_text):
        answer, cost = await title_answer(request.user_text, request.lang)
        item.reponse = answer
        item.state, item.task_type, item.cout_eur = "done", "title", float(cost)
        return
    # "Remind me tomorrow at 9 to ...": code reads the date and time and returns a
    # calendar file with an alarm, without a model call (no routing call either).
    from services.orchestrator.reminder_route import reminder_turn

    if await reminder_turn(request, item):
        return
    # The model decides what this plain chat turn asks for (ATLAS_MODEL_ROUTER=1),
    # once, before any gate reads it; None leaves the lexical gates unchanged.
    from services.orchestrator import request_router

    if request_router.enabled():
        before = request.timeout_seconds
        if await request_router.route(request) is not None:
            from services.orchestrator.deadline import extend

            # The request deadline was set before routing: an app, a drawing in
            # the panel or a file turn gets its 300 s.
            extend(request.timeout_seconds - before)
    language = conversation_language(request.conversation_messages, request.ui_locale)
    if not request.continuation and is_appearance_request(
        request.user_text, history=request.messages[:-1]
    ):
        language = requested_language(request.user_text) or language
    request = request.model_copy(update={"lang": language})
    # After the interface's own auxiliary tasks (title) and with the conversation
    # language known: a turn that needs running code or a real file goes to Work.
    from services.orchestrator.chat_work import escalate_to_work

    if await escalate_to_work(request, item):
        return
    # The chat answers this turn: its own deadline rule applies again. With the
    # Work deadline the chat limits were refused and the person got a raw
    # failure (staging 2026-11-10, a presentation asked after a Work conflict).
    request.__dict__["work_declined"] = True
    # A turn whose answer depends on where the person is: the stated place, the
    # consented browser position, or one short question; never a guessed city.
    from services.orchestrator.location_route import location_turn

    if not request.continuation and await location_turn(request, item):
        return
    from services.orchestrator.link_requests import own_words

    # The classifying words are read in the person's own words, never in a
    # pasted document; the hard limits then read the whole message (owner
    # ruling 2026-11-17).
    if (
        extended_enabled()
        and not request.has_attachments
        and extended_request(own_words(request.user_text))
    ):
        if category := text_limit(request.user_text):
            item.reponse = text_refusal(category, request.lang)
            item.state, item.task_type, item.cout_eur = "done", "text", 0.0
            if sink := sink_context.get():
                await sink({"delta": {"content": item.reponse}})
            return
        # An API caller named its model: that model answers, never another one
        # chosen for the content of the request.
        profiles = (
            ()
            if request._api_key_request
            else tuple(
                profile for profile in extended_profiles() if profile in PROFILES
            )
        )
        if profiles:
            request = request.model_copy(update={"model": profiles[0]})
            request_started = time.monotonic()
            stream = sink_context.get()
            buffered = _RoutedStreamBuffer(stream) if stream is not None else None
            token = sink_context.set(buffered) if buffered is not None else None
            try:
                await _process(request, item)
            finally:
                if token is not None:
                    sink_context.reset(token)
            first_cost = Decimal(str(item.cout_eur))
            can_retry = len(profiles) > 1 and (
                buffered is None or not buffered.released
            )
            if can_retry and (not item.reponse.strip() or short_refusal(item.reponse)):
                remaining = Decimal("0.10") - first_cost
                if remaining > 0:
                    if buffered is not None:
                        buffered.discard()
                    retry_request = request.model_copy(update={"model": profiles[1]})
                    await _process(
                        retry_request,
                        item,
                        cost_limit=remaining,
                        wall_clock_limit=max(
                            0,
                            request.timeout_seconds
                            - (time.monotonic() - request_started),
                        ),
                    )
                    item.cout_eur += float(first_cost)
            if buffered is not None and not buffered.released:
                await buffered.release()
        else:
            await _process(request, item)
    else:
        await _process(request, item)
    partial_searches = tuple(
        dict.fromkeys(
            error.removeprefix("partial_search_failure:")
            for error in item.erreurs
            if error.startswith("partial_search_failure:")
        )
    )
    failed_searches = tuple(
        dict.fromkeys(
            outcome
            for outcome in item.erreurs
            if outcome in OUTCOMES and outcome not in partial_searches
        )
    )
    from services.orchestrator.closing import LIMIT_CLOSED

    # A turn closed for a search limit already says so in its answer.
    if (
        item.state == "done"
        and (failed_searches or partial_searches)
        and not LIMIT_CLOSED.get()
    ):
        notice = "\n\n" + "\n".join(
            [
                *(
                    search_failure_notice(outcome, request.lang)
                    for outcome in failed_searches
                ),
                *(
                    partial_search_failure_notice(outcome, request.lang)
                    for outcome in partial_searches
                ),
            ]
        )
        item.reponse += notice
        if sink := sink_context.get():
            await sink({"delta": {"content": notice}})
    if (
        item.state == "done"
        and any(not d.reused for d in request.documents)
        and any(d.get("hierarchical_synthesis") for d in item.documents)
    ):
        from services.orchestrator.attachment_reading import reduced_document_note

        notice = reduced_document_note(request.lang)
        if not item.reponse.endswith(notice):
            item.reponse += "\n\n" + notice
            if sink := sink_context.get():
                await sink({"delta": {"content": "\n\n" + notice}})
    if item.state == "done" and item.task_type not in {
        "imagegen",
        "vision",
        "visual_tools",
    }:
        from services.orchestrator.source_truth import (
            drawing_references,
            inline_source_markers,
            without_unretrieved,
            source_footer,
            visible_sources,
        )

        from services.orchestrator.image_truth import verified_images

        item.reponse = verified_images(
            item.reponse,
            {str(image["reference"]) for image in item.images if "reference" in image},
        )
        result_before_guard = item.reponse
        item.reponse = without_unretrieved(
            clean_answer(item.reponse),
            set(item.retrieved_urls)
            | drawing_references(item)
            | set(item.tool_urls)
            | {str(c.get("source")) for c in item.citations}
            | set(
                re.findall(
                    re.escape(
                        os.environ.get(
                            "ATLAS_PUBLIC_URL",
                            atlas_ports().url("adapter", "localhost"),
                        ).rstrip("/")
                    )
                    + r"/sources/[a-f0-9]{64}",
                    item.reponse,
                )
            ),
        )
        sources = visible_sources(item)
        from services.orchestrator.link_requests import (
            fallback_terms,
            page_request,
            unavailable,
        )
        from services.orchestrator.citation_cleanup import hollow

        if (
            not request.project_id
            and page_request(
                request.user_text, tuple(m.text for m in request.messages[:-1])
            )
            and hollow(clean_answer(result_before_guard), item.reponse)
        ):
            item.reponse = unavailable(
                fallback_terms(
                    request.user_text, tuple(m.text for m in request.messages[:-1])
                ),
                request.lang,
            )
        item.reponse = inline_source_markers(
            item.reponse,
            sources,
            preserve_labels=page_request(
                request.user_text, tuple(m.text for m in request.messages[:-1])
            ),
        )
        # A trip answer lists the pages about its trip and the pages it cites,
        # not every search hit (travel_rebuild.unrelated).
        hidden = set(item.off_trip_urls)
        listed = [url for url in sources if url not in hidden or url in item.reponse]
        if request.atlas_ui:
            item.reponse += source_footer(listed, request.lang)
        elif sources and not any(url in item.reponse for url in sources):
            # An API answer whose citations were all removed as unretrieved kept
            # no source at all (staging battery 2026-11-12): the pages actually
            # consulted are listed instead.
            item.reponse += source_footer(listed or sources, request.lang)


# The ceiling of a simple drawing turn (the ordinary chat ceiling).
DRAWING_COST = Decimal("0.10")


async def _process_simple_drawing(request: ChatRequest, item: Interaction) -> None:
    """Use one text-model pass for a guest SVG, with no search or offered tools."""
    started = time.monotonic()
    model = await GatewayModel.connect(
        os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway")),
        local_enabled=os.environ.get("GPU_LOCAL", "0") == "1",
    )
    model.tools = []
    model.observing = True
    model.sink = None
    # The drawing is one whole SVG written in one call: the gateway and the loop
    # allow it the turn's longer deadline only with the file flag. configure_quality
    # sets the flag, so it is given here (setting it before was silently reset:
    # staging -44, en-draw-10, the repair call refused as invalid_limits).
    model.configure_quality(request.model, request.max_tokens, produces_files=True)
    # The file flag also raises the model's allowance to the file ceiling (0.30 EUR),
    # above this turn's 0.10 EUR limit: the first reservation stopped the turn at
    # once as a budget refusal (staging -45). The drawing keeps its own ceiling.
    model.cost_ceiling = DRAWING_COST
    model.local_enabled = os.environ.get("GPU_LOCAL", "0") == "1"
    tools = ChatTools(item, request.user_text, allow_image=False)
    result = await run(
        Query(
            question=request.messages[-1].text,
            lang=request.lang,
            prose_lang=request.prose_lang,
        ),
        model,
        tools,
        Path("prompts/chat.txt").read_text()
        + Path("prompts/chat-agent.txt").read_text()
        + Path("prompts/web-chat.txt").read_text()
        + language_instruction(request.prose_lang)
        + guest_capability_instruction()
        + (
            spatial_drawing_instruction()
            if is_spatial_drawing_request(request.user_text)
            else simple_drawing_instruction()
        ),
        Limits(
            profile=request.model,
            tool_calls=0,
            tokens=None,
            cost=DRAWING_COST,
            wall_clock=max(0, request.timeout_seconds - (time.monotonic() - started)),
            # The drawing is one whole SVG written in one call, like an app or a
            # picture turn, whose 300 s deadline this turn may carry: without the
            # flag the limits refused it as invalid (staging 2026-11-14).
            produces_files=True,
        ),
        history=[
            {"role": message.role, "content": message.text}
            for message in request.messages[:-1]
        ],
        include_intermediate=request.atlas_ui,
        retry_web=False,
        initial_calls=(),
    )
    if result.state == "done":
        await diagram.complete_svg(
            request,
            result,
            model,
            tools,
            started,
            DRAWING_COST,
            request.timeout_seconds,
            require_legend=False,
        )
    item.cout_eur = float(result.cost)
    if result.state == "done":
        await finish_simple_drawing(request, item, result)
    else:
        item.reponse, item.state = result.text, result.state
        if result.reason:
            item.erreurs.append(result.reason)


def _pictures_by_code() -> bool:
    """Pictures by code are on and their steps have their profiles here."""
    from services.orchestrator.pictures.turn import available

    return available()


async def _picture_turn(
    request: ChatRequest,
    item: Interaction,
    cost_limit: Decimal | None,
    wall_clock_limit: float | None,
    store_artifact: bool,
) -> None:
    from services.orchestrator.pictures.turn import process_picture_turn

    await process_picture_turn(
        request,
        item,
        cost_limit=cost_limit,
        wall_clock_limit=wall_clock_limit,
        store_artifact=store_artifact,
    )


async def _process(
    request: ChatRequest,
    item: Interaction,
    *,
    cost_limit: Decimal | None = None,
    wall_clock_limit: float | None = None,
) -> None:
    from services.orchestrator.person_lookup import person_name, lookup

    artifact_history: list[dict[str, object]] = [
        {"role": message.role, "content": message.text} for message in request.messages
    ]
    artifact_eligible = (
        os.environ.get("ATLAS_ARTIFACTS") == "1"
        and bool(getattr(request, "artifact_owner", ""))
        and not request.continuation
        and not request.has_attachments
        and not any(message.images for message in request.messages)
    )
    artifact_current = (
        current_artifact_message(
            artifact_history, getattr(request, "artifact_owner", "")
        )
        if artifact_eligible
        else None
    )
    from services.orchestrator import pictures

    if pictures.enabled() and not request.continuation and not request.has_attachments:
        from services.orchestrator.pictures.choice import (
            current_after_click,
            handle_choice,
        )

        # "The second" (in any interface language) right after four candidates chooses one.
        if await handle_choice(request, item):
            return
        if artifact_eligible:
            artifact_current = await asyncio.to_thread(
                current_after_click, request, artifact_current, item.request_id
            )
    from services.orchestrator import request_router

    # The model routing decision (ATLAS_MODEL_ROUTER=1) is read first; the
    # lexical tests below decide only when there is none.
    routed = request_router.decision(request)
    picture = None
    if artifact_eligible and artifact_current is not None and _pictures_by_code():
        from services.orchestrator.pictures import dialogue as picture_dialogue

        picture = await asyncio.to_thread(
            picture_dialogue.picture_of, request, artifact_current
        )
        if picture is not None:
            # A follow-up on the conversation's picture (a change, a retry, a
            # critique) takes the picture path, whatever its wording.
            legacy_change = (
                routed.kind == "drawing" and routed.follow_up
                if routed is not None
                else app_intent(request.user_text, artifact_history) == "modify"
                or "dessine" in request.user_text.casefold()
            )
            if await picture_dialogue.follow_up(
                request,
                item,
                artifact_current,
                picture,
                routed,
                legacy_change=legacy_change,
                cost_limit=cost_limit,
                wall_clock_limit=wall_clock_limit,
            ):
                return
            # A picture is never handed to the HTML artifact editor.
            artifact_current = None
    if routed is not None:
        artifact_intent = (
            request_router.app_route(
                request, routed, has_artifact=artifact_current is not None
            )
            if artifact_eligible
            else None
        )
    else:
        artifact_intent = (
            app_intent(request.user_text, artifact_history)
            if artifact_eligible
            else None
        )
        if (
            artifact_eligible
            and artifact_current is not None
            and "dessine" in request.user_text.casefold()
        ):
            artifact_intent = "modify"

    if picture is not None and artifact_intent == "modify":
        # The current artifact was a picture: anything else is a new one.
        artifact_intent = "create"
    if (
        not request.continuation
        and not request.has_attachments
        and not any(m.images for m in request.messages)
        and (name := person_name(request.user_text))
    ):
        tools = ChatTools(item, request.user_text, allow_image=False)
        tools.daily_ledger = getattr(request, "daily_ledger", None)
        tools.daily_user = getattr(request, "daily_user", "unknown")
        tools.search_identities = getattr(request, "search_identities", ())
        tools.search_conversation = getattr(request, "native_chat_id", "")
        tools.daily_lang = request.lang
        await lookup(request, item, tools, name)
        return
    if artifact_intent is not None:
        # The file is written in the streamed answer, never in tool arguments,
        # and a follow-up changes the conversation's current artifact.
        item.task_type = "artifact"
        drawing_kind = (
            routed.kind == "drawing"
            if routed is not None
            else svg_requested(request.user_text)
        )
        if (
            artifact_intent == "modify"
            and artifact_current is not None
            and _pictures_by_code()
        ):
            from services.orchestrator.pictures.followup import (
                process_picture_followup,
            )

            # A change to a picture by code goes through the picture chain.
            if await process_picture_followup(
                request,
                item,
                artifact_current,
                cost_limit=cost_limit,
                wall_clock_limit=wall_clock_limit,
            ):
                return
        if artifact_intent == "create" and drawing_kind and _pictures_by_code():
            await _picture_turn(request, item, cost_limit, wall_clock_limit, True)
            return
        await process_app_turn(
            request,
            item,
            intent=artifact_intent,
            kind="drawing" if drawing_kind else "app",
            current=artifact_current,
            cost_limit=cost_limit,
            wall_clock_limit=wall_clock_limit,
        )
        return
    technical_diagram = not request.continuation and diagram.requested(
        request.user_text
    )
    explaining_image = image_explanation(request.user_text) and (
        image_topic(request.user_text) or len(image_dialogue(request.messages)) > 1
    )
    if (
        images_enabled()
        and artifact_intent is None
        and not technical_diagram
        and not is_spatial_drawing_request(request.user_text)
        and not explaining_image
        and not request.continuation
        and not request.has_attachments
        and not any(message.images for message in request.messages)
        and (
            (
                routed.kind == "drawing"
                and not is_appearance_request(
                    request.user_text, history=request.messages[:-1]
                )
            )
            if routed is not None
            else explicit_image_request(request.user_text)
            or image_iteration(request.messages)
        )
    ):
        dialogue = image_dialogue(request.messages)
        sink = sink_context.get()
        if sink:
            await sink({"phase": "tool_started", "tool": "generate_image", "urls": []})
        succeeded = False
        try:
            await process_image(
                " ".join(dialogue)
                if len(dialogue) == 1 or raw_scene_continuation(request.messages)
                else visual_image_prompt(dialogue),
                item,
                request.seed,
                language=request.lang,
                dialogue=None
                if image_retry_confirmation(request.messages)
                else dialogue,
                medium=image_medium(request.messages) if len(dialogue) > 1 else None,
                timeout=min(118, request.timeout_seconds),
            )
            succeeded = True
        finally:
            if sink:
                await sink(
                    {
                        "phase": "tool_finished",
                        "tool": "generate_image",
                        "urls": [],
                        "ok": succeeded,
                    }
                )
        return
    # Attachments take precedence over project/corpus commands and lexical routing.
    if request.documents and (
        os.environ.get("ATLAS_TERMINAL_ENABLED") != "1"
        or any(message.images for message in request.messages)
    ):
        from services.orchestrator.document_chat import process_documents

        await process_documents(request, item)
        return
    from services.orchestrator.capability_strategy import (
        strategy,
        finish as finish_visual,
    )

    if not request.continuation and not request.has_attachments:
        visual_calls = strategy(request.user_text)
        if visual_calls and artifact_intent is None:
            await finish_visual(request, item, visual_calls)
            return
    drawing = (
        not request.continuation
        and not request.has_attachments
        and (
            # A drawing the app panel does not take (no panel here, API calls)
            # is drawn as SVG in the answer, never refused.
            routed.kind == "drawing"
            and not technical_diagram
            and (
                is_spatial_drawing_request(request.user_text)
                or request_router.svg_drawing(request, routed)
            )
            if routed is not None
            else (
                is_spatial_drawing_request(request.user_text)
                or (
                    not images_enabled()
                    and not explicit_image_request(request.user_text)
                )
            )
            and is_simple_drawing_request(request.user_text)
        )
        and artifact_intent is None
    )
    if (
        drawing
        and artifact_intent is None
        and not is_spatial_drawing_request(request.user_text)
        and _pictures_by_code()
    ):
        await _picture_turn(
            request, item, cost_limit, wall_clock_limit, artifact_eligible
        )
        return
    if drawing and artifact_intent is None:
        request = request.model_copy(
            update={"lang": simple_drawing_language(request.user_text, request.lang)}
        )
        await _process_simple_drawing(request, item)
        return
    if (
        routed is None
        and not images_enabled()
        and artifact_intent is None
        and explicit_image_request(request.user_text)
    ):
        if _pictures_by_code() and not is_spatial_drawing_request(request.user_text):
            # With pictures by code a drawing is made, never refused.
            await _picture_turn(
                request, item, cost_limit, wall_clock_limit, artifact_eligible
            )
            return
        # Lexical routing only: a routed drawing never reaches this refusal.
        item.reponse, item.state, item.task_type = (
            unavailable_reply(request.lang),
            "done",
            "text",
        )
        sink = sink_context.get()
        if sink:
            await sink({"delta": {"content": item.reponse}})
        return
    from services.orchestrator.link_requests import page_request, instruction

    links = (
        page_request(request.user_text, tuple(m.text for m in request.messages[:-1]))
        and not request.continuation
    )
    followup = not links and not request.continuation and is_followup(request.messages)
    appearance = (
        not links
        and not request.continuation
        and not technical_diagram
        and not request.has_attachments
        and is_appearance_request(request.user_text, history=request.messages[:-1])
    )
    if followup:
        item.task_type = "followup"
    iteration = image_iteration(request.messages)
    images_in_history = any(message.images for message in request.messages)
    from services.orchestrator.location_route import asked_text

    discovery = (
        required_research(
            asked_text(request),
            request.lang,
            device_timings="tavily" in search_providers(),
            history=tuple(m.text for m in request.messages[:-1]) if links else (),
        )
        if not request.has_attachments and (not followup or images_in_history)
        else ()
    )
    # The original question still drives policy classification, but a UI retry is
    # only completing already-started prose: it must not begin another route.
    # Research cannot be required where the level does not offer web search
    # (Strict): the turn answers from the tools it has, never a search refusal.
    if request.continuation or appearance or not privacy.tool_allowed("web_search"):
        discovery = ()
    image_context: str | None = None
    image_cost = Decimal(0)
    # The device-timings heuristic (short question with a capitalised token) must not
    # turn an image instruction such as "FAIS UN MONTAGE" into a research request.
    image_research = bool(
        images_in_history
        and not request.has_attachments
        and discovery
        and required_research(request.user_text, request.lang)
    )
    if image_research:
        # A required tool cannot receive pixels. Read them first, then retain the
        # vision result as explicitly untrusted evidence for the tool loop.
        labels = {
            "fr": "Lis l'image et transcris les valeurs visibles utiles pour répondre à cette demande avant toute recherche.",
            "en": "Read the image and transcribe the visible values needed to answer this request before any research.",
            "de": "Lies das Bild und schreibe die sichtbaren Werte auf, die für diese Anfrage vor jeder Recherche nötig sind.",
            "es": "Lee la imagen y transcribe los valores visibles necesarios para responder a esta solicitud antes de buscar.",
            "it": "Leggi l'immagine e trascrivi i valori visibili necessari per rispondere a questa richiesta prima di cercare.",
        }
        last = request.messages[-1]
        content: object
        if isinstance(last.content, list):
            content = [
                *last.model_dump()["content"],
                {"type": "text", "text": labels[request.lang]},
            ]
        else:
            content = last.content + "\n\n" + labels[request.lang]
        vision_request = request.model_copy(
            update={
                "messages": [
                    *request.messages[:-1],
                    ChatMessage.model_validate(
                        {
                            **last.model_dump(),
                            "content": content,
                        }
                    ),
                ]
            }
        )
        vision_item = Interaction()
        try:
            await process_vision(vision_request, vision_item, emit=False)
        except (GatewayError, TimeoutError, ValueError, aiohttp.ClientError) as exc:
            from services.orchestrator.document_chat import vision_failure_notice

            item.images = [
                image for message in request.messages for image in message.images
            ]
            item.cout_eur = vision_item.cout_eur
            item.reponse = vision_failure_notice(request.lang, exc)
            item.state, item.task_type = "done", "vision"
            return
        image_context, image_cost = (
            vision_item.reponse,
            Decimal(str(vision_item.cout_eur)),
        )
    elif images_in_history and not request.has_attachments and artifact_intent is None:
        await process_vision(request, item)
        return
    started = time.monotonic()
    model = await GatewayModel.connect(
        os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway")),
        local_enabled=os.environ.get("GPU_LOCAL", "0") == "1",
    )
    if followup and os.environ.get("ATLAS_TERMINAL_ENABLED") != "1":
        model.tools = []
    elif (
        not appearance
        and not technical_diagram
        and images_enabled()
        and not explaining_image
    ):
        model.tools.append(declaration())
    if os.environ.get("ATLAS_TERMINAL_ENABLED") == "1":
        model.tools.extend(json.loads(Path("prompts/file-tools.json").read_text()))
        if request.has_attachments:
            model.tools = [
                t
                for t in model.tools
                if isinstance(function := t.get("function"), dict)
                and function.get("name") != "rag_search"
            ]
    if not corpus_allowed(request.user_text):
        model.tools = [
            tool
            for tool in model.tools
            if isinstance(function := tool.get("function"), dict)
            and function.get("name") != "rag_search"
        ]
    from services.orchestrator.capability_strategy import relevant as visual_relevant
    from services.orchestrator.visual_tools import declarations as visual_declarations

    if visual_relevant(request.user_text):
        model.tools.extend(visual_declarations())
    from services.orchestrator import exact_tools

    # Dates, times, units and currencies are computed by code (T8). Offered on
    # the turns about them only: other turns keep their recorded requests.
    earlier = [m.text for m in request.conversation_messages[:-1] if m.role == "user"]
    exact_subjects = exact_tools.relevant(
        request.user_text, earlier[-1] if earlier else ""
    )
    if exact_subjects:
        model.tools.extend(exact_tools.declarations(exact_subjects))
    from services.orchestrator import relative_dates

    # A question naming a relative day (tomorrow, the weekend, a weekday, in N
    # days) reads its dates from a calendar computed by code (T8). Offered on
    # those turns only: other turns keep their recorded requests.
    calendar_note = relative_dates.note(
        request.user_text,
        earlier[-1] if earlier else "",
        request.ui_timezone,
        request.prose_lang,
    )
    from services.orchestrator import journey

    # A trip from one place to another: a door-to-door plan whose times code
    # computes (journey), and every travel value checked (travel_claims).
    travel = travel_claims.current.get()
    routed_decision = request_router.decision(request)
    trip = journey.relevant(
        asked_text(request),
        earlier[-1] if earlier else "",
        located=bool(travel is not None and travel.place),
    )
    if travel is not None and (
        trip
        or travel.place
        or (routed_decision is not None and routed_decision.needs_location)
    ):
        travel.retrieved = lambda: list(item.retrieved_urls)
        travel.activate()
    from services.orchestrator import transit_planner

    # Public transport itineraries computed by a journey planner (MOTIS 2;
    # transit_planner), offered before the web path when it is configured.
    if (
        trip
        and transit_planner.enabled()
        and privacy.tool_allowed(transit_planner.NAME)
    ):
        model.tools.append(transit_planner.declaration())
    if trip and privacy.tool_allowed("web_search"):
        model.tools.append(journey.declaration())
        if travel is not None:
            travel.journey_offered = True
    # Specialized public sources (encyclopedia, news feeds, Eurostat, French
    # communes) before a general search (T8). Offered on the turns about them
    # only: other turns keep their recorded requests; web_search stays offered.
    source_families = source_tools.relevant(
        request.user_text, earlier[-1] if earlier else ""
    )
    if source_families:
        model.tools.extend(source_tools.declarations(source_families))
    if (
        "news" in source_families
        and [call.id for call in discovery] == ["required-current"]
        and privacy.allows(news_feeds.CAPABILITY)
    ):
        # A required current-events search reads the news feeds first; the
        # model may still search the web after reading them.
        from services.orchestrator.link_requests import own_words

        discovery = (
            Call(
                "required-news",
                "news_headlines",
                json.dumps(
                    {
                        # The person's own words, never a pasted text.
                        "topic": own_words(request.user_text)[:200],
                        "language": search_language(
                            request.conversation_messages, request.ui_locale
                        ),
                    },
                    ensure_ascii=False,
                ),
            ),
        )
    model.observing = True
    # The final image replaces the model prose for this route.  Holding its output
    # prevents an incomplete Mermaid or ASCII diagram reaching an SSE client first.
    # Image-policy answers are released only after the post-check. Otherwise an
    # unsupported refusal could already be visible through streaming.
    image_discussion = image_topic(request.user_text) or any(
        image_topic(message.text) for message in request.messages[-3:-1]
    )
    model.sink = (
        None
        if appearance or image_discussion or technical_diagram or links
        else sink_context.get()
    )
    delivery_buffer = diagram.DeliveryBuffer(model.sink) if model.sink else None
    if delivery_buffer is not None:
        model.sink = delivery_buffer
    from packages.file_intent import produces_file, requests_file_delivery

    produces_files = (
        routed.kind in ("file", "drawing")
        if routed is not None
        else produces_file(request.user_text)
    )
    turn_cost = Decimal("0.30" if request.has_attachments or produces_files else "0.10")
    from packages.file_intent import edits_attachment

    # Editing an attached file means returning it: keep the file tools (J43).
    deliver_files = (
        routed.kind == "file"
        if routed is not None
        else requests_file_delivery(request.user_text)
    ) or (bool(request.documents) and edits_attachment(request.user_text))
    # Only the turn that calls no tool is answer text: what the model writes
    # before a tool call is a working note, held back from the stream and left
    # out of the answer. A file turn keeps every part: its content may be
    # written before the call that saves it, and the fallbacks below read it.
    # So does a diagram turn, whose explanation may precede a picture call.
    answer_only = not (produces_files or deliver_files or technical_diagram)
    if answer_only:
        # A plain first turn streams from its first token; a research turn is
        # expected to call a tool, so even its first turn waits to be known.
        model.note_hold = (
            "turn" if routed is not None and routed.kind == "research" else "after_tool"
        )
    from services.orchestrator.file_content import requested_formats

    requested_file_formats = (
        request_router.formats(routed, requested_formats(request.user_text))
        if routed is not None
        else requested_formats(request.user_text)
    )
    structured_presentation = deliver_files and "pptx" in requested_file_formats
    if (
        os.environ.get("ATLAS_TERMINAL_ENABLED") == "1"
        and not produces_files
        and not request.has_attachments
    ):
        model.tools = [
            tool
            for tool in model.tools
            if isinstance(function := tool.get("function"), dict)
            and function.get("name") not in {"terminal_command", "publish_document"}
        ]
    if os.environ.get("ATLAS_STABLE") == "1":
        # Stable guests never receive model-authored shell or publication tools.
        # Deterministic presentation delivery below remains available.
        model.tools = without_model_code_tools(model.tools)
    if structured_presentation:
        # The terminal remains the delivery boundary, but model-authored tool use
        # cannot replace the validated outline path.
        model.tools = []
    if appearance:
        # Commons retrieval is service-owned. General research tools let the model
        # repeatedly announce a drawing between searches instead of delivering it.
        model.tools = []
    model.configure_quality(
        request.model,
        request.max_tokens,
        has_attachments=request.has_attachments,
        produces_files=produces_files,
    )
    model.cost_ceiling = cost_limit
    model.spent = image_cost
    item.reasoning_effort = request.reasoning_effort
    model.local_enabled = os.environ.get("GPU_LOCAL", "0") == "1"
    item.cout_eur = 0.30 if request.has_attachments or produces_files else 0.10
    # Reserved until ledger returns.
    tools = ChatTools(
        item,
        request.user_text,
        allow_image=not followup
        and not technical_diagram
        and images_enabled()
        and not explaining_image,
    )
    tools.daily_ledger = getattr(request, "daily_ledger", None)
    tools.daily_user = getattr(request, "daily_user", "unknown")
    tools.search_identities = getattr(request, "search_identities", ())
    tools.search_conversation = getattr(request, "native_chat_id", "")
    tools.daily_lang = request.lang
    if request.ui_timezone != "UTC":
        tools.home_zone = request.ui_timezone
    tools.search_language = search_language(
        request.conversation_messages, request.ui_locale
    )
    from services.orchestrator.connectors import prepare as prepare_connectors

    if not request._api_key_request:
        tools.connectors = await prepare_connectors(request)
    if tools.connectors is not None:
        tools.connectors.bind(model)
    from services.orchestrator.skill_tools import SkillTools, offer

    if os.environ.get("ATLAS_SKILLS_ENABLED") == "1":
        tools.skills = await SkillTools.connect(
            request._skill_catalog, telemetry=item.skills_loaded
        )
        if tools.skills.entries and not appearance:
            model.tools = offer(model.tools, tools.skills.declaration())
    tools.model = model
    tools.terminal_code_disabled = structured_presentation
    file_context = ""
    terminal_error = ""
    if tools.terminal is not None:
        paths = []
        try:
            await tools.terminal.initialize()
        except (RuntimeError, OSError) as exc:
            if not request.has_attachments:
                raise
            from packages.attachment_reader import failure_reason

            terminal_error = failure_reason(exc)
        if not terminal_error:
            paths = [
                await tools.terminal.upload(document, request.timeout_seconds)
                for document in request.documents
            ]
        file_context = (
            Path("prompts/files.txt").read_text()
            + "\n"
            + json.dumps(
                {"request_directory": tools.terminal.root, "attachments": paths},
                ensure_ascii=False,
            )
        )
        if structured_presentation:
            from services.orchestrator.presentation_delivery import OUTLINE_INSTRUCTION

            file_context += "\n" + OUTLINE_INSTRUCTION
    from services.orchestrator.attachment_reading import (
        document_status,
        failure_notice,
        read_announced,
        reading_progress,
    )

    attachment_sources = await read_announced(
        request,
        item,
        tools.terminal if not terminal_error else None,
        terminal_error,
        # Attached documents are always read in process: the sandbox may lack the
        # model's preferred PDF library, and an unread document invites invention.
        read_document_bytes=bool(request.documents),
    )
    if (tracker := number_provenance.current.get()) is not None:
        tracker.collect(attachment_sources)
    notice = failure_notice(attachment_sources, request.lang)
    if attachment_sources and model.sink:
        pages, failed_pages, ocr_pages = reading_progress(attachment_sources)
        await model.sink(
            {
                "phase": "document_read",
                "description": document_status(
                    pages, failed_pages, request.lang, ocr_pages
                ),
            }
        )
    if attachment_sources and not any(
        source.get("text") for source in attachment_sources
    ):
        item.reponse, item.state, item.cout_eur = notice, "done", 0.0
        if notice and model.sink:
            await model.sink({"delta": {"content": notice}})
        if tools.terminal is not None:
            item.terminal_commands = tools.terminal.commands
            item.terminal_failed_commands = tools.terminal.failed_commands
        return
    if attachment_sources and not deliver_files:
        model.tools = [
            tool
            for tool in model.tools
            if isinstance(function := tool.get("function"), dict)
            and function.get("name") not in {"terminal_command", "publish_document"}
        ]
    attachment_history: list[dict[str, object]] = [
        {"role": m.role, "content": m.text} for m in request.messages[:-1]
    ]
    if artifact_current is not None:
        attachment_history.append(dict(artifact_current.message))
    if image_context is not None:
        attachment_history.append(
            {
                "role": "user",
                "content": json.dumps(
                    {"untrusted_image_description": image_context},
                    ensure_ascii=False,
                ),
            }
        )
    if attachment_sources:
        from packages.table_arithmetic import arithmetic_instruction

        file_context = (
            (file_context if deliver_files else "")
            + Path("prompts/documents.txt").read_text()
            + arithmetic_instruction(attachment_sources)
        )
        attachment_history.append(
            {
                "role": "user",
                "content": json.dumps(
                    {"untrusted_attachments": attachment_sources}, ensure_ascii=False
                ),
            }
        )
    if attachment_sources:
        from packages.context_usage import register

        register(
            str(attachment_history[-1]["content"]),
            {"document_passages": attachment_sources},
        )
    try:
        if attachment_sources and model.sink:
            await model.sink({"phase": "synthesizing_document"})
        result = await run(
            Query(
                question=iteration or request.messages[-1].text,
                lang=request.lang,
                prose_lang=request.prose_lang,
            ),
            model,
            tools,
            Path("prompts/chat.txt").read_text()
            + Path("prompts/chat-agent.txt").read_text()
            + Path("prompts/web-chat.txt").read_text()
            + (
                Path("prompts/tavily.txt").read_text()
                if "tavily" in search_providers()
                else ""
            )
            + (Path("prompts/followup.txt").read_text() if followup else "")
            + language_instruction(request.prose_lang)
            + (instruction() if links else "")
            + (policy_instruction() if image_discussion else "")
            + extended_instruction()
            + (guest_capability_instruction() if not images_enabled() else "")
            + (diagram.instruction() if technical_diagram else "")
            + file_context
            + (
                legend_instruction()
                if appearance and images_enabled()
                else mechanical_svg_instruction(include_svg=mechanical_svg_enabled())
                if appearance
                else ""
            )
            + calendar_note,
            Limits(
                profile=request.model,
                tool_calls=0 if appearance else max(0, 10 - len(attachment_sources)),
                tokens=None,
                cost=cost_limit if cost_limit is not None else turn_cost,
                has_attachments=request.has_attachments,
                produces_files=produces_files,
                wall_clock=max(
                    0,
                    (
                        request.timeout_seconds
                        if wall_clock_limit is None
                        else wall_clock_limit
                    )
                    - (time.monotonic() - started),
                ),
            ),
            include_intermediate=request.atlas_ui,
            final_only=answer_only,
            history=attachment_history,
            skill_messages=tools.skills.instructions
            if tools.skills is not None
            else None,
            retry_web=True,
            initial_calls=discovery,
            terminal_tools=(
                frozenset({"generate_image"}) if not followup else frozenset()
            ),
        )
        if (
            appearance
            and not images_enabled()
            and mechanical_svg_enabled()
            and result.state == "done"
        ):
            await diagram.complete_svg(
                request,
                result,
                model,
                tools,
                started,
                cost_limit if cost_limit is not None else Decimal("0.10"),
                request.timeout_seconds
                if wall_clock_limit is None
                else wall_clock_limit,
                require_legend=True,
            )
        if (
            technical_diagram or diagram.has_diagram(result.text)
        ) and result.state == "done":
            await diagram.complete_diagram(
                request,
                item,
                result,
                model,
                tools,
                started,
                cost_limit if cost_limit is not None else turn_cost,
                request.timeout_seconds
                if wall_clock_limit is None
                else wall_clock_limit,
                attachment_history,
            )
        if (
            images_enabled()
            and image_discussion
            and unsupported_image_block(result.text, iteration or request.user_text)
        ):
            if not explaining_image and (
                wants_generation(request.user_text) or iteration
            ):
                # The invented block is dropped silently: the image is the answer.
                await finish(request, item, result, request.user_text, started)
                return
            result.text = short_correction(request.lang)
        from services.orchestrator.file_content import (
            answer_content,
            requested_formats,
            presentable,
        )

        formats = (
            requested_file_formats
            if routed is not None
            else requested_formats(request.user_text)
        )
        preserve_code = bool(
            re.search(
                r"\b(?:script|code|python|sql|function|fonction|programm)\b",
                request.user_text,
                re.I,
            )
        )
        file_recovery_attempted = False
        file_recovery_failed = False
        if structured_presentation and tools.terminal is not None:
            from services.orchestrator.presentation_delivery import deliver_presentation

            delivered = await deliver_presentation(
                model, request, item, result, tools.terminal, started, formats
            )
            if delivered is None:
                # D5: no second file-generation route can replace an honest failure.
                from services.orchestrator.presentation_delivery import (
                    outline_markdown,
                )

                file_recovery_failed = True
                result.text = outline_markdown(result.text)
            else:
                from services.orchestrator.presentation_delivery import (
                    outline_markdown,
                )

                result = delivered
                result.text = outline_markdown(result.text)
        if (
            deliver_files
            and tools.terminal is not None
            and not tools.terminal.files
            and tools.terminal.commands == 0
            and not file_recovery_failed
            and "terminal_operation_failed" not in item.erreurs
            and result.tool_calls <= 7
            and os.environ.get("ATLAS_STABLE") != "1"
        ):
            file_recovery_attempted = True
            first_result = result
            remaining_seconds = max(
                0, request.timeout_seconds - (time.monotonic() - started)
            )
            remaining_cost = max(Decimal(0), Decimal("0.30") - first_result.cost)
            # A failed forced-tool call has unknown usage even when the original
            # answer is substantive. Always leave room to finish the content.
            content_reserve = min(Decimal("0.08"), remaining_cost / 2)
            recovery_allowance = remaining_cost - content_reserve
            model.delivery_reserve = content_reserve
            if remaining_seconds > 0 and recovery_allowance > 0:
                # The same model retains its spending ledger. Recovery only needs
                # file tools; reopening search would reset its per-request quotas.
                model.spent = max(model.spent, first_result.cost)
                model.tools = [
                    tool
                    for tool in model.tools
                    if isinstance(function := tool.get("function"), dict)
                    and function.get("name") in {"terminal_command", "publish_document"}
                ]
                model.required_file_tool = "terminal_command"
                model.required_file_formats = formats
                recovery_history: list[Message] = [
                    {"role": m.role, "content": m.text} for m in request.messages
                ]
                recovery_history.append(
                    {"role": "assistant", "content": first_result.text}
                )
                try:
                    result = await run(
                        Query(
                            question=iteration or request.messages[-1].text,
                            lang=request.lang,
                            prose_lang=request.prose_lang,
                        ),
                        model,
                        tools,
                        Path("prompts/chat.txt").read_text()
                        + Path("prompts/chat-agent.txt").read_text()
                        + language_instruction(request.prose_lang)
                        + (instruction() if links else "")
                        + file_context
                        + "\n\nFile-delivery recovery: the prior answer produced no file. "
                        "Use terminal_command to create the requested files from the "
                        "actual answer content, then publish_document for each file. "
                        "Follow the original request and its language for all titles "
                        "and content. For a presentation with PDF, render the same "
                        "substantive slide content into both formats, one PDF page per "
                        "slide. Never add placeholder or filler pages. Do not claim a "
                        "file or link exists unless publish_document succeeded. If "
                        "creation fails, return the actual requested content inline "
                        "and explain the failure in the request language.",
                        Limits(
                            profile=request.model,
                            tokens=None,
                            tool_calls=max(
                                0,
                                10 - len(attachment_sources) - first_result.tool_calls,
                            ),
                            cost=remaining_cost,
                            has_attachments=request.has_attachments,
                            produces_files=True,
                            wall_clock=max(0, remaining_seconds - 90),
                        ),
                        include_intermediate=request.atlas_ui,
                        history=recovery_history,
                        skill_messages=tools.skills.instructions
                        if tools.skills is not None
                        else None,
                    )
                    result.cost += first_result.cost
                    result.tool_calls += first_result.tool_calls
                except GatewayError as exc:
                    # Unknown failed usage retains the full remaining reservation.
                    # A delivery failure must not discard an already usable answer.
                    item.erreurs.append(exc.code)
                    item.provider_retries += exc.retries
                    result = first_result
                    result.cost += recovery_allowance
                    file_recovery_failed = True
                finally:
                    model.delivery_reserve = Decimal(0)
            # Preserve substantive first-pass evidence, never an invented recovery URL.
            # A stopped pass carries a diagnostic, not content.
            if not tools.terminal.files:
                if first_result.state == "done" and first_result.text.strip():
                    result.text, result.state = first_result.text.strip(), "done"
                else:
                    result.text = result.text.strip()
        if deliver_files and tools.terminal is not None:
            from services.orchestrator.file_content import render_command
            from services.orchestrator.file_results import inline_fallback

            terminal_already_failed = "terminal_operation_failed" in item.erreurs
            missing = tuple(
                ext
                for ext in formats
                if not any(url.endswith("." + ext) for url in tools.terminal.files)
            )
            if (
                missing
                and not file_recovery_failed
                and "terminal_operation_failed" not in item.erreurs
            ):
                ready = not file_recovery_failed and presentable(result.text, missing)
                if not ready and result.tool_calls + 1 + len(missing) <= 10:
                    from services.orchestrator.content_recovery import recover_content

                    ready = await recover_content(
                        model, request, item, result, missing, started
                    )
                    if ready:
                        # Recovered content replaces a stop diagnostic.
                        result.state = "done"
                command, paths = (
                    render_command(result.text, missing) if ready else ("", [])
                )
                remaining = request.timeout_seconds - (time.monotonic() - started)
                if (
                    command
                    and result.tool_calls + 1 + len(paths) <= 10
                    and remaining > 0
                    and result.cost < Decimal("0.30")
                ):
                    # Deterministic rendering uses no inference; all I/O stays in
                    # the original deadline and remaining tool allowance.
                    result.tool_calls += 1
                    render_execute = (
                        tools.execute_trusted_render
                        if os.environ.get("ATLAS_STABLE") == "1"
                        else tools.execute
                    )
                    try:
                        async with asyncio.timeout(remaining):
                            created = await render_execute(
                                Call(
                                    "content-render",
                                    "terminal_command",
                                    json.dumps({"command": command}),
                                ),
                                min(30, remaining),
                            )
                            data = created.get("data")
                            if (
                                isinstance(data, dict)
                                and data.get("exit_code") == 0
                                and not created.get("error")
                            ):
                                for path in paths:
                                    remaining = request.timeout_seconds - (
                                        time.monotonic() - started
                                    )
                                    if remaining <= 0:
                                        break
                                    result.tool_calls += 1
                                    await render_execute(
                                        Call(
                                            "content-publish-" + path,
                                            "publish_document",
                                            json.dumps(
                                                {"path": path, "strategy": "created"}
                                            ),
                                        ),
                                        min(30, remaining),
                                    )
                    except TimeoutError:
                        item.erreurs.append("file_render_timeout")
            missing = tuple(
                ext
                for ext in formats
                if not any(url.endswith("." + ext) for url in tools.terminal.files)
            )
            if not tools.terminal.files or missing:
                if result.state != "done":
                    # A stopped run (output or cost budget, provider failure) has
                    # no content above: keep its honest failure and status.
                    pass
                elif not terminal_already_failed or file_recovery_attempted:
                    result.text = inline_fallback(
                        result.text,
                        request.lang,
                        preserve_code=preserve_code,
                        partial=bool(tools.terminal.files),
                    )
                    result.state = "done"
                    result.reason = "file_not_produced"
                else:
                    result.text = answer_content(
                        result.text, preserve_code=preserve_code
                    )
            else:
                result.text = answer_content(result.text, preserve_code=preserve_code)
        elif deliver_files and result.state == "done":
            from services.orchestrator.file_results import inline_fallback

            result.text = inline_fallback(
                result.text, request.lang, preserve_code=preserve_code
            )
        from services.orchestrator.source_truth import urls as urls_in_text
        from services.orchestrator.shopping_links import render_shopping

        if links and (
            shopping := render_shopping(tools.shopping_searches, request.lang)
        ):
            result.text = shopping
        if links and urls_in_text(result.text):
            from services.orchestrator.shopping_prices import verified_prices

            result.text = verified_prices(
                result.text, tools.fetched_pages, request.lang, tools.search_snippets
            )
        item.reponse, item.state, item.cout_eur = (
            result.text + ("\n\n" + notice if notice else ""),
            result.state,
            float(result.cost + image_cost),
        )
        if notice and model.sink:
            await model.sink({"delta": {"content": "\n\n" + notice}})
        if tools.terminal is not None:
            item.terminal_commands = tools.terminal.commands
            item.terminal_failed_commands = tools.terminal.failed_commands
            item.files = tools.terminal.files
            if item.files:
                from services.orchestrator.file_results import render_files

                if result.state != "done":
                    # Published files remain usable even if closing prose fails.
                    item.erreurs.append(result.reason or "closing_answer_failed")
                    item.state = "done"
                item.reponse = render_files(
                    item.reponse, item.files, tools.terminal.strategies, request.lang
                )
        if result.state == "done" and appearance:
            if images_enabled():
                await finish_appearance(request, item, result, started)
            else:
                await finish_mechanical_drawing(
                    request,
                    item,
                    result,
                    fallback_only=os.environ.get("ATLAS_STABLE") != "1",
                )
                logging.getLogger(__name__).warning(
                    "mechanical_result model_calls=%d retries=%d cost_eur=%s",
                    len(model.observations),
                    model.provider_retries,
                    result.cost,
                )
            return
        if result.state == "done" and tools.image_prompt is not None:
            await finish(request, item, result, tools.image_prompt, started)
            return
        if result.reason:
            item.erreurs.append(result.reason)
        if delivery_buffer is not None:
            # SSE resolves raw citation identifiers itself, after provenance filtering.
            await delivery_buffer.finish(item.reponse)
        if result.state == "done" and not followup:
            await render_citations(item)
        if tools.source_truncated:
            labels = json.loads(Path("prompts/activity-labels.json").read_text())
            item.reponse += (
                "\n\n" + labels.get(request.lang, labels["en"])["source_truncated"]
            )
        if (
            (technical_diagram or image_discussion)
            and not links
            and not appearance
            and (sink := sink_context.get())
        ):
            await sink({"delta": {"content": item.reponse}})
    except GatewayError as exc:
        if links and exc.code in {
            "search_unavailable",
            "quota_exceeded",
            "search_rate_limited",
            "search_no_results",
            "daily_search_limit",
        }:
            from services.orchestrator.link_requests import (
                fallback_terms,
                reason,
                unavailable,
            )

            item.erreurs.append(exc.code)
            item.reponse = unavailable(
                fallback_terms(
                    request.user_text, tuple(m.text for m in request.messages[:-1])
                ),
                request.lang,
            )
            cause = exc.detail.strip() or reason(exc.code, request.lang)
            if cause:
                item.reponse += "\n\n" + cause
            item.state = "done"
            item.cout_eur = float(model.spent)
            return
        if tools.terminal is None or not tools.terminal.files:
            raise
        from services.orchestrator.file_results import render_files

        item.files = list(tools.terminal.files)
        item.file_strategies = list(tools.terminal.strategies)
        item.terminal_commands = tools.terminal.commands
        item.terminal_failed_commands = tools.terminal.failed_commands
        item.erreurs.append(exc.code)
        item.provider_retries += exc.retries
        item.reponse = render_files(
            exc.detail, item.files, item.file_strategies, request.lang
        )
        item.state = "done"
    finally:
        item.provider_retries += model.provider_retries
        item.tokens = {"in": model.input_tokens, "out": model.output_tokens}
        item.provider_model = model.provider_model
        item.reasoning = model.reasoning
        item.trace_tokens, item.answer_tokens = model.trace_tokens, model.answer_tokens
        item.token_split_estimated = model.token_split_estimated
        item.reasoning_retried = model.reasoning_retried
        item.reasoning_effort = model.reasoning_used
        item.latence_ms["generation"] = model.generation_ms
        if model.observations and item.task_type != "imagegen":
            item.modele_utilise = model.observations[-1].provider
            item.route_decision = model.observations[0].route
