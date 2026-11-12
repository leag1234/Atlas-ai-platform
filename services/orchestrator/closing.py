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
"""

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
CLOSING_MARK = "Research is closed for this turn: a search limit was reached."


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
            and content.startswith(CLOSING_MARK)
        )
    return False
