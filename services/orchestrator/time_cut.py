"""A turn cut by a time limit never ends empty.

Measured on the owned server (strict privacy, 2026-11-21): 14 of 20 chat turns
ended at the 120 s limit with HTTP 200 and no answer text, only the collapsed
steps. The answer was being written (first token after about 10 s, then 14
tokens/s), but a researched turn's answer call is held until it ends
(narration.TurnHold: it may still turn out to be a working note before a tool
call) and a factual turn's answer until it is checked (chat_pipeline
._HeldAnswer). The cut dropped what was held, and the stream ended with an
error event the interface does not show as an answer.

Every model call that streams records the text it writes here (model.py). When
a time limit ends the turn, the person reads the answer written so far, then
one sentence saying it was cut by time; when nothing was written, one sentence
saying the model could not answer in time and what to do next.
"""

from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass

from packages import privacy
from packages.user_errors import time_cut_sentence

# The stops of a turn that ran into a time limit: the request deadline, the
# loop's own wall clock, and the answer call's share of it (loop.answer_share).
TIME_CODES = frozenset({"timeout", "wall_clock", "answer_time"})


@dataclass
class Draft:
    """The answer text of the model call under way, after the narration filter."""

    text: str = ""
    # The call ended without asking for a tool: the text is the whole answer.
    finished: bool = False

    def begin(self) -> None:
        self.text, self.finished = "", False

    def add(self, text: str) -> None:
        self.text += text

    def end(self, *, tool_call: bool) -> None:
        if tool_call:
            # What a model writes before a tool call is a working note.
            self.text = ""
        else:
            self.finished = True


current: ContextVar[Draft | None] = ContextVar("time_cut_draft", default=None)


def by_time(errors: Iterable[str]) -> bool:
    """Whether a turn that did not finish was stopped by a time limit."""
    return any(error in TIME_CODES for error in errors)


def written(draft: Draft | None) -> str:
    """The draft text the person may read, or "" (none, a note, protocol text)."""
    from services.orchestrator.narration import (
        protocol_material,
        unfinished_announcement,
        without_internal,
    )

    text = without_internal(draft.text).strip() if draft is not None else ""
    if not text or protocol_material(text) or unfinished_announcement(text):
        return ""
    return text


def closing(language: str, *, written: bool, finished: bool, limit: float) -> str:
    """The one sentence that ends a turn cut by a time limit."""
    return time_cut_sentence(
        language,
        written=written,
        finished=finished,
        owned=privacy.owned_only(),
        limit=limit,
    )


def answer(text: str, sentence: str) -> str:
    """The stored (and non-streamed) answer: what was written, then the sentence."""
    return text.rstrip() + "\n\n" + sentence if text.strip() else sentence
