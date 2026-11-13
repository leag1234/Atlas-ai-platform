"""A Work tool that keeps failing the same way must not burn the turn.

The loop already stops a call repeated with identical arguments. A model that
changes its arguments a little each time and gets the same refusal was not
seen: on staging one turn sent 17 document publications, 15 of them refused
(most with the same named reason), for 249 seconds and 0.35 EUR where the
other turns cost 0.01 to 0.05 (2026-11-12).

A failure is counted per tool and per error code. A success of that tool clears
its counts, and a different error has its own count, so a retry that makes
progress is never cut. From SAME_FAILURE_NUDGE failures the tool result tells
the model to change approach; at SAME_FAILURE_STOP the turn stops and the
person gets the explanation of a stopped Work turn, with what was produced.
"""

from collections.abc import Mapping
import json
import re

# Failures of one tool with one error code, without a success of that tool in
# between, before the model is told to change approach, and before the turn stops.
SAME_FAILURE_NUDGE = 3
SAME_FAILURE_STOP = 5
STOP_REASON = "repeated_tool_failure"
NOTE_KEY = "repeated_failure"

# A failed read, search or fetch is a result the model writes with (see
# open_failures in code_mode.py): it is told to change approach, and these
# tools have their own quotas, but the turn is not stopped for them.
RELAYED = frozenset({"read_file", "web_search", "web_fetch"})


def relayed_failure_tools() -> frozenset[str]:
    """Tools whose error is a result to relay, not a broken step."""
    # A machine tool's refusal is a sentence for the person (key not installed
    # yet, host key changed): their next step, not a broken turn.
    from services.orchestrator.work_machine_tools import NAMES

    return RELAYED | frozenset(NAMES)


def stops_on_repeat(tool: str, code: str) -> bool:
    """Whether repeating this failure of this tool stops the turn.

    A machine out of reach (local address, no answer) is a relayed sentence
    too, but no other call changes it: retried, it burnt a turn to its wall
    clock on staging (2026-11-13).
    """
    from services.orchestrator.work_machine_tools import UNREACHABLE

    if explained_limit(code):
        return False
    return tool not in relayed_failure_tools() or code in UNREACHABLE


def family(tool: str) -> str:
    """The machine tools share their counts: one machine, five ways to retry it."""
    from services.orchestrator.work_machine_tools import NAMES

    return "machine tools" if tool in NAMES else tool


def explained_limit(code: str) -> bool:
    """A limit of the person's machine (busy, minutes used up).

    It has its own explanation when the turn ends; stopping here would replace
    it by the general one.
    """
    from services.orchestrator.code_mode import MACHINE_LIMITS

    return code in MACHINE_LIMITS


def error_code(output: Mapping[str, object]) -> str | None:
    """The stable part of a tool error; None when the result is not an error.

    Tool errors are a code ("tool_not_selected"), a sentence that starts with
    its named reason, or a validation report whose first line carries a count.
    The first line with its standalone numbers masked is the same for two
    failures of the same kind and differs between two kinds; a number inside a
    name (a file, a cell) is part of the name.
    """
    error = output.get("error")
    if not error:
        return None
    text = error if isinstance(error, str) else json.dumps(error, sort_keys=True)
    lines = text.strip().splitlines()
    first = lines[0].strip() if lines else ""
    return re.sub(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", "#", first)[:200]


class RepeatedFailures:
    """Counts of the same failure of the same tool within one turn."""

    def __init__(self) -> None:
        self.counts: dict[tuple[str, str], int] = {}

    def record(
        self, tool: str, output: Mapping[str, object]
    ) -> tuple[dict[str, object], int]:
        """The result the model reads, and the count when the turn must stop (else 0)."""
        code = error_code(output)
        group = family(tool)
        if code is None:
            for key in [key for key in self.counts if key[0] == group]:
                del self.counts[key]
            return dict(output), 0
        count = self.counts[group, code] = self.counts.get((group, code), 0) + 1
        if count < SAME_FAILURE_NUDGE:
            return dict(output), 0
        stops = stops_on_repeat(tool, code)
        note = (
            f"{tool} has now failed {count} times in this turn with the same "
            "error. Another variant of the same call will fail the same way: do "
            "not send one. Read the error and change approach: do what it asks, "
            "simplify the input, use another tool, or finish with what exists "
            "and say plainly what could not be done."
        )
        if stops:
            note += f" At {SAME_FAILURE_STOP} such failures the turn stops."
        return (
            {**output, NOTE_KEY: note},
            count if stops and count >= SAME_FAILURE_STOP else 0,
        )
