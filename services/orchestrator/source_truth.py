"""Citation provenance uses retrieved tool results, never assistant history."""

import re
from urllib.parse import urlparse

from services.orchestrator.interactions import Interaction

URL = re.compile(r"https?://[^\s<>\"'()\]]+")
REFERENCE = re.compile(r"\[\[atlas-reference:([^\]]+)\]\]")


def drawing_references(item: Interaction) -> set[str]:
    """Trust only exact references registered by the server for this response."""
    payloads = {
        str(image["payload"])
        for image in item.images
        if image.get("kind") == "qr" and "payload" in image
    }
    return payloads | {
        reference
        for image in item.images
        if image.get("kind") in {"mechanical_svg", "drawing_svg", "qr", "web_image"}
        and "reference" in image
        for reference in (
            str(image["reference"]),
            str(image["reference"]) + "?download=1",
        )
    }


def visible_sources(item: Interaction) -> list[str]:
    """Commons attribution is already rendered with its title, author and license."""
    if not item.commons_requests:
        return item.retrieved_urls
    return [
        url
        for url in item.retrieved_urls
        if urlparse(url).hostname
        not in {"commons.wikimedia.org", "upload.wikimedia.org"}
    ]


def urls(text: str) -> set[str]:
    return {match.rstrip(".,;:)]}>'\"") for match in URL.findall(text)}


def without_unretrieved(text: str, retrieved: set[str]) -> str:
    from services.orchestrator.citation_fragments import tool_citations
    from services.orchestrator.mermaid_check import FENCE

    original = text
    text = tool_citations(text, retrieved)
    diagram_ranges: list[tuple[int, int]] = []

    def replace(match: re.Match[str]) -> str:
        value = match[0].rstrip(".,;:)]}>'\"")
        if value in retrieved:
            return match[0]
        if any(start <= match.start() < end for start, end in diagram_ranges):
            return "unverified source"
        return "[unverified source]"

    # Remove fabricated Markdown citations as a whole, including placeholder URLs.
    # Bare unknown URLs retain the existing explicit provenance notice.
    text = re.sub(
        r"(?<!!)\[[^\]\n]+\]\((?:\[unverified source\]|https?://[^\s]+?)\)",
        lambda m: m[0] if urls(m[0]) and urls(m[0]) <= retrieved else "",
        text,
        flags=re.I,
    )
    definitions = re.compile(r"(?m)^\s{0,3}\[([^\]\n]+)\]:[ \t]*(\S+)[^\n]*$")
    for definition in list(definitions.finditer(text)):
        if definition[2].strip("<>") not in retrieved:
            label = re.escape(definition[1])
            text = re.sub(r"(?<!!)\[[^\]\n]+\]\[" + label + r"\]", "", text, flags=re.I)
            text = text.replace(definition[0], "")
    text = text.replace("[unverified source]", "")
    diagram_ranges = [(m.start(), m.end()) for m in FENCE.finditer(text)]
    text = URL.sub(replace, text)
    # Retrieval provenance contains URLs, not model-authored visit dates. Never
    # preserve an invented date merely because some other URL was retrieved.
    text = re.sub(
        r"(?i)[ \t]*\(?\b(?:consult[eé]\w* le|consulted (?:on|at)|accessed on)"
        r"[ \t]+(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4}|"
        r"\d{1,2} [^\s()]+ \d{4}|[A-Za-z]+ \d{1,2},? \d{4})\)?",
        "",
        text,
    )
    if not retrieved:
        text = re.sub(r"(?im)^sources?\s*:.*(?:\n|$)", "", text)

    if text != original:
        from services.orchestrator.citation_cleanup import cleanup

        text = cleanup(text)
    return text


def inline_source_markers(
    text: str, retrieved: list[str], *, preserve_labels: bool = False
) -> str:
    """Turn retrieved web URLs into compact Markdown links Open WebUI renders."""
    numbers = {url: index for index, url in enumerate(dict.fromkeys(retrieved), 1)}
    text = provider_reference_markers(text, retrieved)

    def markdown(match: re.Match[str]) -> str:
        url = match.group(1).rstrip(".,;:)]}>'\"")
        if preserve_labels:
            return match[0]
        return f"[{numbers[url]}]({url})" if url in numbers else match.group(0)

    text = re.sub(r"(?<!!)\[[^\]]+\]\((https?://[^\s)]+)\)", markdown, text)

    def bare(match: re.Match[str]) -> str:
        url = match[0].rstrip(".,;:)]}>'\"")
        tail = match[0][len(url) :]
        return f"[{numbers[url]}]({url}){tail}" if url in numbers else match[0]

    rendered = re.sub(r"(?<!\]\()https?://[^\s<>\"'()\]]+", bare, text)
    return rendered


def provider_reference_markers(text: str, retrieved: list[str]) -> str:
    """Resolve only typed provider citation chunks, never service-owned links."""
    numbers = {url: index for index, url in enumerate(dict.fromkeys(retrieved), 1)}

    def reference(match: re.Match[str]) -> str:
        identifier = match.group(1)
        if identifier in numbers:
            url = identifier
        elif identifier.isdecimal() and int(identifier) < len(retrieved):
            url = retrieved[int(identifier)]
        else:
            # A reference ID is meaningful only in the provider's tool result.
            # Never render it unless it resolves to this conversation's retrieval.
            return ""
        return f"[{numbers[url]}]({url})"

    # Resolve a reference list together so rejected numeric IDs cannot leave
    # separators behind after provenance filtering.
    run = re.compile(REFERENCE.pattern + r"(?:[,;][ \t]*" + REFERENCE.pattern + r")*")
    return run.sub(
        lambda match: ", ".join(
            rendered
            for part in REFERENCE.finditer(match[0])
            if (rendered := reference(part))
        ),
        text,
    )


def source_footer(retrieved: list[str], language: str) -> str:
    """Expose the actual retrieval trail separately from the model's citations."""
    import json
    from pathlib import Path

    if not retrieved:
        return ""
    labels = json.loads(Path("prompts/activity-labels.json").read_text())
    labels = labels.get(language, labels["en"])
    consulted = {
        "en": "Sources consulted in this conversation",
        "fr": "Sources consultées dans cette conversation",
        "de": "In diesem Gespräch konsultierte Quellen",
        "es": "Fuentes consultadas en esta conversación",
        "it": "Fonti consultate in questa conversazione",
    }
    return (
        "\n\n<details>\n<summary>"
        + consulted.get(language, consulted["en"])
        + " — "
        + str(labels["web_search"])
        + " / "
        + str(labels["web_fetch"])
        + "</summary>\n\n"
        + "\n".join("- <" + url + ">" for url in dict.fromkeys(retrieved))
        + "\n</details>"
    )
