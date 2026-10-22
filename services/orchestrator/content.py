"""Bounded lexical retrieval over the whole source, with exact Unicode offsets."""

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from packages.evidence import estimated_tokens


@dataclass(frozen=True)
class SelectedPassage:
    text: str
    start: int
    end: int
    examined: int


def select_passages(
    text: str, query: str, budget: int, *, window_bytes: int = 800
) -> list[SelectedPassage]:
    if budget <= 0 or window_bytes <= 0:
        raise ValueError("invalid_content_budget")
    if not text:
        return []
    if len(text.encode()) <= budget:
        return [SelectedPassage(text, 0, len(text), 1)]
    width = min(window_bytes, budget)
    windows: list[tuple[int, int, str]] = []
    start = 0
    while start < len(text):
        part = text[start : start + width].encode()[:width].decode("utf-8", "ignore")
        if not part:
            start += 1
            continue
        end = start + len(part)
        windows.append((start, end, part))
        if end == len(text):
            break
        start += max(1, len(part) * 3 // 4)
    terms = set(re.findall(r"\w+", query.casefold()))
    # A qualified identifier names the subject, unlike generic question words.
    identifiers = {
        name.rsplit(".", 1)[-1].casefold()
        for name in re.findall(r"\b(?:\w+\.)+\w+", query)
    }
    identifiers.update(
        term.casefold() for term in re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", query)
    )
    if window_bytes > 800:
        for identifier in sorted(identifiers):
            for heading in re.finditer(
                r"^" + re.escape(identifier) + r"[ \t]*$", text, re.M | re.I
            ):
                start = max(0, heading.start() - width // 3)
                part = (
                    text[start : start + width]
                    .encode()[:width]
                    .decode("utf-8", "ignore")
                )
                windows.append((start, start + len(part), part))
    matches = [
        terms.intersection(re.findall(r"\w+", p.casefold())) for _, _, p in windows
    ]
    frequency = Counter(term for match in matches for term in match)
    scores = []
    for (_, _, part), match in zip(windows, matches, strict=True):
        contributions = []
        for term in match:
            weight = 1.0
            if term in identifiers:
                # Keep the definition after its heading, not a heading at the cut.
                position = part.casefold().find(term)
                weight = 4 * (2 - position / len(part))
                if re.search(r"^" + re.escape(term) + r"[ \t]*$", part, re.M | re.I):
                    weight *= 8
            contributions.append(math.log1p(len(windows) / frequency[term]) * weight)
        # Accurate summation preserves ties independently of set iteration order.
        scores.append(math.fsum(contributions))
    chosen: list[SelectedPassage] = []
    remaining = budget
    for index in sorted(range(len(windows)), key=lambda i: (-scores[i], i)):
        start, end, part = windows[index]
        if any(start < p.end and end > p.start for p in chosen):
            continue
        size = len(part.encode())
        if size > remaining:
            continue
        chosen.append(SelectedPassage(part, start, end, len(windows)))
        remaining -= size
        if not remaining:
            break
    logging.getLogger(__name__).info(
        "content_selected examined=%d selected=%d bytes=%d",
        len(windows),
        len(chosen),
        budget - remaining,
    )
    return sorted(chosen, key=lambda p: p.start)


def hierarchical_summary(
    text: str, query: str, budget: int = 7900, *, coverage: bool = False
) -> tuple[str, list[SelectedPassage], int]:
    """Reduce a long text: attached documents keep every page in order (coverage);
    web pages and tool output keep the original relevance-first selection."""
    if coverage:
        return _document_summary(text, query, budget)
    return _web_summary(text, query)


def _web_summary(text: str, query: str) -> tuple[str, list[SelectedPassage], int]:
    """Extract section evidence, then synthesize an extractive overview and key passage."""
    sections = [text[i : i + 8192] for i in range(0, len(text), 8192)]
    representatives = [
        p.text for section in sections for p in select_passages(section, query, 160)
    ]
    budget = 7900
    identifiers = set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", query))
    # Comparisons need evidence for both subjects. A single 5000-byte window
    # otherwise consumes nearly all the passage allowance before the second entry.
    window = 2400 if len(identifiers) > 1 else 5000
    while True:
        overview = "\n".join(
            p.text
            for p in select_passages("\n".join(representatives), query, budget // 4)
        )
        relevant = select_passages(text, query, budget * 3 // 4, window_bytes=window)
        summary = overview + "\n" + "\n".join(p.text for p in relevant)
        if estimated_tokens(summary) <= 4000:
            return summary, relevant, len(sections)
        budget //= 2


def _document_summary(
    text: str, query: str, budget: int
) -> tuple[str, list[SelectedPassage], int]:
    """Keep ordered coverage of every document part plus query-relevant evidence."""
    sections = _document_sections(text)
    identifiers = set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", query))
    # Comparisons need evidence for both subjects. A single 5000-byte window
    # otherwise consumes nearly all the passage allowance before the second entry.
    window = 2400 if len(identifiers) > 1 else 5000
    while True:
        # Newlines separating excerpts count against the same byte limit as evidence.
        # The relevance selector can emit no more than one window per budget slice.
        separator_reserve = len(sections) + budget // window + 1
        evidence_budget = max(0, budget - separator_reserve)
        coverage_budget = evidence_budget // 3
        coverage = _coverage_passages(text, sections, coverage_budget)
        relevant = select_passages(
            text,
            query,
            evidence_budget - coverage_budget,
            window_bytes=window,
        )
        passages = sorted(coverage + relevant, key=lambda passage: passage.start)
        summary = "\n".join(passage.text for passage in passages)
        # The token ceiling follows the byte budget: 4000 tokens at the default 7900 bytes.
        if len(summary.encode()) <= budget and estimated_tokens(summary) <= max(
            4000, budget // 3
        ):
            return summary, passages, len(sections)
        budget //= 2


def _document_sections(text: str) -> list[tuple[int, int]]:
    """Split page-marked documents by page, otherwise retain M10-sized sections."""
    markers = list(
        re.finditer(r"^\[(?:Page|Slide) \d+\]|^\[Sheet: [^\]]+\]", text, re.M)
    )
    if markers:
        starts = [marker.start() for marker in markers]
        if starts[0]:
            starts.insert(0, 0)
        return list(zip(starts, starts[1:] + [len(text)], strict=True))
    return [
        (start, min(len(text), start + 8192)) for start in range(0, len(text), 8192)
    ]


def _coverage_passages(
    text: str, sections: list[tuple[int, int]], budget: int
) -> list[SelectedPassage]:
    """Reserve one ordered, page-marked excerpt for every source section."""
    if not sections or budget <= 0:
        return []
    share, remainder = divmod(budget, len(sections))
    passages: list[SelectedPassage] = []
    for index, (start, end) in enumerate(sections):
        allowance = share + (1 if index < remainder else 0)
        excerpt = text[start:end].encode()[:allowance].decode("utf-8", "ignore")
        if excerpt:
            passages.append(
                SelectedPassage(excerpt, start, start + len(excerpt), len(sections))
            )
    return passages
