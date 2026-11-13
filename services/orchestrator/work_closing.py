"""The end of a Work turn once the file it asked for is published.

After publish_document returned, the loop made one more model call that read the
whole turn again and answered the request a second time: a closing message of
1,150 to 1,850 characters restating the document, its sections and its sources.
On staging (corpus of 2026-11-13, 14 document turns that published their file)
the turn ended 38 to 60 seconds after the publication, under "Checking the
result…"; twice the turn's time limit cut that call and a delivered file ended
as a failed turn. The same call measured alone on the staging gateway, on the
context of one of those turns: 10 to 31 seconds, 325 to 1,014 output tokens.

Once the requested file is published, that call reads a closing instruction
(code text, after the tool results, like the closing of a chat turn on a search
limit in closing.py): one or two sentences when the file was all the request
asked for; otherwise the rest of the request, which the model still does. The
same call with the instruction: 1.4 to 3.5 seconds, 39 to 57 output tokens, also
when the project session context puts the question after it
(session_context.py); with "and give me the five essential rules here" the model
still wrote the five rules.

The links stay the code's (file_results.append_deliverable_links adds the ones
the answer lacks), and the source checks are unchanged: an address in the
closing message is kept only if it was retrieved in this turn
(source_truth.without_unretrieved, in the loop).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# The tool that delivers a requested file and returns its receipt.
PUBLISHING = "publish_document"

CLOSING_INSTRUCTION = (
    "The requested file is published and verified ({files}). The user already "
    "gets its download and preview links with your reply: do not write them "
    "and do not say where they are (above, below). If the request asked "
    "only for this file, end the turn now: reply in one or two short sentences, "
    "in the language of the request, that say what was delivered. Do not "
    "summarize the document, list its sections or its sources, or offer more "
    "work. If the request also asks for something else (another file, an answer "
    "or a summary in the conversation), do that now instead."
)


def published(tool: str, output: object) -> dict[str, Any] | None:
    """The receipt of the file this tool call delivered, or None."""
    if tool != PUBLISHING or not isinstance(output, dict) or output.get("error"):
        return None
    receipt = output.get("deliverable")
    return receipt if isinstance(receipt, dict) else None


def closing_instruction(receipts: Sequence[dict[str, Any]]) -> str:
    names = [str(receipt.get("relative_path") or "") for receipt in receipts]
    return CLOSING_INSTRUCTION.format(
        files=", ".join(name for name in names if name) or "one file"
    )


def closing_step(
    tools: Any, file_requested: bool, executed: Sequence[tuple[str, object]]
) -> str | None:
    """The instruction after a batch of tool results, or None.

    Only in a turn that asked for a file, after a batch that published one, and
    once no requested file is still missing.
    """
    if not file_requested:
        return None
    receipts = [
        receipt
        for tool, output in executed
        if (receipt := published(tool, output)) is not None
    ]
    if not receipts:
        return None
    pending = getattr(getattr(tools, "selection", None), "deliverable_pending", None)
    if callable(pending) and pending():
        return None
    return closing_instruction(receipts)
