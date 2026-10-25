"""Authenticated project Chat/Work adapter; retrieval owns every durable result."""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

import aiohttp
from fastapi import Request

from packages.language import conversation_language, language_instruction
from packages.ports import atlas_ports
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.code_api import ExecutionBroker
from services.orchestrator.code_ui import project_mode
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call, Limits, Message, Query, Reservation, run
from services.orchestrator.memory import consolidated
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
    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context

    def estimate(self, call: Call) -> Reservation:
        return Reservation(0, Decimal(0))

    async def execute(self, call: Call, timeout: float) -> Message:
        if call.name != "rag_search":
            return {"error": "tool_unavailable"}
        return dict(trust="untrusted", data=dict(passages=self.context["passages"]))


class DispatchEvents:
    def __init__(self, broker: TranscriptBroker, state: dict[str, Any]) -> None:
        self.broker, self.state = broker, state
        self.call_id = ""
        self.calls = 0
        self.unknown = False
        self.payload_bytes = 0

    async def publish(self, phase: str, value: Message) -> None:
        turn, context = self.state["turn"], self.state["context"]
        if phase == "intent":
            self.call_id = str(uuid4())
            self.calls += 1
            self.payload_bytes = len(json.dumps(value).encode())
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
            purpose="generation",
            sources=context["context_manifest"]["sources"],
            instructions_version=context["instructions_version"],
            memory_revision=context["memory_revision"],
            byte_counts=counts,
            input_tokens=0,
            output_tokens=0,
            tokens_estimated=True,
            transmission=transmission,
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
            session_id=None,
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
            "events",
            dict(
                conversation_id=turn["conversation_id"],
                request_id=turn["request_id"],
                events=[event],
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
    item.request_id = "chatcmpl-" + turn["request_id"]
    sink = sink_context.get()
    if sink:
        for event in state["events"]:
            await sink({"project": event})
    if state["replay"]:
        item.reponse = "\n".join(
            r["text"]
            for r in turn["content_refs"]
            if r["kind"] == "answer" and r["text"]
        )
        item.state = "done"
        return
    history = [
        ChatMessage(
            role="user" if r["kind"] == "question" else "assistant", content=r["text"]
        )
        for t in context["history"]
        for r in t["content_refs"]
        if r["kind"] in {"question", "answer"} and r["text"]
    ]
    history.append(
        ChatMessage(
            role="user",
            content=json.dumps(
                dict(
                    project_instructions=context["instructions_text"],
                    memory=context["facts"],
                    passages=context["passages"],
                    coverage=context["context_manifest"]["coverage"],
                )
            ),
        )
    )
    history.append(request.messages[-1])
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
    tool_calls = 0
    try:
        from services.orchestrator.location import location_dependent

        if request.project_action != "work" and location_dependent(request.user_text):
            from services.orchestrator.nearby_chat import process_nearby

            tool_calls = await process_nearby(
                scoped,
                item,
                cost_limit=Decimal(str(turn["usage"]["reserved_eur"])),
            )
        elif request.project_action == "work":
            from services.orchestrator.code_mode import process_work

            await process_work(
                scoped,
                item,
                subject=request._project_subject,
                org_id=request.org_id,
                project_id=request.project_id,
                session_id=str(request.work_session_id),
            )
        else:
            model = await GatewayModel.connect(
                os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway")),
                local_enabled=False,
            )
            model.configure_quality(request.model, request.max_tokens)
            model.cost_ceiling = Decimal(str(turn["usage"]["reserved_eur"]))
            model.dispatch_hook = events.publish
            model.tools = [
                t
                for t in model.tools
                if isinstance(function := t.get("function"), dict)
                and function.get("name") == "rag_search"
            ]
            # Buffer until citations and content have been checked, then emit the
            # canonical answer through the normal content delta path.
            model.sink = None
            result = await run(
                Query(question=request.user_text, lang=scoped.lang),
                model,
                ContextTools(context),
                Path("prompts/project.txt").read_text()
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
            )
            tool_calls = result.tool_calls
            item.state, item.cout_eur = result.state, float(result.cost)
            if result.reason:
                item.erreurs.append(result.reason)
            answer, _ = consolidated(result.text, request.user_text)
            from services.orchestrator.project_citations import render

            item.reponse = render(
                answer, context["passages"], request.org_id, request.project_id
            )
            coverage = context["context_manifest"]["coverage"]
            if not coverage["complete"]:
                item.reponse += "\n\nSource coverage: " + "; ".join(
                    coverage["warnings"][:8]
                )
            item.tokens = {"in": model.input_tokens, "out": model.output_tokens}
        terminal = "completed" if item.state == "done" else "failed"
        if terminal == "failed":
            error = ProjectError(
                422, "invalid_request", "; ".join(item.erreurs) or item.state
            ).body
    except asyncio.CancelledError:
        terminal = "cancelled"
        error = ProjectError(422, "invalid_request", "Turn cancelled by request").body
        raise
    except ProjectError as exc:
        error = exc.body
        raise
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
