"""Authenticated project Chat/Work adapter; retrieval owns every durable result."""

from __future__ import annotations

import asyncio
import json
import os
import re
from decimal import Decimal
from contextvars import ContextVar
from pathlib import Path
from time import monotonic
from typing import Any
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import aiohttp
from fastapi import Request

from packages import context_usage
from packages.language import conversation_language, language_instruction
from packages.ports import atlas_ports
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.code_api import ExecutionBroker
from services.orchestrator.code_ui import project_mode
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call, Limits, Message, Query, Reservation, run
from services.orchestrator.model import GatewayModel
from services.orchestrator.project_ui import (
    browser_origin,
    broker_identity,
    bounded_json,
    verified_subject,
)
from services.orchestrator.stream_client import sink_context
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_work_contract import validate


class TranscriptBroker(ExecutionBroker):
    private_prefix = "/internal/atlas/transcript/"
    success_statuses = (200, 201)


def answer_content(text: str) -> str:
    """Unwrap historical answers and omit internal record references."""
    import re

    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if (
        isinstance(value, dict)
        and set(value) == {"answer", "facts"}
        and isinstance(value["answer"], str)
        and value["answer"].strip()
        and isinstance(value["facts"], list)
    ):
        text = value["answer"]
    text = re.sub(r"\bchunk\s+`?[a-f0-9]{32,64}`?\s*:\s*", "", text, flags=re.I)
    identifier = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
    lines = []
    for line in text.splitlines():
        if re.search(
            r"^\s*(?:[#*_`]+\s*)?(?:La\s+)?(?:validation|vérification|verification)\b"
            r".*\b(?:package_readable|requested_content|source_coverage)\b",
            line,
            re.I,
        ):
            continue
        if re.search(r"mémoire|memoire|memory|record|identifiant|memoria", line, re.I):
            line = re.sub(r"\([^()]*" + identifier + r"[^()]*\)", "", line, flags=re.I)
            line = re.sub(
                r"(?:ID|identifiant|record)?\s*" + identifier, "", line, flags=re.I
            )
            line = re.sub(r"[ \t]+([.,;])", r"\1", line)
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def recurring_guidance(question: str, language: str) -> str | None:
    """Return turn-local capability guidance without changing the shared prompt."""
    recurring = re.search(
        r"\b(?:every|each)\s+(?:day|week|month|year)|\b(?:weekly|daily|monthly|schedule[ds]?|recurr(?:ing|ent))\b"
        r"|\b(?:chaque|tous les)\s+(?:jour|semaine|mois|an)|\b(?:hebdomadaire|quotidien|planifi)"
        r"|\b(?:cada|todos los)\s+(?:día|semana|mes)|\b(?:semanal|programad)"
        r"|\b(?:jede[nsr]?|wöchentlich|täglich|monatlich|geplant)\b"
        r"|\b(?:ogni|settimanale|giornaliero|pianificat)\b",
        question,
        re.IGNORECASE,
    )
    if not recurring:
        return None
    notices = {
        "fr": "Faites la partie demandée maintenant. Dites clairement que les mises à jour périodiques automatiques ne sont pas encore disponibles et expliquez comment relancer cette demande manuellement.",
        "de": "Erledige den einmaligen Teil jetzt. Sage klar, dass automatische regelmäßige Aktualisierungen noch nicht verfügbar sind, und erkläre, wie die Anfrage manuell erneut gestartet wird.",
        "es": "Haz ahora la parte puntual solicitada. Indica claramente que las actualizaciones periódicas automáticas aún no están disponibles y explica cómo volver a ejecutar la solicitud manualmente.",
        "it": "Completa ora la parte richiesta una tantum. Spiega chiaramente che gli aggiornamenti periodici automatici non sono ancora disponibili e come rieseguire manualmente la richiesta.",
        "en": "Complete the requested one-off work now. State plainly that automatic periodic updates are not available yet and explain how to run this request again manually.",
    }
    return notices.get(language, notices["en"])


async def saved_memory_reply(
    broker: Any, turn: dict[str, Any], question: str, language: str
) -> str | None:
    from packages.project_memory import explicit

    text = explicit(question)
    if text is None:
        return None
    status, response = await broker.public("GET", "memory")
    if status != 200:
        return None
    saved = next(
        (
            entry
            for entry in response["items"]
            if entry.get("source_turn_id") == turn["id"]
            and entry.get("text") == text
            and entry.get("status") in {"active", "proposed"}
        ),
        None,
    )
    if saved is None:
        return None
    labels = {
        "fr": (
            "C’est enregistré dans la mémoire du projet : ",
            "C’est proposé dans la mémoire du projet, en attente de confirmation : ",
        ),
        "en": (
            "Saved in project memory: ",
            "Proposed in project memory, awaiting confirmation: ",
        ),
        "de": (
            "Im Projektgedächtnis gespeichert: ",
            "Im Projektgedächtnis zur Bestätigung vorgeschlagen: ",
        ),
        "es": (
            "Guardado en la memoria del proyecto: ",
            "Propuesto en la memoria del proyecto, pendiente de confirmación: ",
        ),
        "it": (
            "Salvato nella memoria del progetto: ",
            "Proposto nella memoria del progetto, in attesa di conferma: ",
        ),
    }
    return labels.get(language, labels["en"])[saved["status"] == "proposed"] + text


async def authenticate_chat(
    request: Request, raw: Any
) -> tuple[str | None, str | None]:
    if not isinstance(raw, dict):
        return None, None
    legacy = project_mode() and bool(raw.get("code_mode") or raw.get("code_session_id"))
    scoped = raw.get("org_id") is not None
    selectors = any(
        raw.get(k)
        for k in (
            "project_id",
            "conversation_id",
            "file_version_ids",
            "work_session_id",
        )
    )
    requires_scope = (
        project_mode()
        or raw.get("project_action") == "work"
        or bool(raw.get("file_version_ids"))
    )
    if not scoped and not (requires_scope and selectors) and not legacy:
        return None, None
    if legacy:
        if scoped or selectors or raw.get("project_action", "chat") != "chat":
            raise ProjectError(409, "selector_conflict")
        browser_origin(request)
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
        ) as client:
            subject = await verified_subject(request, client)
            selection = await legacy_selection(
                client, subject, raw.get("code_session_id")
            )
        raw.pop("code_mode", None)
        raw.pop("code_session_id", None)
        raw.update(selection, project_action="work")
        scoped = True
    if raw.get("code_mode") or raw.get("code_session_id") or not scoped:
        raise ProjectError(409, "selector_conflict")
    if (
        raw.get("project_action", "chat") != "work"
        and raw.get("work_session_id") is not None
    ):
        raise ProjectError(409, "selector_conflict")
    browser_origin(request)
    # Location is validated by ChatRequest and never enters the durable broker.
    validate("ProjectChatRequest", {k: v for k, v in raw.items() if k != "location"})
    key = request.headers.get("idempotency-key", "")
    if not key or len(key) > 200:
        raise ProjectError(422, "invalid_request", "Idempotency-Key is required")
    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
    ) as client:
        subject = await verified_subject(request, client)
        await broker_identity(client, subject, raw["org_id"], raw["project_id"])
    return subject, key


async def legacy_selection(
    client: aiohttp.ClientSession, subject: str, session_id: Any
) -> dict[str, str]:
    """Resolve only an operator-mapped alias across the subject's current grants."""
    from services.orchestrator.chat_pipeline import retrieval_url
    from urllib.parse import urlencode

    try:
        alias = str(UUID(session_id))
    except (ValueError, TypeError, AttributeError):
        raise ProjectError(422, "invalid_request") from None
    memberships = await broker_identity(client, subject, None, None)
    matches = []
    for membership in memberships["memberships"]:
        org = membership["org_id"]
        cursor = None
        while True:
            identity = await broker_identity(client, subject, org, None)
            query = "?" + urlencode({"cursor": cursor}) if cursor else ""
            async with client.get(
                retrieval_url() + f"/api/atlas/orgs/{org}/projects" + query,
                headers={"X-Atlas-Project-Identity": identity["identity"]},
                allow_redirects=False,
            ) as reply:
                projects = await bounded_json(reply)
                if reply.status != 200:
                    raise ProjectError(
                        reply.status,
                        str(projects.get("error", "dependency_unavailable")),
                    )
            for project in projects["items"]:
                broker = ExecutionBroker(subject, org, project["id"])
                status, session = await broker.public("GET", "work-sessions/" + alias)
                if status == 404:
                    continue
                if status != 200:
                    raise ProjectError(
                        status, str(session.get("error", "dependency_unavailable"))
                    )
                if session["legacy_session_id"] == alias:
                    matches.append(
                        dict(
                            org_id=org,
                            project_id=project["id"],
                            conversation_id=session["conversation_id"],
                            work_session_id=session["id"],
                        )
                    )
            cursor = projects["next_cursor"]
            if cursor is None:
                break
    if len(matches) != 1:
        raise ProjectError(
            409 if matches else 404, "selector_conflict" if matches else "not_found"
        )
    return matches[0]


class ContextTools:
    def __init__(
        self,
        context: dict[str, Any],
        memory: Any = None,
        check: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.context = context
        self.memory = memory
        self.connectors: Any = None
        self.skills: Any = None
        self.check = check

    def estimate(self, call: Call) -> Reservation:
        return Reservation(0, Decimal(0))

    async def execute(self, call: Call, timeout: float) -> Message:
        from services.orchestrator.api_key_edge import key_call

        if key_call.get() and (
            call.name == "mcp_call"
            or call.name.startswith("mcp_")
            or call.name == "switch_connector_group"
        ):
            return {"error": "insufficient_scope", "missing_scope": "connectors"}
        if self.connectors is not None and (
            call.name.startswith("mcp_") or call.name == "switch_connector_group"
        ):
            return dict(await self.connectors.execute(call, timeout))
        if self.check is not None:
            await self.check()
        if self.skills is not None:
            if not await self.skills.authorized():
                return {"error": "skill_authority_revoked"}
            if call.name == "load_skill":
                return dict(await self.skills.execute(call, timeout))
            if self.skills.blocks(call.name):
                return {"error": "skill_action_not_permitted"}
        if call.name == "project_memory" and self.memory is not None:
            return dict(await self.memory.execute(call))
        if call.name != "rag_search":
            return {"error": "tool_unavailable"}
        return dict(trust="untrusted", data=dict(passages=self.context["passages"]))


class DispatchEvents:
    def __init__(self, broker: TranscriptBroker, state: dict[str, Any]) -> None:
        self.broker, self.state = broker, state
        self.call_id = ""
        self.calls = 0
        self.state_update = False
        self.unknown = False
        self.payload_bytes = 0
        self.session_id: str | None = None
        self.generation: int | None = None
        self.context: dict[str, Any] | None = None
        from services.orchestrator.session_context import SessionContext

        self.session_context: SessionContext | None = None

    async def publish(self, phase: str, value: Message) -> None:
        payload: dict[str, Any] = dict(value)
        turn, context = self.state["turn"], self.state["context"]
        if phase == "intent":
            self.call_id = str(uuid4())
            self.calls += 1
            self.payload_bytes = len(json.dumps(value).encode())
            self.context = context_usage.measure(
                payload["messages"], payload["tools"], payload.get("profile")
            )
            if self.session_context and self.session_context.last:
                from services.orchestrator.session_context import calibration

                family, digest, estimate = self.session_context.last
                self.context.update(
                    calibrated_tokens=calibration.count(family, digest, estimate),
                    calibration_factor=calibration.factors.get(family, 4 / 3),
                    context_status=self.session_context.status,
                )
        elif phase == "acknowledged" and self.context is not None:
            self.context["provider_input_tokens"] = payload.get("usage", {}).get(
                "prompt_tokens"
            )
        counts = dict(
            source_bytes=0,
            selected_utf8_bytes=sum(
                len(p["text"].encode()) for p in context["passages"]
            ),
            image_bytes=0,
            serialized_payload_bytes=self.payload_bytes,
            response_bytes=len(json.dumps(value).encode())
            if phase == "acknowledged"
            else 0,
        )
        self.unknown = phase != "acknowledged"
        transmission = {
            "intent": "not_sent",
            "acknowledged": "acknowledged",
            "unknown": "possibly_sent",
        }[phase]
        model = dict(
            call_id=self.call_id,
            attempt=1,
            provider_ref="configured-generation",
            purpose="session_state" if self.state_update else "generation",
            sources=context["context_manifest"]["sources"],
            instructions_version=context["instructions_version"],
            memory_revision=context["memory_revision"],
            byte_counts=counts,
            input_tokens=payload.get("usage", {}).get("prompt_tokens", 0),
            output_tokens=payload.get("usage", {}).get("completion_tokens", 0),
            tokens_estimated=True,
            transmission=transmission,
            **({"context": self.context} if self.context is not None else {}),
        )
        event = dict(
            org_id=self.broker.org,
            project_id=self.broker.project,
            seq=1,
            event_id=str(uuid4()),
            timestamp=turn["created_at"],
            actor=dict(
                kind="member", member_id=turn["created_by_member_id"], service=None
            ),
            conversation_id=turn["conversation_id"],
            session_id=self.session_id,
            request_id=turn["request_id"],
            kind="model_dispatch_intent" if phase == "intent" else "model_" + phase,
            resource_refs=context["context_manifest"]["sources"],
            source=dict(kind="server", ref=None),
            destination=dict(kind="model", ref="configured-generation"),
            byte_counts=counts,
            outcome="intent"
            if phase == "intent"
            else "succeeded"
            if phase == "acknowledged"
            else "unknown",
            approval_id=None,
            policy_revision=context["context_manifest"]["policy_revision"],
            previous_digest=None,
            digest="0" * 64,
            model_dispatch=model,
            audit_record=None,
        )
        result = await self.broker.call(
            "state-events"
            if self.state_update
            else "work-events"
            if self.session_id
            else "events",
            dict(
                conversation_id=turn["conversation_id"],
                request_id=turn["request_id"],
                events=[event],
                **(
                    {"generation": self.generation}
                    if self.generation is not None and not self.state_update
                    else {}
                ),
            ),
        )
        sink = sink_context.get()
        if sink:
            for persisted in result["events"]:
                await sink({"project": persisted})


def answer_refs(item: Interaction) -> list[dict[str, Any]]:
    from services.orchestrator.location import LEDGER_LABEL

    if not item.reponse.strip():
        return []
    text = item.reponse
    marker = item.location_used == LEDGER_LABEL
    if marker:
        text = text.removesuffix(LEDGER_LABEL).rstrip()
    refs = [dict(kind="answer", text=text, file_version_id=None)] if text else []
    if marker:
        # A separate visible answer fragment uses the existing closed transcript
        # contract. Only this exact server-owned marker becomes a ledger event.
        refs.append(dict(kind="answer", text=LEDGER_LABEL, file_version_id=None))
    return refs


work_dispatch: ContextVar[DispatchEvents | None] = ContextVar(
    "work_dispatch", default=None
)


async def process_scoped_project(request: ChatRequest, item: Interaction) -> None:
    if (
        not request.org_id
        or not request.project_id
        or not request._project_subject
        or not request._transcript_key
    ):
        raise ProjectError(401, "unauthenticated")
    broker = TranscriptBroker(
        request._project_subject, request.org_id, request.project_id
    )
    body = dict(
        model=request.model,
        org_id=request.org_id,
        project_id=request.project_id,
        conversation_id=request.conversation_id,
        project_action=request.project_action,
        messages=[dict(role="user", content=request.messages[-1].text)],
        stream=True,
        file_version_ids=request.file_version_ids,
    )
    if request.work_session_id:
        body["work_session_id"] = request.work_session_id
    state = await broker.call(
        "begin",
        dict(
            request_id=str(UUID(item.request_id.removeprefix("chatcmpl-"))),
            request=body,
        ),
        request._transcript_key,
    )
    turn, context = state["turn"], state["context"]
    if request._connector_scope:
        request._connector_scope["conversation"] = turn["conversation_id"]
    item.context_manifest = context["context_manifest"]
    item.request_id = "chatcmpl-" + turn["request_id"]
    sink = sink_context.get()
    if sink:
        for event in state["events"]:
            await sink({"project": event})
    if state["replay"]:
        if context := turn["usage"].get("context"):
            item.context_samples.append(context)
        item.reponse = "\n".join(
            r["text"]
            for r in turn["content_refs"]
            if r["kind"] == "answer" and r["text"]
        )
        item.reponse = answer_content(item.reponse)
        item.state = "done"
        return
    from services.orchestrator.session_context import assemble, SessionContext

    history, dropped = assemble(context, request.messages[-1])
    session_context = SessionContext(broker, state, dropped)
    scoped = request.model_copy(
        update=dict(
            messages=history, lang=conversation_language(history, request.ui_locale)
        )
    )
    started = monotonic()
    terminal = "failed"
    error: dict[str, Any] | None = None
    model: GatewayModel | None = None
    events = DispatchEvents(broker, state)
    events.session_context = session_context
    tool_calls = 0

    async def check() -> None:
        await broker.call(
            "check",
            dict(
                conversation_id=turn["conversation_id"], request_id=turn["request_id"]
            ),
        )

    try:
        from services.orchestrator.location import location_dependent

        memory_reply = await saved_memory_reply(
            broker, turn, request.user_text, scoped.lang
        )
        if memory_reply is not None and request.project_action != "work":
            item.reponse, item.state = memory_reply, "done"
        elif request.project_action != "work" and location_dependent(request.user_text):
            from services.orchestrator.nearby_chat import process_nearby

            tool_calls = await process_nearby(
                scoped,
                item,
                cost_limit=Decimal(str(turn["usage"]["reserved_eur"])),
                check=check,
            )
        elif request.project_action == "work":
            from services.orchestrator.code_mode import process_work

            work_options: dict[str, Any] = (
                {"saved_reply": memory_reply} if memory_reply is not None else {}
            )
            dispatch_token = work_dispatch.set(events)
            try:
                await process_work(
                    scoped,
                    item,
                    subject=request._project_subject,
                    org_id=request.org_id,
                    project_id=request.project_id,
                    session_id=str(request.work_session_id),
                    **work_options,
                )
                if item.state == "failed" and scoped.lang == "fr":
                    from packages.configuration import temporarily_unavailable

                    item.reponse = temporarily_unavailable(scoped.lang)
            finally:
                work_dispatch.reset(dispatch_token)
        else:
            model = await GatewayModel.connect(
                os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway")),
                local_enabled=False,
            )
            model.configure_quality(request.model, request.max_tokens)
            model.cost_ceiling = Decimal(str(turn["usage"]["reserved_eur"]))
            model.dispatch_hook = events.publish
            model.session_context = session_context
            model.tools = [
                t
                for t in model.tools
                if isinstance(function := t.get("function"), dict)
                and function.get("name") == "rag_search"
            ]
            # Buffer until citations and content have been checked, then emit the
            # canonical answer through the normal content delta path.
            model.sink = None
            from packages.tool_results import declaration
            from services.orchestrator.result_references import ResultReferences

            model.tools.append(declaration())
            from packages.project_memory_tool import declaration as memory_declaration
            from services.orchestrator.project_memory_tools import MemoryTools

            model.tools.append(memory_declaration())
            references = ResultReferences(
                broker, turn["conversation_id"], turn["request_id"]
            )
            from services.orchestrator.connectors import prepare as prepare_connectors

            context_tools = ContextTools(
                context, MemoryTools(broker, state), check=check
            )
            if not request._api_key_request:
                context_tools.connectors = await prepare_connectors(request)
            if context_tools.connectors is not None:
                context_tools.connectors.bind(model)
            from services.orchestrator.skill_tools import SkillTools

            if os.environ.get("ATLAS_SKILLS_ENABLED") == "1":
                context_tools.skills = await SkillTools.connect(
                    broker, telemetry=item.skills_loaded
                )
                if context_tools.skills.entries:
                    model.tools.append(context_tools.skills.declaration())
            guidance = recurring_guidance(request.user_text, scoped.lang)
            skill_messages = (
                context_tools.skills.instructions
                if context_tools.skills is not None
                else []
            ) + ([{"role": "system", "content": guidance}] if guidance else [])
            result = await run(
                Query(question=request.user_text, lang=scoped.lang),
                model,
                context_tools,
                Path("prompts/chat.txt").read_text()
                + language_instruction(scoped.lang),
                Limits(
                    profile=request.model,
                    tokens=262144,
                    cost=model.cost_ceiling,
                    wall_clock=max(
                        0, request.timeout_seconds - (monotonic() - started)
                    ),
                ),
                history=[dict(role=m.role, content=m.text) for m in history[:-1]],
                references=references,
                skill_messages=skill_messages or None,
            )
            tool_calls = result.tool_calls
            item.state, item.cout_eur = result.state, float(result.cost)
            if result.reason:
                item.erreurs.append(result.reason)
            from services.orchestrator.project_citations import render

            item.reponse = render(
                answer_content(result.text),
                context["passages"],
                request.org_id,
                request.project_id,
            )
            coverage = context["context_manifest"]["coverage"]
            if not coverage["complete"]:
                item.reponse += "\n\nSource coverage: " + "; ".join(
                    coverage["warnings"][:8]
                )
            item.tokens = {"in": model.input_tokens, "out": model.output_tokens}
        item.reponse = answer_content(item.reponse)
        if session_context.warning:
            item.reponse += "\n\n" + session_context.warning
        terminal = "completed" if item.state == "done" else "failed"
        if terminal == "failed":
            from packages.configuration import temporarily_unavailable

            if scoped.lang == "fr":
                item.reponse = temporarily_unavailable(scoped.lang)
                message = item.reponse
            else:
                message = "; ".join(item.erreurs) or item.state
            error = ProjectError(422, "invalid_request", message).body
    except asyncio.CancelledError:
        terminal = "cancelled"
        error = ProjectError(422, "invalid_request", "Turn cancelled by request").body
        raise
    except ProjectError as exc:
        error = exc.body
        raise
    except Exception as exc:
        from packages.configuration import temporarily_unavailable
        import logging

        logging.getLogger(__name__).warning(
            "project_turn_failed kind=%s", type(exc).__name__
        )
        item.state = "failed"
        item.reponse = temporarily_unavailable(scoped.lang)
        error = ProjectError(503, "dependency_unavailable", item.reponse).body
    finally:
        unknown = terminal != "completed" or events.unknown
        usage = dict(
            tool_calls=tool_calls,
            model_calls=events.calls,
            elapsed_seconds=monotonic() - started,
            cost_eur=item.cout_eur,
            reserved_eur=0,
            unknown_eur=turn["usage"]["reserved_eur"] if unknown else 0,
            stored_bytes=0,
            input_tokens=item.tokens.get("in", 0),
            output_tokens=item.tokens.get("out", 0),
            tokens_estimated=False,
            **({"context": events.context} if events.context is not None else {}),
        )
        import anyio

        with anyio.CancelScope(shield=True):
            try:
                await broker.call(
                    "finish",
                    dict(
                        request_id=turn["request_id"],
                        conversation_id=turn["conversation_id"],
                        state=terminal,
                        content_refs=answer_refs(item),
                        usage=usage,
                        error=error,
                    ),
                    turn["request_id"] + ":finish",
                )
            except ProjectError as exc:
                if exc.status != 409:
                    raise
                stored = await broker.call(
                    "begin",
                    dict(request_id=turn["request_id"], request=body),
                    request._transcript_key,
                )
                if stored["turn"]["state"] != "cancelled":
                    raise
        # Begin replay is the contracted way to fetch the stored terminal event.
        completed = await broker.call(
            "begin",
            dict(request_id=turn["request_id"], request=body),
            request._transcript_key,
        )
        if sink:
            for event in completed["events"]:
                if (
                    event.get("type") in {"recap", "error"}
                    and event.get("turn_id") == turn["id"]
                ):
                    await sink({"project": event})
        if completed["turn"]["state"] == "completed":
            from services.orchestrator.session_updates import schedule

            schedule(broker, completed)
