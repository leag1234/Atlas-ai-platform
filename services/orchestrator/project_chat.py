"""Project chat: scoped retrieval, automatic facts and one shared bounded ledger."""

import json
from decimal import Decimal
import os
from packages.ports import atlas_ports
import re
import time
from pathlib import Path

from pydantic import BaseModel, Field

from services.orchestrator.chat_pipeline import (
    ChatTools,
    Passages,
    Source,
    retrieval_url,
    select_passages,
)
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.vision import process_vision
from packages.language import conversation_language, language_instruction
from services.orchestrator.followup import is_followup, image_iteration
from services.orchestrator.image_tool import declaration, finish, images_enabled
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call, Limits, Message, Query, run
from services.orchestrator.memory import consolidated
from services.orchestrator.model import GatewayModel
from services.orchestrator.stream_client import sink_context
from services.orchestrator.project_stream import AnswerStream
from services.orchestrator.tools import Rag


class MemoryFact(BaseModel):
    id: str
    text: str = Field(max_length=500)
    kind: str


class HistoryTurn(BaseModel):
    question: str
    answer: str


class Context(BaseModel):
    instructions: str = Field(max_length=4000)
    revision: int = Field(ge=0)
    facts: list[MemoryFact] = Field(max_length=8)
    history: list[HistoryTurn] = Field(max_length=8)


class ProjectTools(ChatTools):
    def __init__(self, item: Interaction, project: str) -> None:
        super().__init__(item, allow_image=images_enabled())
        self.project = project

    async def execute(self, call: Call, timeout: float) -> Message:
        if call.name == "generate_image":
            return await super().execute(call, timeout)
        # Memory must never be sent to an external web search engine.
        if call.name != "rag_search":
            return {"error": "tool_unavailable"}
        started = time.monotonic()
        try:
            request = Rag.model_validate_json(call.arguments)
            result = Passages.model_validate(
                await GatewayModel.post(
                    retrieval_url() + f"/projects/{self.project}/search",
                    request.model_dump(),
                    timeout,
                )
            )
            passages = [p.model_dump() for p in result.passages]
            self.item.chunks_recuperes.extend(passages)
            return {
                "trust": "untrusted",
                "data": {"passages": select_passages(result.passages, request.query)},
            }
        finally:
            self.item.latence_ms["retrieval"] += (time.monotonic() - started) * 1000


def render_project_citations(item: Interaction, project: str, answer: str) -> str:
    retrieved = {str(p["chunk_id"]): p for p in item.chunks_recuperes}
    keys = list(dict.fromkeys(re.findall(r"\b[a-f0-9]{64}\b", answer)))
    if not set(keys) <= retrieved.keys():
        raise ValueError("invalid_citation")
    # Sources are validated against this request's scoped retrieval results.
    for key in keys:
        item.citations.append(Source.model_validate(retrieved[key]).model_dump())
        answer = answer.replace(key, f"[Source](/projects/{project}/sources/{key})")
    return answer


async def process_project(request: ChatRequest, item: Interaction) -> None:
    from services.orchestrator.code_ui import project_mode

    if request.org_id is not None or project_mode() or request.project_action == "work":
        from services.orchestrator.project_transcript import process_scoped_project

        await process_scoped_project(request, item)
        return
    started = time.monotonic()
    project = request.project_id
    base = retrieval_url() + f"/projects/{project}"
    question = request.messages[-1].text
    context = Context.model_validate(
        await GatewayModel.post(
            base + "/context",
            {
                "conversation_id": request.conversation_id,
                "query": question,
            },
            5,
        )
    )
    # Client history has no trusted scope. Rebuild it from the validated server
    # conversation; only the current upload belongs to this request's project.
    scoped_messages = [
        message
        for turn in context.history
        for message in (
            ChatMessage(role="user", content=turn.question),
            ChatMessage(role="assistant", content=turn.answer),
        )
    ]
    scoped_messages.append(request.messages[-1])
    scoped = request.model_copy(
        update={
            "messages": scoped_messages,
            "lang": conversation_language(scoped_messages, request.ui_locale),
            "project_id": None,
            "conversation_id": None,
        }
    )
    followup = is_followup(scoped_messages)
    iteration = image_iteration(scoped_messages)
    if followup or request.has_attachments or request.messages[-1].images:
        if followup or request.has_attachments:
            from services.orchestrator.chat_pipeline import process

            await process(scoped, item)
        else:
            await process_vision(scoped, item)
        if item.state == "done":
            await GatewayModel.post(
                base + "/turns",
                {
                    "conversation_id": request.conversation_id,
                    "question": question,
                    "answer": item.reponse,
                    "facts": [],
                    "revision": context.revision,
                },
                max(0.001, request.timeout_seconds - (time.monotonic() - started)),
            )
        return
    model = await GatewayModel.connect(
        os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway")),
        local_enabled=False,
    )
    model.local_enabled, model.observing = False, True
    sink = sink_context.get()
    answer_stream = AnswerStream(sink) if sink else None
    model.sink = answer_stream
    model.configure_quality(request.model, request.max_tokens)
    item.reasoning_effort = request.reasoning_effort
    memory_prefix = (
        ("Project memory:\n" + "\n".join("- " + f.text for f in context.facts) + "\n\n")
        if context.facts
        else ""
    )
    if sink and memory_prefix:
        await sink({"delta": {"content": memory_prefix}, "memory": True})
    model.tools = [
        t
        for t in model.tools
        if isinstance(t.get("function"), dict)
        and isinstance(function := t.get("function"), dict)
        and function.get("name") == "rag_search"
    ]
    if images_enabled():
        model.tools.append(declaration())
    tools = ProjectTools(item, str(project))
    item.cout_eur = 0.1
    history: list[Message] = []
    for turn in context.history:
        history.extend(
            [
                {"role": "user", "content": turn.question},
                {"role": "assistant", "content": turn.answer},
            ]
        )
    system = (
        Path("prompts/project.txt").read_text()
        + language_instruction(scoped.lang)
        + "\n"
        + json.dumps(
            {
                "project_instructions": context.instructions,
                "memory": [f.model_dump() for f in context.facts],
            },
            ensure_ascii=False,
        )
    )
    try:
        result = await run(
            Query(question=iteration or question, lang=scoped.lang),
            model,
            tools,
            system,
            Limits(
                profile=request.model,
                tokens=262144,
                cost=Decimal("0.10"),
                wall_clock=max(
                    0, request.timeout_seconds - (time.monotonic() - started)
                ),
            ),
            history=history,
            terminal_tools=frozenset({"generate_image"})
            if images_enabled()
            else frozenset(),
        )
        item.state, item.cout_eur = result.state, float(result.cost)
        if result.reason:
            item.erreurs.append(result.reason)
        if result.state != "done":
            return
        if tools.image_prompt is not None:
            await finish(scoped, item, result, tools.image_prompt, started)
            answer = item.reponse
            facts: list[str] = []
        else:
            answer, facts = consolidated(result.text, question)
        if (
            tools.image_prompt is None
            and answer_stream
            and answer_stream.shown != answer
        ):
            raise ValueError("project_stream_changed")
        answer = render_project_citations(item, str(project), answer)
        answer = memory_prefix + answer
        await GatewayModel.post(
            base + "/turns",
            {
                "conversation_id": request.conversation_id,
                "question": question,
                "answer": answer,
                "facts": facts,
                "revision": context.revision,
            },
            max(0.001, request.timeout_seconds - (time.monotonic() - started)),
        )
        item.reponse = answer
    finally:
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
