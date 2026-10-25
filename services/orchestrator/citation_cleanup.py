"""Remove Markdown containers emptied by provenance filtering, outside code."""

import re


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
    for i in reversed(range(len(lines))):
        if i in protected:
            continue
        line = lines[i]
        plain = re.sub(r"^\s*(?:#{1,6}\s+|[-*+•·]\s*|\d+[.)]\s+)?", "", line)
        plain = re.sub(r"[*_`]|</?(?:strong|b|h[1-6])[^>]*>", "", plain).strip()
        if not plain:
            lines[i] = ""
            continue
        following = next(
            (j for j in range(i + 1, len(lines)) if lines[j].strip()), None
        )
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
            or bool(numbered and re.fullmatch(r"[\w -]+\s+/\s+[\w -]+", numbered[1]))
        )
        next_line = lines[following] if following is not None else ""
        next_heading = re.match(r"^\s*(#{1,6})\s", next_line)
        section_end = following is None or any(
            i < boundary < following for boundary in boundaries
        )
        if heading and next_heading:
            section_end |= len(next_heading[1]) <= len(heading[1])
        if numbered_title:
            section_end |= bool(re.match(r"^\s*\d+[.)]\s", next_line))
        label_peer = bool(re.match(r"^\s*[-*+•·]", line)) and bool(
            re.match(r"^\s*(?:[-*+•·]|\d+[.)]|#)", next_line)
        )
        next_label = next_line.rstrip(" *_`").endswith(":")
        if not child and (
            plain.endswith(":")
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
