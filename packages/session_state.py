"""Typed derived session data and loss-averse, atomic update semantics."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from packages.context_usage import encoded, tokens

Text = Annotated[str, Field(min_length=1, max_length=2000)]
Identifier = Annotated[str, Field(min_length=1, max_length=100)]
TurnId = Annotated[str, Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")]


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Identifier
    text: Text
    source: Text
    turn_id: TurnId


Entries = Annotated[list[Entry], Field(max_length=64)]


class SessionState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decisions: Entries
    established_facts: Entries
    open_questions: Entries
    files_read: Entries
    files_produced: Entries
    current_plan: Entries

    @model_validator(mode="after")
    def bounded(self) -> SessionState:
        for name in type(self).model_fields:
            entries = getattr(self, name)
            if len({e.id for e in entries}) != len(entries):
                raise ValueError("duplicate_session_entry")
        if tokens(encoded(self.model_dump())) > 6000:
            raise ValueError("session_state_too_large")
        return self

    @classmethod
    def empty(cls) -> SessionState:
        return cls(**{name: [] for name in cls.model_fields})


class StateUpdate(SessionState):
    resolved_questions: Annotated[list[Identifier], Field(max_length=64)]
    replace_plan: bool

    @model_validator(mode="after")
    def bounded(self) -> StateUpdate:
        SessionState.model_validate(
            self.model_dump(include=set(SessionState.model_fields))
        )
        return self


def merge(previous: SessionState, update: StateUpdate, turns: set[str]) -> SessionState:
    result: dict[str, Any] = {}
    for name in SessionState.model_fields:
        entries = getattr(update, name)
        if any(e.turn_id not in turns for e in entries):
            raise ValueError("unknown_session_source_turn")
        old = {e.id: e for e in getattr(previous, name)}
        if name == "current_plan" and update.replace_plan:
            old = {}
        if name == "open_questions":
            if not set(update.resolved_questions) <= old.keys():
                raise ValueError("unknown_resolved_question")
            old = {k: v for k, v in old.items() if k not in update.resolved_questions}
        old.update({e.id: e for e in entries})
        result[name] = [e.model_dump() for e in old.values()]
    return SessionState.model_validate(result)
