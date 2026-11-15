"""Remove Markdown containers emptied by provenance filtering, outside code."""

from bisect import bisect_right
import re

from packages import linear_scan

_LABEL_PREFIX = re.compile(r"[\w -]*")


def _emphasis_viable(text: str) -> linear_scan.Viable:
    """A tag needs a ">" after it: looked up, not rescanned from every "<b"."""
    last_close = text.rfind(">")
    return lambda found: found[0] in "*_`" or last_close >= found.end()


# Emphasis marks and tags.
_EMPHASIS = linear_scan.Scan(
    re.compile(r"[*_`]|</?(?:strong|b|h[1-6])[^>]*+>"),
    re.compile(r"[*_`]|</?(?:strong|b|h[1-6])"),
    linear_scan.attempted,
    _emphasis_viable,
)


def slash_label(text: str) -> bool:
    r"""re.fullmatch(r"[\w -]+\s+/\s+[\w -]+", text), in linear time.

    The expression splits every space run between "[\w -]+" and "\s+" in all
    ways before it fails: a long run took seconds (2026-11-15). The text holds
    exactly one "/"; before it, one or more label characters then one or more
    spaces; after it, the mirror image.
    """
    left, slash, right = text.partition("/")
    return bool(slash) and _label_side(left) and _label_side(right[::-1])


def _label_side(side: str) -> bool:
    """side = label characters (one or more), then whitespace (one or more)."""
    label = _LABEL_PREFIX.match(side).end()  # type: ignore[union-attr]
    spaces = len(side) - len(side.rstrip())
    # A split point k: side[:k] is all label characters, side[k:] all spaces.
    return max(len(side) - spaces, 1) <= min(label, len(side) - 1)


def cleanup(text: str) -> str:
    lines = text.splitlines()
    protected: set[int] = set()
    fence = ""
    for i, line in enumerate(lines):
        marker = re.match(r"\s*(`{3,}|~{3,})", line)
        if fence or marker:
            protected.add(i)
        if marker:
            fence = "" if fence and marker[1].startswith(fence) else marker[1][:3]
    # A removed link between a label and --- can turn that label into a setext
    # heading. Separate rules before pruning so removal cannot promote text.
    boundaries: set[int] = set()
    for i, line in enumerate(lines):
        if i not in protected and re.fullmatch(r"\s*(?:-{3,}|={3,})\s*", line):
            boundaries.add(i)
            lines[i] = ""
    ordered_boundaries = sorted(boundaries)
    # "following" is the next non-blank line, kept while walking backwards:
    # lines after i are final, and searching them again for each line was
    # quadratic once many lines were blanked.
    following: int | None = None
    for i in reversed(range(len(lines))):
        if i + 1 < len(lines) and lines[i + 1].strip():
            following = i + 1
        if i in protected:
            continue
        line = lines[i]
        plain = re.sub(r"^\s*(?:#{1,6}\s+|[-*+•·]\s*|\d+[.)]\s+)?", "", line)
        plain = _EMPHASIS.sub("", plain).strip()
        if not plain:
            lines[i] = ""
            continue
        child = following is not None and (
            len(lines[following]) - len(lines[following].lstrip())
            > len(line) - len(line.lstrip())
        )
        heading = re.match(r"^\s*(#{1,6})\s", line)
        numbered = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        # Inline emphasis in a numbered instruction is content, not a heading.
        # Only an entire title (or a bare slash-separated label) can be pruned.
        numbered_title = bool(numbered) and (
            plain.endswith(":")
            or bool(numbered and re.fullmatch(r"\*\*[^*]+\*\*", numbered[1]))
            or bool(numbered and slash_label(numbered[1]))
        )
        next_line = lines[following] if following is not None else ""
        next_heading = re.match(r"^\s*(#{1,6})\s", next_line)
        section_end = following is None or (
            bisect_right(ordered_boundaries, i) < len(ordered_boundaries)
            and ordered_boundaries[bisect_right(ordered_boundaries, i)] < following
        )
        if heading and next_heading:
            section_end |= len(next_heading[1]) <= len(heading[1])
        if numbered_title:
            section_end |= bool(re.match(r"^\s*\d+[.)]\s", next_line))
        label_peer = bool(re.match(r"^\s*[-*+•·]", line)) and bool(
            re.match(r"^\s*(?:[-*+•·]|\d+[.)]|#)", next_line)
        )
        next_label = next_line.rstrip(" *_`").endswith(":")
        # A list item left as a bare label ("Source:") once its link was removed
        # introduces nothing: without an indented child it is dropped.
        bare_item = bool(re.match(r"^\s*[-*+•·]\s", line)) and plain.endswith(":")
        if not child and (
            bare_item
            or plain.endswith(":")
            and (section_end or label_peer or next_label)
            or (heading or numbered_title)
            and section_end
        ):
            lines[i] = ""
    if lines == text.splitlines():
        return text
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def hollow(original: str, cleaned: str) -> bool:
    if original == cleaned:
        return False

    def words(value: str) -> int:
        return len(re.findall(r"\w+", value))

    return not cleaned.strip() or words(cleaned) * 2 < words(original)
