"""Chat uses the bounded harness and resolves citations through retrieval HTTP."""

from packages.limits import LimitError
from packages.validation import describe_validation
from pydantic import ValidationError

import asyncio
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
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call, Limits, Message, Query, run
from services.orchestrator.model import GatewayError, GatewayModel
from services.orchestrator.tools import Rag, Runtime, search_providers
from services.orchestrator.stream_client import Sink, sink_context
from services.orchestrator.vision import process_vision
from services.orchestrator.search_policy import required_research, corpus_allowed
from services.orchestrator.followup import (
    is_followup,
    image_iteration,
    image_dialogue,
    image_explanation,
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
from services.orchestrator.appearance import (
    is_appearance_request,
    is_simple_drawing_request,
    legend_instruction,
    mechanical_svg_instruction,
    simple_drawing_language,
    simple_drawing_instruction,
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
    """Disclose a later search failure without denying already cited evidence."""
    notices = {
        "en": "Part of the web search failed ({reason}); this answer relies only on the sources retrieved before it.",
        "fr": "Une partie de la recherche web a échoué ({reason}) ; cette réponse repose uniquement sur les sources récupérées avant cet échec.",
        "de": "Ein Teil der Websuche ist fehlgeschlagen ({reason}); diese Antwort stützt sich nur auf die zuvor abgerufenen Quellen.",
        "es": "Parte de la búsqueda web falló ({reason}); esta respuesta se basa únicamente en las fuentes recuperadas antes del fallo.",
        "it": "Una parte della ricerca web non è riuscita ({reason}); questa risposta si basa solo sulle fonti recuperate prima dell'errore.",
    }
    return notices.get(language, notices["en"]).format(reason=outcome)


class Source(BaseModel):
    chunk_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    doc_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=32000)


class Passage(Source):
    score: float = Field(allow_inf_nan=False)


class Passages(BaseModel):
    passages: list[Passage] = Field(max_length=8)


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


async def render_citations(item: Interaction) -> None:
    uncited = re.sub(r"\[[^\]]*\]\([^)]*/sources/[a-f0-9]{64}\)", "", item.reponse)
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
        return await self._execute(call, timeout, trusted_render=False)

    async def execute_trusted_render(self, call: Call, timeout: float) -> Message:
        return await self._execute(call, timeout, trusted_render=True)

    async def _execute(
        self, call: Call, timeout: float, *, trusted_render: bool
    ) -> Message:
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
                period = "this month" if "monthly" in exc.category else "today"
                return {
                    "error": "daily_search_limit",
                    "message": f"search limit reached {period} ({exc.used} of {exc.limit})",
                }
            data = output.get("data")
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
            if call.name in {"web_search", "web_fetch"} and "error" not in output:
                self.item.retrieved_urls.extend(urls)
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
                and (
                    not self.item.retrieved_urls
                    or output.get("error") in {"quota_exceeded", "search_rate_limited"}
                )
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
    try:
        await _process_request(request, item)
    finally:
        context_usage.parts.reset(components)
        context_usage.samples.reset(capture)
    item.reponse = verified_images(
        item.reponse,
        {str(image["reference"]) for image in item.images if "reference" in image},
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
    if await dispatch_project(request, item):
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
    from services.orchestrator.title import is_title_task, title_response

    # Open WebUI sends its title task to the same OpenAI-compatible endpoint.
    # It is auxiliary UI work: keep it deterministic and outside the agent loop.
    if is_title_task(request.user_text):
        item.reponse = title_response(request.user_text, request.lang)
        item.state, item.task_type, item.cout_eur = "done", "title", 0.0
        return
    language = conversation_language(request.conversation_messages, request.ui_locale)
    if not request.continuation and is_appearance_request(
        request.user_text, history=request.messages[:-1]
    ):
        language = requested_language(request.user_text) or language
    request = request.model_copy(update={"lang": language})
    if (
        extended_enabled()
        and not request.has_attachments
        and extended_request(request.user_text)
    ):
        if category := text_limit(request.user_text):
            item.reponse = text_refusal(category, request.lang)
            item.state, item.task_type, item.cout_eur = "done", "text", 0.0
            if sink := sink_context.get():
                await sink({"delta": {"content": item.reponse}})
            return
        profiles = tuple(
            profile for profile in extended_profiles() if profile in PROFILES
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
    if item.state == "done" and (failed_searches or partial_searches):
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
    if item.state == "done" and any(
        d.get("hierarchical_synthesis") for d in item.documents
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
            page_request,
            unavailable,
            link_query,
        )
        from services.orchestrator.citation_cleanup import hollow

        if page_request(
            request.user_text, tuple(m.text for m in request.messages[:-1])
        ) and hollow(clean_answer(result_before_guard), item.reponse):
            item.reponse = unavailable(
                link_query(
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
        item.reponse += source_footer(sources, request.lang)


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
    model.configure_quality(request.model, request.max_tokens)
    model.local_enabled = os.environ.get("GPU_LOCAL", "0") == "1"
    tools = ChatTools(item, request.user_text, allow_image=False)
    result = await run(
        Query(question=request.messages[-1].text, lang=request.lang),
        model,
        tools,
        Path("prompts/chat.txt").read_text()
        + Path("prompts/chat-agent.txt").read_text()
        + Path("prompts/web-chat.txt").read_text()
        + language_instruction(request.lang)
        + guest_capability_instruction()
        + simple_drawing_instruction(),
        Limits(
            profile=request.model,
            tool_calls=0,
            tokens=None,
            cost=Decimal("0.10"),
            wall_clock=max(0, request.timeout_seconds - (time.monotonic() - started)),
        ),
        history=[
            {"role": message.role, "content": message.text}
            for message in request.messages[:-1]
        ],
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
            Decimal("0.10"),
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


async def _process(
    request: ChatRequest,
    item: Interaction,
    *,
    cost_limit: Decimal | None = None,
    wall_clock_limit: float | None = None,
) -> None:
    technical_diagram = not request.continuation and diagram.requested(
        request.user_text
    )
    explaining_image = image_explanation(request.user_text) and (
        image_topic(request.user_text) or len(image_dialogue(request.messages)) > 1
    )
    if (
        images_enabled()
        and not technical_diagram
        and not explaining_image
        and not request.continuation
        and not request.has_attachments
        and not any(message.images for message in request.messages)
        and (
            explicit_image_request(request.user_text)
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
                " ".join(dialogue),
                item,
                request.seed,
                language=request.lang,
                dialogue=dialogue,
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
        if visual_calls:
            await finish_visual(request, item, visual_calls)
            return
    drawing = (
        not request.continuation
        and not request.has_attachments
        and not images_enabled()
        and not explicit_image_request(request.user_text)
        and is_simple_drawing_request(request.user_text)
    )
    if drawing:
        request = request.model_copy(
            update={"lang": simple_drawing_language(request.user_text, request.lang)}
        )
        await _process_simple_drawing(request, item)
        return
    if not images_enabled() and explicit_image_request(request.user_text):
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
    discovery = (
        required_research(
            request.user_text,
            request.lang,
            device_timings="tavily" in search_providers(),
            history=tuple(m.text for m in request.messages[:-1]) if links else (),
        )
        if not request.has_attachments and (not followup or images_in_history)
        else ()
    )
    # The original question still drives policy classification, but a UI retry is
    # only completing already-started prose: it must not begin another route.
    if request.continuation or appearance:
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
    elif images_in_history and not request.has_attachments:
        # The profiled vision route accepts a complete conversation. This retains
        # validated history images (at most four) for text-only follow-ups.
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

    produces_files = produces_file(request.user_text)
    from packages.file_intent import edits_attachment

    # Editing an attached file means returning it: keep the file tools (J43).
    deliver_files = requests_file_delivery(request.user_text) or (
        bool(request.documents) and edits_attachment(request.user_text)
    )
    from services.orchestrator.file_content import requested_formats

    requested_file_formats = requested_formats(request.user_text)
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
    item.cout_eur = (
        0.30 if request.has_attachments or produces_files else 0.10
    )  # Reserved until ledger returns.
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
    tools.daily_lang = request.lang
    tools.search_language = search_language(
        request.conversation_messages, request.ui_locale
    )
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
            Query(question=iteration or request.messages[-1].text, lang=request.lang),
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
            + language_instruction(request.lang)
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
            ),
            Limits(
                profile=request.model,
                tool_calls=0 if appearance else max(0, 10 - len(attachment_sources)),
                tokens=None,
                cost=cost_limit
                if cost_limit is not None
                else Decimal(
                    "0.30" if request.has_attachments or produces_files else "0.10"
                ),
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
            history=attachment_history,
            retry_web=True,
            initial_calls=discovery,
            terminal_tools=frozenset({"generate_image"})
            if not followup
            else frozenset(),
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
                cost_limit
                if cost_limit is not None
                else Decimal(
                    "0.30" if request.has_attachments or produces_files else "0.10"
                ),
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

        formats = requested_formats(request.user_text)
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
                        ),
                        model,
                        tools,
                        Path("prompts/chat.txt").read_text()
                        + Path("prompts/chat-agent.txt").read_text()
                        + language_instruction(request.lang)
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
                        history=recovery_history,
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
            if not tools.terminal.files:
                result.text = first_result.text.strip() or result.text.strip()
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
                if not terminal_already_failed or file_recovery_attempted:
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
        elif deliver_files:
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
            from services.orchestrator.link_requests import unavailable, link_query

            item.erreurs.append(exc.code)
            item.reponse = unavailable(
                link_query(
                    request.user_text, tuple(m.text for m in request.messages[:-1])
                ),
                request.lang,
            )
            item.reponse += "\n\n" + exc.detail
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
        item.latence_ms["generation"] = model.generation_ms
        if model.observations and item.task_type != "imagegen":
            item.modele_utilise = model.observations[-1].provider
            item.route_decision = model.observations[0].route
