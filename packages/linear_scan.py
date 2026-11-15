"""Linear finditer and sub for patterns whose failed attempts share a long run.

One attempt of a pattern such as ``\\[([^\\]]+)\\]\\(([^\\s)]+)\\)`` is linear, but
finditer and sub make an attempt at every "[": when many attempts read the same
long run and fail at its end ("[a](" repeated, then one far ")"), the run is
read again for each of them and the cost is quadratic. One crafted message or
tool output can then hold the interpreter for minutes (a quadratic expression
froze staging on 2026-11-15).

``Scan`` attempts the pattern only at the openings it can start with and, after
a failed attempt, skips the later openings that are proven to fail the same
way. It returns the same matches as ``pattern.finditer`` and the same text as
``pattern.sub``; tests compare them with the plain calls on random inputs.
"""

from collections.abc import Callable, Iterator
import re

# For one text: given a failed opening, the end position below which every later
# opening (judged by where it ends) fails too.
FailedUntil = Callable[[re.Match[str]], int]
# Builds the FailedUntil of one text (it may keep state for that text).
Failure = Callable[[str], FailedUntil]
# For one text: False when an opening cannot match (checked before the attempt).
Viable = Callable[[re.Match[str]], bool]


class Scan:
    """``finditer`` and ``sub`` of a pattern that never matches empty.

    ``opening`` finds every position where a match can start (with the same
    lookbehinds as the pattern); ``failure`` bounds the openings that share a
    failed attempt's outcome; ``viable``, when given, builds a cheap check that
    rejects an opening without attempting it.
    """

    def __init__(
        self,
        pattern: re.Pattern[str],
        opening: re.Pattern[str],
        failure: Failure,
        viable: Callable[[str], Viable] | None = None,
    ) -> None:
        self.pattern = pattern
        self.opening = opening
        self.failure = failure
        self.viable = viable

    def finditer(self, text: str, start: int = 0) -> Iterator[re.Match[str]]:
        failed_until = self.failure(text)
        viable = self.viable(text) if self.viable else None
        position = start
        failed_end = -1
        while (
            position <= len(text)
            and (found := self.opening.search(text, position)) is not None
        ):
            position = found.start() + 1
            if found.end() < failed_end or (viable is not None and not viable(found)):
                continue
            match = self.pattern.match(text, found.start())
            if match is not None and match.end() > match.start():
                yield match
                position = match.end()
            else:
                failed_end = max(failed_end, failed_until(found))

    def search(self, text: str, start: int = 0) -> re.Match[str] | None:
        return next(self.finditer(text, start), None)

    def findall(self, text: str) -> list[str]:
        """``pattern.findall`` for a pattern with exactly one group."""
        return [match[1] for match in self.finditer(text)]

    def sub(self, replacement: str | Callable[[re.Match[str]], str], text: str) -> str:
        parts: list[str] = []
        last = 0
        for match in self.finditer(text):
            parts.append(text[last : match.start()])
            parts.append(
                replacement(match)
                if callable(replacement)
                else match.expand(replacement)
            )
            last = match.end()
        if not parts:
            return text
        parts.append(text[last:])
        return "".join(parts)


class RunEnd:
    """First stop at or after a position. Positions asked in increasing order
    inside one run read that run once."""

    def __init__(self, text: str, stop: re.Pattern[str]) -> None:
        self.text = text
        self.stop = stop
        self.start = self.end = -1

    def __call__(self, position: int) -> int:
        if not self.start <= position <= self.end:
            found = self.stop.search(self.text, position)
            self.start = position
            self.end = found.start() if found else len(self.text)
        return self.end


def bracket_link_failure(
    label_stop: str,
    target_stop: str,
    *,
    empty_label: bool = False,
    run_end_fails: Callable[[str, int], bool] | None = None,
) -> Failure:
    """``failure`` of ``\\[LABEL\\]\\(TARGET\\)`` links opened by "[".

    LABEL is a run of characters outside ``label_stop`` (a character class body
    that holds "]"), empty only if ``empty_label``. TARGET starts right after
    "](" and is read as a run of characters outside ``target_stop`` (a class
    body that holds ")"). ``run_end_fails(text, end)`` says that no target whose
    run ends at ``end`` can match (by default: the run does not end on ")").
    An opening before the label's first stop reads the same label, so it fails
    too. When the failed attempt's target run ends where every target fails, an
    opening followed by a "]" before that end has its own "](", if any, inside
    the run: its target run ends at the same place, so it fails as well.
    """
    label_end = re.compile(f"[{label_stop}]")
    target_end = re.compile(f"[{target_stop}]")

    def failure(text: str) -> FailedUntil:
        run_end = RunEnd(text, target_end)

        def failed_until(found: re.Match[str]) -> int:
            start = found.start()
            close = label_end.search(text, start + 1)
            if close is None:
                return len(text) + 1
            label = close.start()
            if (
                text[label] != "]"
                or text[label + 1 : label + 2] != "("
                or (label == start + 1 and not empty_label)
            ):
                return label + 1
            end = run_end(label + 2)
            if run_end_fails is None:
                fails = text[end : end + 1] != ")"
            else:
                fails = run_end_fails(text, end)
            if not fails:
                # The attempt failed on something else (its target's start).
                return label + 1
            return text.rfind("]", label, end) + 1

        return failed_until

    return failure


def nothing_after(text: str) -> FailedUntil:
    """``failure`` of a pattern that fails only when nothing after the opening
    can complete it (a lazy span to a closer, read with DOTALL): every later
    opening fails as well."""
    return lambda found: len(text) + 1


def up_to(stop: str, *, flags: int = 0) -> Failure:
    """``failure`` of a pattern whose attempt reads a run up to the first match
    of ``stop`` after its opening and depends only on what is there: every
    later opening that ends before that stop reads the same run and fails.
    Without a stop, nothing later can match."""
    compiled = re.compile(stop, flags)

    def failure(text: str) -> FailedUntil:
        def failed_until(found: re.Match[str]) -> int:
            hit = compiled.search(text, found.end())
            return hit.start() if hit else len(text) + 1

        return failed_until

    return failure


def run_from_start(run: str, *, flags: int = 0) -> Failure:
    """``failure`` of a pattern whose openings inside one run of ``run`` (read
    from the failed opening's start) all reach the run's end the same way."""
    compiled = re.compile(run, flags)

    def failure(text: str) -> FailedUntil:
        def failed_until(found: re.Match[str]) -> int:
            hit = compiled.match(text, found.start())
            return hit.end() if hit else found.end()

        return failed_until

    return failure


def attempted(text: str) -> FailedUntil:
    """``failure`` that skips nothing (the ``viable`` check does the work)."""
    return lambda found: found.end()


# A tag "<...>": without a ">" after a "<" no later "<" closes either.
TAGS = Scan(re.compile(r"<[^>]*+>"), re.compile(r"<"), up_to(r">"))

# An inline data:image URL; a failed one reads its media type to the first ","
# or space, which every later opening before it reaches too.
DATA_IMAGES = Scan(
    re.compile(r"data:image/[^,\s]+,[A-Za-z0-9+/=]+"),
    re.compile(r"data:image/"),
    up_to(r"[,\s]"),
)
