"""OpenAI-compatible text/image chat; clients cannot alter tool budgets."""

from packages.profiles import PROFILES, context_window
from packages.file_intent import produces_file
from packages.evidence import estimated_tokens
from packages.language import LANGUAGES, conversation_language, status_language
from pydantic_core import PydanticCustomError

import os
import re

from typing import Any, Literal, Self
from uuid import UUID
from pydantic import ConfigDict, Field, PrivateAttr, model_validator
from packages.images import VisionInput, VisionMessage

from services.orchestrator.documents import Attachment
from services.orchestrator.location import Location

ChatMessage = VisionMessage


CONTINUATION_INSTRUCTIONS = {
    "fr": "Continuez votre réponse précédente exactement là où elle s'est arrêtée. Ne répétez rien et n'ajoutez pas d'introduction.",
    "en": "Continue your previous answer exactly from where it stopped. Do not repeat anything and do not add an introduction.",
    "de": "Setze deine vorherige Antwort genau dort fort, wo sie abgebrochen wurde. Wiederhole nichts und füge keine Einleitung hinzu.",
    "es": "Continúa tu respuesta anterior exactamente donde se interrumpió. No repitas nada ni añadas una introducción.",
    "it": "Continua la tua risposta precedente esattamente da dove si è interrotta. Non ripetere nulla e non aggiungere un'introduzione.",
}


def continuation_instruction(answer: str, language: str) -> str:
    """Ask for a suffix only, preserving any unfinished Markdown structure."""
    instruction = CONTINUATION_INSTRUCTIONS[language]
    open_fence = answer.count("```") % 2 == 1
    cut_citation = bool(re.search(r"\[[^\]]*$|\[[^\]]*\]\([^)]*$", answer))
    lines = [line for line in answer.splitlines() if line.strip()]
    cut_table = len(lines) >= 2 and "|" in lines[-1] and "|" in lines[-2]
    if open_fence or cut_citation or cut_table:
        instruction += (
            " Preserve and complete any unfinished code block, citation, or table."
        )
    return instruction


class ChatRequest(VisionInput):
    model_config = ConfigDict(extra="ignore", strict=True)
    location: Location | None = Field(default=None, exclude=True, repr=False)
    model: str = PROFILES[0]
    documents: list[Attachment] = Field(default_factory=list, max_length=8)
    org_id: str | None = None
    connector_org_id: str | None = None
    file_version_ids: list[str] = Field(default_factory=list)
    _skill_catalog: Any = PrivateAttr(default=None)
    _api_key_request: bool = PrivateAttr(default=False)
    _project_subject: str | None = PrivateAttr(default=None)
    _connector_scope: dict[str, str | None] = PrivateAttr(default_factory=dict)
    _connector_approvals: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _transcript_key: str | None = PrivateAttr(default=None)
    project_id: str | None = None
    conversation_id: str | None = None
    project_action: Literal["chat", "work"] = "chat"
    work_session_id: str | None = None
    code_mode: bool = False
    code_session_id: str | None = None
    atlas_ui: bool = False
    stream: bool = False
    cancel_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        exclude=True,
    )
    reasoning_effort: Literal["none", "low", "high"] = "none"
    max_tokens: int = Field(default=6000, ge=1, le=16000)
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)
    ui_locale: Literal["fr", "de", "es", "it", "en", "nl"] = "fr"
    lang: Literal["fr", "de", "es", "it", "en"] = "fr"
    continuation: bool = Field(default=False, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def discard_empty_assistant_placeholders(cls, value: Any) -> Any:
        """Keep a cancelled Open WebUI turn from blocking the next user message."""
        if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
            return value
        return {
            **value,
            "messages": [
                message
                for index, message in enumerate(value["messages"])
                if not (
                    isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and (
                        message.get("content") is None
                        or (
                            isinstance(message.get("content"), str)
                            and not message["content"].strip()
                        )
                    )
                    and index + 1 < len(value["messages"])
                    and isinstance(value["messages"][index + 1], dict)
                    and value["messages"][index + 1].get("role") == "user"
                )
            ],
        }

    @model_validator(mode="before")
    @classmethod
    def add_continuation_turn(cls, value: Any) -> Any:
        """Adapt Open WebUI's terminal assistant turn without relaxing validation."""
        if not isinstance(value, dict) or value.get("continuation"):
            return value
        messages = value.get("messages")
        if not isinstance(messages, list) or not messages:
            return value
        last = messages[-1]
        if not (
            isinstance(last, dict)
            and last.get("role") == "assistant"
            and isinstance(last.get("content"), str)
            and last["content"].strip()
        ):
            return value
        locale = value.get("ui_locale", "fr")
        if locale == "nl":
            locale = "en"
        language = value.get("lang", locale)
        if locale not in CONTINUATION_INSTRUCTIONS:
            return value
        try:
            from packages.language import conversation_language

            language = conversation_language(
                [VisionMessage.model_validate(message) for message in messages], locale
            )
        except (TypeError, ValueError):
            # VisionInput still reports malformed messages through its normal boundary.
            language = language if language in CONTINUATION_INSTRUCTIONS else locale
        return {
            **value,
            "messages": [
                *messages,
                {
                    "role": "user",
                    "content": continuation_instruction(last["content"], language),
                },
            ],
            "continuation": True,
        }

    @model_validator(mode="before")
    @classmethod
    def discard_unsupported_interface_language(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("ui_locale") in (*LANGUAGES, "nl"):
            return value
        return {key: item for key, item in value.items() if key != "ui_locale"}

    @property
    def status_language(self) -> str:
        # The legacy answer-language hint is not evidence of a known UI locale.
        locale = self.ui_locale if "ui_locale" in self.model_fields_set else None
        if os.environ.get("ATLAS_STABLE") == "1":
            return conversation_language(self.conversation_messages, locale or "en")
        return status_language(self.conversation_messages, locale)

    @property
    def last_real_user_message(self) -> ChatMessage | None:
        messages = self.messages[:-1] if self.continuation else self.messages
        return next((m for m in reversed(messages) if m.role == "user"), None)

    @property
    def user_text(self) -> str:
        message = self.last_real_user_message
        return message.text if message is not None else ""

    @property
    def conversation_messages(self) -> list[ChatMessage]:
        if not self.continuation:
            return self.messages
        message = self.last_real_user_message
        return (
            [*self.messages[:-2], message]
            if message is not None
            else self.messages[:-1]
        )

    @property
    def has_attachments(self) -> bool:
        # Open WebUI filesystem uploads arrive as file tags, not document bytes.
        return bool(self.documents) or any(
            re.search(r'<file\s[^>]*type=[\'"]filesystem[\'"]', block) is not None
            for message in self.messages
            if message.role == "user"
            for block in re.findall(
                r"<attached_files>(.*?)</attached_files>", message.text, re.S
            )
        )

    @property
    def timeout_seconds(self) -> float:
        if self.code_mode or self.project_action == "work":
            from services.orchestrator.code_mode import settings

            # The inner code deadline owns the measured stop, including cleanup.
            return settings().wall_clock + 15
        return (
            300.0
            if self.has_attachments
            or produces_file(self.user_text, include_images=False)
            else 120.0
        )

    @model_validator(mode="after")
    def valid_project(self) -> Self:
        if self.code_mode and (
            self.org_id is not None
            or bool(self.file_version_ids)
            or self.project_id is not None
            or self.conversation_id is not None
            or self.project_action != "chat"
            or self.work_session_id is not None
        ):
            raise ValueError("selector_conflict")
        if self.code_mode:
            from services.orchestrator.code_workspace import enabled

            if not enabled():
                raise ValueError("code_mode_disabled")
            if self.code_session_id is None:
                raise ValueError("code_session_id_required")
            self.code_session_id = str(UUID(self.code_session_id))
        elif self.code_session_id is not None:
            raise ValueError("selector_conflict")
        if self.org_id is not None:
            self.org_id = str(UUID(self.org_id))
            if self.project_id is None or self.conversation_id is None:
                raise ValueError("selector_conflict")
        elif self.file_version_ids:
            raise ValueError("selector_conflict")
        self.file_version_ids = [str(UUID(v)) for v in self.file_version_ids]
        if len(set(self.file_version_ids)) != len(self.file_version_ids):
            raise ValueError("selector_conflict")
        if self.model not in PROFILES:
            raise ValueError("invalid_profile")
        maximum = 6000
        if "max_tokens" not in self.model_fields_set:
            self.max_tokens = maximum
        if self.max_tokens > maximum:
            raise PydanticCustomError(
                "output_budget",
                "Output requested {measured} tokens, limit {limit} tokens",
                {"measured": self.max_tokens, "limit": maximum},
            )
        window = context_window(self.model, vision=any(m.images for m in self.messages))
        tokens = sum(estimated_tokens(m.text) + 4 for m in self.messages)
        tool_allowance = 8192
        if tokens + self.max_tokens + tool_allowance > window:
            raise PydanticCustomError(
                "conversation_window_exceeded",
                "Conversation estimated at {tokens} tokens; model window {window} tokens; "
                "reserved output {reserved} tokens; tool allowance {tools} tokens",
                {
                    "tokens": tokens,
                    "window": window,
                    "reserved": self.max_tokens,
                    "tools": tool_allowance,
                },
            )
        self.reasoning_effort = "none"
        if (self.project_id is None) != (self.conversation_id is None):
            raise ValueError("project_conversation_required")
        if self.project_id is not None and self.conversation_id is not None:
            UUID(self.project_id)
            UUID(self.conversation_id)
        if self.project_action == "work":
            if self.project_id is None or self.work_session_id is None:
                raise ValueError("work_session_required")
            self.work_session_id = str(UUID(self.work_session_id))
        elif self.work_session_id is not None:
            raise ValueError("selector_conflict")
        return self
