"""A search refused for a limit closes the research of a chat turn.

When a search tool answers that a limit is reached (the allowance of the
account, the quota of the provider or its hourly rate), nothing the model can
still do in this turn brings the web back. Left with its tools, a model
searched again, turned to internal documents that had nothing to do with the
question, and ended on its own planning notes instead of an answer (staging,
2026-11-12).

So the refusal is the last tool exchange of the turn. The loop puts the
instruction below after the tool results, as the last message of the model
call that follows; the model is offered no tool for that call, and its text is
the answer. This is the next model call, not an additional one: it is reserved
against the same budget and runs under the same deadline as any other.

Work is not concerned: there a file still has to be written and published
after the research, and a tool that keeps failing has its own stop.

Every limit that ends research reaches the model with its own cause, in the
words it should repeat: the allowance of the account or the quota of the search
provider (SEARCH_LIMITS), the fixed steps of one answer (STEP_LIMITS) and the
time of the turn. A model told only "tool_quota_exhausted" past the third search
of an answer wrote "search quota exhausted" in its study (staging, 2026-11-19):
no quota was involved, and the reader was told the service had run out (D5).
"""

import json
from collections.abc import Mapping, Sequence
from contextvars import ContextVar

from packages.language import language_instruction

# The limit that closed the current chat turn, if any. The answer then already says that
# web search was unavailable, so the pipeline adds no "partial results" caveat of its own
# (the two contradicted each other on staging, 2026-11-12). Set in the request's task.
LIMIT_CLOSED: ContextVar[str] = ContextVar("search_limit_closed", default="")

# What each refusal means, in the words given to the model. A limit is always
# named as a limit, never as an outage (D5).
SEARCH_LIMITS = {
    "daily_search_limit": "the web search allowance of this account is used up for now",
    "quota_exceeded": "the quota of the web search service is used up",
    "search_rate_limited": "the web search service is limiting requests for the moment",
}
SEARCH_TOOLS = frozenset({"web_search", "web_images"})
CLOSED_PREFIX = "Research is closed for this turn:"
CLOSING_MARK = CLOSED_PREFIX + " a search limit was reached."
TIME_CLOSING_MARK = CLOSED_PREFIX + " its time is nearly used up."
STEP_CLOSING_MARK = CLOSED_PREFIX + " this answer has used its research steps."

# The steps one answer may take, and what reaching each means. These are fixed
# limits of a single answer: the next message starts again from zero, so none of
# them is a quota, an allowance, an outage or an unavailable service.
STEP_LIMIT_CODE = "answer_step_limit"
STEP_LIMITS = {
    "search_limit": "this answer has run {searches} web searches, the most one answer runs",
    "fetch_limit": "this answer has read {fetches} web pages, the most one answer reads",
    "tool_calls": "this answer has taken {tools} tool steps, the most one answer takes",
}
STEP_LIMIT_RULE = (
    "This is a fixed limit of one answer, not a quota, an exhausted allowance, "
    "an outage or an error: search works again in the next message. Never call "
    "it a quota and never say that searches are exhausted, used up or "
    "unavailable."
)

# The share of a chat turn kept for writing the answer once research has started.
# A researched answer on staging (2026-11-13) ran three searches and two page
# reads, then began its writing call at 100 to 110 s of a 120 s turn: six turns
# out of ten were cut with nothing shown. One research round took 15 to 20 s.
WRITING_RESERVE_SECONDS = 45.0


def limit_code(output: Mapping[str, object]) -> str | None:
    """The search limit a tool result refuses for; None for any other result."""
    code = output.get("error")
    return code if isinstance(code, str) and code in SEARCH_LIMITS else None


def closing_instruction(code: str, language: str) -> str:
    """The fixed instruction of the closing call, answered in the conversation language."""
    return (
        CLOSING_MARK + f" Cause: {SEARCH_LIMITS[code]}.\n"
        "No tool is available for the rest of this turn. Rules for the answer "
        "you write now:\n"
        "1. Write the final answer for the user, from the results already "
        "obtained in this conversation when they are relevant, and from your own "
        "knowledge.\n"
        "2. Mention the limit once, in one short sentence: web search was not "
        "available because its usage limit is reached, so the information may be "
        "dated or incomplete. A usage limit is not an outage, a failure, an error "
        "or a technical reason: never use those words for it.\n"
        "3. Do not recount what was tried: no search attempts, no irrelevant "
        "results, no tool names, no search in internal documents. Do not "
        "describe your plan or your reasoning steps, and never announce what you "
        "are going to write: write the answer itself.\n"
        "4. Do not present a remembered fact as verified today. Give no source "
        "name, link or consultation date that was not retrieved in this "
        "conversation; if sources were requested and none was retrieved, say so "
        "in that same sentence." + language_instruction(language)
    )


def time_closing_instruction(language: str) -> str:
    """The instruction of the closing call when the turn's research time is used up."""
    return (
        TIME_CLOSING_MARK + "\n"
        "No tool is available for the rest of this turn. Rules for the answer "
        "you write now:\n"
        "1. Write the final answer for the user from the results already "
        "obtained in this conversation, and from your own knowledge where they "
        "are silent.\n"
        "2. If a point the user asked about could not be checked in the results, "
        "say so plainly for that point, in one short sentence, and say what the "
        "user can check or ask next. Do not invent it. If you name the cause, "
        "it is the time of this answer: never call it a quota, a usage limit, "
        "an outage or an error.\n"
        "3. Do not recount what was tried: no search attempts, no tool names, no "
        "timing. Do not describe your plan, and never announce what you are "
        "going to write: write the answer itself.\n"
        "4. Give no source name, link or consultation date that was not "
        "retrieved in this conversation." + language_instruction(language)
    )


def step_limit_cause(reason: str, counts: Mapping[str, int]) -> str:
    """What the step limit reached means, with the counts of this answer."""
    return STEP_LIMITS[reason].format(
        searches=counts.get("searches", 0),
        fetches=counts.get("fetches", 0),
        tools=counts.get("tools", 0),
    )


def step_limit_result(reason: str, counts: Mapping[str, int]) -> dict[str, object]:
    """The tool result of a call refused because the answer used its steps."""
    return {
        "error": STEP_LIMIT_CODE,
        "reason": reason,
        "message": "Not run: "
        + step_limit_cause(reason, counts)
        + ". "
        + STEP_LIMIT_RULE
        + " Continue with the results already obtained.",
    }


def step_closing_instruction(
    reason: str, counts: Mapping[str, int], language: str
) -> str:
    """The instruction of the closing call when the answer used its research steps."""
    return (
        STEP_CLOSING_MARK + f" Cause: {step_limit_cause(reason, counts)}.\n"
        "No tool is available for the rest of this turn. Rules for the answer "
        "you write now:\n"
        "1. Write the final answer for the user from the results already "
        "obtained in this conversation, and from your own knowledge where they "
        "are silent.\n"
        "2. If a point the user asked about could not be checked in the results, "
        "say so plainly for that point, in one short sentence: it was not "
        "searched in this answer, and the user can ask for it in a next "
        "message. " + STEP_LIMIT_RULE + "\n"
        "3. Do not recount what was tried: no search attempts, no tool names. "
        "Do not describe your plan, and never announce what you are going to "
        "write: write the answer itself.\n"
        "4. Give no source name, link or consultation date that was not "
        "retrieved in this conversation." + language_instruction(language)
    )


def closing_requested(messages: Sequence[Mapping[str, object]]) -> bool:
    """Whether the loop closed the research of this turn: no tool is offered.

    The instruction follows the tool results of the turn. The same words typed
    by a person in the conversation come before any of them and close nothing.
    """
    instructed = False
    for message in reversed(messages):
        if message.get("role") == "tool":
            return instructed
        instructed |= (
            message.get("role") == "user"
            and isinstance(content := message.get("content"), str)
            and content.startswith(CLOSED_PREFIX)
        )
    return False


# The same call repeated with the same arguments, in a chat turn. A small owned
# model in strict mode (staging -139, 2026-11-20) called the local encyclopedia
# four times in a row with the same query: it ran twice, the third call was
# answered with the previous result, and the fourth stopped the turn with no
# answer. The second identical call now gets the first result back without
# running again; a third one closes the research and the answer is written from
# what is there (LOOP_CLOSING_MARK).
LOOP_CLOSING_MARK = CLOSED_PREFIX + " the same step was repeated without new results."
REPEATED_CALL_CODE = "repeated_call"
REPEAT_LIMIT = 3
# Failures that a second identical call may cure: it runs again once.
TRANSIENT_ERRORS = frozenset(
    {"timeout", "source_unavailable", "search_unavailable", "provider_timeout"}
)
# Steps whose stop a closing call can still answer: research is over, the
# budget and the time of the turn are not (budget and deadline stops keep their
# refusal, D5).
CLOSABLE_STOPS = frozenset(
    {"loop_detected", "tool_calls", "search_limit", "fetch_limit"}
)


def repeated_call_result(previous: Mapping[str, object] | None) -> dict[str, object]:
    """The answer to a call that repeats the previous one exactly, without running it."""
    error = previous.get("error") if previous is not None else None
    message = (
        "Not run again: this exact call was already made in this answer and its "
        "result does not change. "
        + (
            f"It returned no usable result ({error}); repeating it will not help. "
            if isinstance(error, str)
            else "Its result is in previous_result. "
        )
        + "If that result does not answer the question, change the arguments or "
        "write the answer from what you have and say what could not be checked. "
        "The same call a third time ends the research of this answer."
    )
    output: dict[str, object] = {"error": REPEATED_CALL_CODE, "message": message}
    if previous is not None and len(json.dumps(previous, ensure_ascii=False)) <= 4000:
        output["previous_result"] = dict(previous)
    return output


def loop_closing_instruction(language: str) -> str:
    """The instruction of the closing call when the same step kept repeating."""
    return (
        LOOP_CLOSING_MARK + "\n"
        "No tool is available for the rest of this turn. Rules for the answer "
        "you write now:\n"
        "1. Write the final answer for the user from the results already "
        "obtained in this conversation when they are relevant, and from your own "
        "knowledge where they are silent.\n"
        "2. If a result is about something other than what the user asked (a "
        "homonym, another product), ignore it. If a point could not be checked, "
        "say so plainly for that point, in one short sentence, and say what the "
        "user can ask next. Do not invent it. Never call this an outage, a quota "
        "or an error.\n"
        "3. Do not recount what was tried: no attempts, no tool names. Do not "
        "describe your plan, and never announce what you are going to write: "
        "write the answer itself.\n"
        "4. Give no source name, link or consultation date that was not "
        "retrieved in this conversation." + language_instruction(language)
    )


def stop_closing_instruction(
    reason: str, counts: Mapping[str, int], language: str
) -> str:
    """The closing instruction for a stop of CLOSABLE_STOPS."""
    if reason in STEP_LIMITS:
        return step_closing_instruction(reason, counts, language)
    return loop_closing_instruction(language)
