"""The <attached_files> blocks a chat client adds around attached documents.

Read with packages.linear_scan: a block opening without its closing tag ends
every later attempt as well, where the lazy span would otherwise be reread
from each later opening to the end of the message (quadratic).
"""

from bisect import bisect_left
import re

from packages import linear_scan

_OPENING = re.compile(r"<attached_files>")
BLOCKS = linear_scan.Scan(
    re.compile(r"<attached_files>.*?</attached_files>", re.S),
    _OPENING,
    linear_scan.nothing_after,
)
BLOCKS_ANY_CASE = linear_scan.Scan(
    re.compile(r"<attached_files>.*?</attached_files>", re.S | re.I),
    re.compile(r"<attached_files>", re.I),
    linear_scan.nothing_after,
)
CONTENTS = linear_scan.Scan(
    re.compile(r"<attached_files>(.*?)</attached_files>", re.S),
    _OPENING,
    linear_scan.nothing_after,
)
_CLOSING = re.compile(r"</attached_files>", re.I)


def _tag_viable(text: str) -> linear_scan.Viable:
    """An "[attachment: ...]" line tag needs a "]" before its line ends; a block
    needs its closing tag after it. Both checked by lookup, not by scanning."""
    brackets = [i for i, char in enumerate(text) if char == "]"]
    newlines = [i for i, char in enumerate(text) if char == "\n"]
    closings = [match.start() for match in _CLOSING.finditer(text)]
    last_closing = closings[-1] if closings else -1

    def viable(found: re.Match[str]) -> bool:
        if found[0].startswith("["):
            index = bisect_left(brackets, found.end())
            if index == len(brackets):
                return False
            line = bisect_left(newlines, found.end())
            return line == len(newlines) or brackets[index] < newlines[line]
        return found.end() <= last_closing

    return viable


# A one-line "[attachment: name]" tag or a whole block.
TAGS = linear_scan.Scan(
    re.compile(
        r"\[attachment: [^\n]*\]|<attached_files>.*?</attached_files>", re.S | re.I
    ),
    re.compile(r"\[attachment: |<attached_files>", re.I),
    linear_scan.attempted,
    _tag_viable,
)
