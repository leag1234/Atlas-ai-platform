"""Project context assembly and threshold enforcement, shared by Chat and Work."""

from __future__ import annotations

import hashlib
import os
from typing import Any

from packages import context_usage
from packages.session_context import Calibration, recent, threshold
from services.orchestrator.chat_schema import ChatMessage
from services.orchestrator.stream_client import sink_context
from services.retrieval.project_identity import ProjectError

calibration = Calibration()


def component(layer: str, value: Any) -> ChatMessage:
    content = context_usage.encoded({layer: value})
    context_usage.register(content, {layer: value})
    return ChatMessage(role="user", content=content)


def assemble(
    context: dict[str, Any], question: ChatMessage
) -> tuple[list[ChatMessage], list[dict[str, Any]]]:
    if "session_state" not in context:
        # P1 fields are additive: an older retrieval service still supplies its
        # canonical combined message until it is upgraded.
        legacy = [
            ChatMessage(
                role="user" if r["kind"] == "question" else "assistant",
                content=r["text"],
            )
            for t in context["history"]
            for r in t["content_refs"]
            if r["kind"] in {"question", "answer"} and r["text"]
        ]
        body = context_usage.encoded(
            dict(
                project_instructions=context["instructions_text"],
                memory=context["facts"],
                passages=context["passages"],
                coverage=context["context_manifest"]["coverage"],
            )
        )
        context_usage.register(
            body,
            dict(
                project_instructions=context["instructions_text"],
                pinned_memory=context["facts"],
                document_passages=dict(
                    passages=context["passages"],
                    coverage=context["context_manifest"]["coverage"],
                ),
            ),
        )
        return legacy + [ChatMessage(role="user", content=body), question], []
    turns, dropped = recent(
        context["history"], set(context.get("represented_turn_ids", []))
    )
    history = [
        component("project_instructions", context["instructions_text"]),
        component("pinned_memory", context["facts"]),
    ]
    if context.get("session_state"):
        history.append(component("session_state", context["session_state"]))
    for turn in turns:
        history.extend(
            ChatMessage(
                role="user" if r["kind"] == "question" else "assistant",
                content=r["text"],
            )
            for r in turn["content_refs"]
            if r["kind"] in {"question", "answer"} and r["text"]
        )
    history.append(
        component(
            "document_passages",
            dict(
                passages=context["passages"],
                coverage=context["context_manifest"]["coverage"],
            ),
        )
    )
    history.append(question)
    return history, dropped


class SessionContext:
    def __init__(
        self, broker: Any, state: dict[str, Any], dropped: list[dict[str, Any]]
    ) -> None:
        self.broker, self.state, self.dropped = broker, state, dropped
        self.last: tuple[str, str, int] | None = None
        self.status = "normal"
        self.warning = ""
        self.question: dict[str, object] | None = None

    async def prepare(
        self, messages: list[dict[str, object]], tools: object, profile: str | None
    ) -> dict[str, Any]:
        # Move only server-registered data, never classify model/user JSON as policy.
        registered = context_usage.parts.get() or {}

        def layer(message: dict[str, object]) -> str | None:
            content = message.get("content")
            parts = registered.get(content, {}) if isinstance(content, str) else {}
            return next(iter(parts)) if len(parts) == 1 else None

        if self.question is None:
            self.question = next(
                (
                    m
                    for m in reversed(messages)
                    if m.get("role") == "user" and layer(m) is None
                ),
                None,
            )
        documents = [m for m in messages if layer(m) == "document_passages"]
        states = [m for m in messages if layer(m) == "session_state"]
        remaining = [
            m
            for m in messages
            if m not in documents and m not in states and m is not self.question
        ]
        prefix = 0
        while prefix < len(remaining) and (
            remaining[prefix].get("role") == "system"
            or layer(remaining[prefix]) in {"project_instructions", "pinned_memory"}
        ):
            prefix += 1
        messages[:] = (
            remaining[:prefix]
            + states
            + remaining[prefix:]
            + documents
            + ([self.question] if self.question is not None else [])
        )
        sample = context_usage.measure(messages, tools, profile)
        family = os.environ.get("ATLAS_PROVIDER", "configured") + ":" + str(profile)
        digest = hashlib.sha256(
            context_usage.encoded(dict(messages=messages, tools=tools)).encode()
        ).hexdigest()
        estimate = sample["total_tokens"]
        count = calibration.count(family, digest, estimate)
        threshold_compacted = False
        window = sample["model_window"]
        represented = set(self.state["context"].get("represented_turn_ids", []))
        for old in self.state["context"].get("history", [])[:-1]:
            if not window or count * 100 < window * 60:
                break
            if old["id"] not in represented or old in self.dropped:
                continue
            sequence = [
                dict(
                    role="user" if r["kind"] == "question" else "assistant",
                    content=r["text"],
                )
                for r in old["content_refs"]
                if r["kind"] in {"question", "answer"} and r["text"]
            ]
            for index in range(prefix + len(states), len(messages) - len(sequence)):
                if sequence and messages[index : index + len(sequence)] == sequence:
                    del messages[index : index + len(sequence)]
                    self.dropped.append(old)
                    threshold_compacted = True
                    break
            sample = context_usage.measure(messages, tools, profile)
            digest = hashlib.sha256(
                context_usage.encoded(dict(messages=messages, tools=tools)).encode()
            ).hexdigest()
            estimate = sample["total_tokens"]
            count = calibration.count(family, digest, estimate)
        self.last = family, digest, estimate
        self.status = (
            threshold(count, sample["model_window"])
            if sample["model_window"]
            else "normal"
        )
        sample.update(
            calibrated_tokens=count,
            calibration_factor=calibration.factors.get(family, 4 / 3),
            context_status=self.status,
        )
        turn = self.state["turn"]
        if self.dropped:
            value = dict(
                turn_ids=[t["id"] for t in self.dropped],
                tokens_removed=sum(
                    context_usage.tokens(context_usage.encoded(t["content_refs"]))
                    for t in self.dropped
                ),
                tokens_added=context_usage.tokens(
                    context_usage.encoded(self.state["context"]["session_state"])
                ),
                reason="threshold"
                if threshold_compacted or self.status != "normal"
                else "recent_window",
            )
            result = await self.broker.call(
                "compact",
                dict(
                    conversation_id=turn["conversation_id"],
                    request_id=turn["request_id"],
                    compaction=value,
                ),
            )
            self.dropped = []
            if sink := sink_context.get():
                for event in result["events"]:
                    await sink({"project": event})
        if self.status in {"warning", "refuse"}:
            self.warning = f"Context used: {count} / {sample['model_window']} tokens ({self.status} threshold: {'95' if self.status == 'refuse' else '80'}%). Start a new session from the session state."
            if self.status == "refuse":
                raise ProjectError(
                    413,
                    "size_limit",
                    self.warning,
                    measurements=[
                        dict(
                            measured=count,
                            limit=sample["model_window"] * 95 // 100,
                            unit="tokens",
                        )
                    ],
                )
        return sample

    def observe(self, provider_tokens: int) -> None:
        if self.last:
            calibration.observe(*self.last, provider_tokens)
