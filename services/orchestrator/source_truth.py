"""Citation provenance uses retrieved tool results, never assistant history."""

from contextvars import ContextVar
import re
from urllib.parse import urlparse

from packages import linear_scan
from services.orchestrator.interactions import Interaction

URL = re.compile(r"https?://[^\s<>\"'()\]]+")
# An XML namespace declaration directly before a URL: xmlns="..." or xmlns:x="...".
_NAMESPACE = re.compile(r"\bxmlns(?::[\w.-]+)?\s*=\s*[\"']$")
REFERENCE = re.compile(r"\[\[atlas-reference:([^\]]+)\]\]")
# A reference list; a failed opening reads to the first "]", which every later
# opening before it reaches too (not retried: quadratic on unclosed openings).
_REFERENCE_RUNS = linear_scan.Scan(
    re.compile(REFERENCE.pattern + r"(?:[,;][ \t]*" + REFERENCE.pattern + r")*"),
    re.compile(r"\[\[atlas-reference:"),
    linear_scan.up_to(r"\]"),
)
# A link label starts at the first "[" of a run of "[" that can start one: the
# later ones reach the same "]" with the same outcome, and retrying each of them
# rescans to that "]" (100 000 "[" took 14 seconds, 2026-11-15).
LABEL_START = r"(?:(?<!\[)|(?<=!\[))"
_MARKDOWN_LINK = re.compile(
    rf"(?<!!){LABEL_START}\[\[([^\]\n]+)\]\(([^)\n]+)\)\]"
    rf"|(?<!!){LABEL_START}\[([^\]\n]+)\]\(([^)\n]+)\)"
)
_LINK_OPENING = re.compile(rf"(?<!!){LABEL_START}\[")
# Openings that share a failed label or target run are not retried: many "[a]("
# before one far ")" would rescan that run from each of them (quadratic).
_MARKDOWN_LINKS = linear_scan.Scan(
    _MARKDOWN_LINK,
    _LINK_OPENING,
    linear_scan.bracket_link_failure(r"\]\n", r")\n"),
)
_PLACEHOLDER_START = re.compile(r"\[unverified", re.I)
# A fabricated citation: a link to a placeholder or to a web URL (up to the
# first ")" after the URL's first character).
_CITATION_LINK = re.compile(
    rf"(?<!!){LABEL_START}\[[^\]\n]+\]\((?:\[unverified source\]|https?://[^\s][^\s)]*+)\)",
    re.I,
)
_CITATION_LINKS = linear_scan.Scan(
    _CITATION_LINK,
    _LINK_OPENING,
    linear_scan.bracket_link_failure(
        r"\]\n",
        r"\s)",
        # A target run that stops on the space of "[unverified source" may
        # still be that placeholder.
        run_end_fails=lambda text, end: text[end : end + 1] != ")"
        and not (
            text[end : end + 1] == " "
            and _PLACEHOLDER_START.fullmatch(text, max(0, end - 11), end) is not None
        ),
    ),
)
_WEB_LINKS = linear_scan.Scan(
    re.compile(rf"(?<!!){LABEL_START}\[[^\]]+\]\((https?://[^\s)]+)\)"),
    _LINK_OPENING,
    linear_scan.bracket_link_failure(r"\]", r"\s)"),
)
_PRODUCT_URL_KEYS = frozenset(
    {
        "url",
        "download_url",
        "download_route",
        "preview_url",
        "preview_route",
        "pr_url",
        "content_url",
        "file_url",
        "reference",
    }
)


def tool_result_urls(value: object) -> set[str]:
    """Return URLs explicitly emitted in product-owned result fields only."""
    if isinstance(value, dict):
        found = set().union(*(tool_result_urls(part) for part in value.values()))
        for key, part in value.items():
            if key in _PRODUCT_URL_KEYS and isinstance(part, str):
                found.update(urls(part))
        return found
    if isinstance(value, list):
        return set().union(*(tool_result_urls(part) for part in value))
    return set()


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
        if image.get("kind")
        in {"mechanical_svg", "drawing_svg", "qr", "web_image", "picture"}
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


def person_citations(text: str, evidence_urls: set[str]) -> str:
    """Keep person-synthesis citations only when they name supplied evidence.

    A malformed Markdown target can make a model's unsupported attribution look like
    a source. If that was the sentence's only citation, the claim cannot be retained.
    """

    def target(match: re.Match[str]) -> str:
        return (match.group(2) or match.group(4) or "").strip().strip("<>")

    def valid(match: re.Match[str]) -> bool:
        return target(match) in evidence_urls

    # A link needs "](" and a closing ")": without them nothing can match, and
    # each unclosed "[" would be rescanned to the end of its line.
    if "](" not in text or ")" not in text:
        return text
    parts = re.split(r"(?<=[.!?])(?=\s)|(?<=\n)", text)
    cleaned: list[str] = []
    for part in parts:
        matches = list(_MARKDOWN_LINKS.finditer(part))
        if matches and any(not valid(match) for match in matches):
            kept = _MARKDOWN_LINKS.sub(
                lambda match: match[0] if valid(match) else "", part
            )
            if not any(valid(match) for match in _MARKDOWN_LINKS.finditer(kept)):
                continue
            cleaned.append(kept)
        else:
            cleaned.append(part)
    return "".join(cleaned)


# URLs the user wrote in this conversation's messages. Repeating an address the
# user supplied is not a claim of a fetched source (D7 covers model citations),
# so the guard keeps them. Set per request by chat_pipeline.process.
user_urls: ContextVar[frozenset[str]] = ContextVar(
    "atlas_user_urls", default=frozenset()
)


def canonical_url(url: str) -> str:
    """One spelling of an address: scheme, host case, a "www." prefix, a final
    slash and a fragment do not make another page. A retrieved page cited as
    "https://www.python.org/downloads/" was removed as unretrieved because the
    fetch had no final slash (staging 2026-11-12)."""
    try:
        parsed = urlparse(url.strip().strip("<>"))
        host = (parsed.hostname or "").lower().removeprefix("www.")
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        # An address the standard parser refuses ("[" in the host, a port that
        # is not a number) is compared as written: an answer quoting one failed
        # with an unexpected stream exception (staging 2026-11-13).
        return url.strip().strip("<>").lower()
    path = parsed.path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{host}{port}{path}{query}"


def without_unretrieved(text: str, retrieved: set[str]) -> str:
    retrieved = set(retrieved) | user_urls.get()
    known = {canonical_url(url) for url in retrieved}

    def fetched(url: str) -> bool:
        return url in retrieved or canonical_url(url) in known

    from services.orchestrator.citation_fragments import code_spans, tool_citations
    from services.orchestrator.mermaid_check import FENCE

    original = text
    text = tool_citations(text, retrieved)
    diagram_ranges: list[tuple[int, int]] = []
    code_ranges: list[tuple[int, int]] = []

    def replace(match: re.Match[str]) -> str:
        value = match[0].rstrip(".,;:)]}>'\"")
        if fetched(value):
            return match[0]
        if any(start <= match.start() < end for start, end in diagram_ranges):
            return "unverified source"
        # A URL inside a fenced code block (an XML namespace, an endpoint in an
        # example) or the value of an xmlns attribute is code, not a cited source:
        # replacing it breaks the code the user copies.
        if any(start <= match.start() < end for start, end in code_ranges):
            return match[0]
        if _NAMESPACE.search(match.string, max(0, match.start() - 40), match.start()):
            return match[0]
        return "[unverified source]"

    # Remove fabricated Markdown citations as a whole, including placeholder URLs.
    # Bare unknown URLs retain the existing explicit provenance notice.
    text = _CITATION_LINKS.sub(
        lambda m: m[0] if urls(m[0]) and all(map(fetched, urls(m[0]))) else "",
        text,
    )
    definitions = re.compile(r"(?m)^\s{0,3}\[([^\]\n]+)\]:[ \t]*(\S+)[^\n]*$")
    for definition in list(definitions.finditer(text)):
        if not fetched(definition[2].strip("<>")):
            label = re.escape(definition[1])
            text = re.sub(
                rf"(?<!!){LABEL_START}\[[^\]\n]+\]\[" + label + r"\]",
                "",
                text,
                flags=re.I,
            )
            text = text.replace(definition[0], "")
    text = text.replace("[unverified source]", "")
    diagram_ranges = [(m.start(), m.end()) for m in FENCE.finditer(text)]
    # Fenced code blocks only: an inline `https://...` in prose can still be a
    # citation and stays under the guard.
    code_ranges = [
        span
        for span in code_spans(text)
        if text.startswith("```", span[0])
        and not any(start < span[1] and span[0] < end for start, end in diagram_ranges)
    ]
    text = URL.sub(replace, text)
    # Retrieval provenance contains URLs, not model-authored visit dates. Never
    # preserve an invented date merely because some other URL was retrieved.
    # Space runs are entered at their first character only (here and below): a
    # retry from each later space rescans the run, the square of its length
    # (100 000 spaces took over 30 seconds, 2026-11-15). The matches are the same.
    text = re.sub(
        r"(?i)(?<![ \t])[ \t]*\(?\b(?:consult[eé]\w* le|consulted (?:on|at)|accessed on)"
        r"[ \t]+(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4}|"
        r"\d{1,2} [^\s()]+ \d{4}|[A-Za-z]+ \d{1,2},? \d{4})\)?",
        "",
        text,
    )
    if not retrieved:
        text = re.sub(r"(?im)^sources?\s*:.*(?:\n|$)", "", text)

    if text != original:
        from services.orchestrator.citation_cleanup import cleanup

        text = cleanup(citation_separators(text))
    return text


# Separators and brackets a removed citation leaves in its list: "([1](a), , )."
# and "Sources: [1](a),, " (staging 2026-11-16), "beau , [1](a)" when the first
# one went, "beau ()." when all did. Space runs are entered at their first
# character only, so every expression stays linear.
_SEPARATOR_REPAIRS = (
    (re.compile(r"([,;])(?:[ \t]*[,;])+"), r"\1"),
    (re.compile(r"\([ \t]*[,;][ \t]*"), "("),
    (re.compile(r"(?<![ \t])[ \t]*[,;][ \t]*(?=\))"), ""),
    (re.compile(r"(?<![ \t\n])[ \t]+[,;][ \t]*(?=\[)"), " "),
    # A function call such as print() has no space before its parentheses.
    (re.compile(r"(?<![ \t])[ \t]+\([ \t]*\)|^\([ \t]*\)[ \t]*", re.M), ""),
    (re.compile(r"(?<=\))[ \t]*[,;][ \t]*$", re.M), ""),
)


def citation_separators(text: str) -> str:
    """Repair the citation lists of a text some citations were removed from."""
    from services.orchestrator.citation_fragments import split_code

    def repair(value: str) -> str:
        for pattern, replacement in _SEPARATOR_REPAIRS:
            value = pattern.sub(replacement, value)
        return value

    return "".join(
        part if index % 2 else repair(part)
        for index, part in enumerate(split_code(text))
    )


def clean_citation_residue(text: str) -> str:
    """Remove citation-only connective phrases and punctuation gaps in prose."""
    from services.orchestrator.citation_fragments import split_code

    def clean(value: str) -> str:
        value = re.sub(
            r"(?i)(?<![ \t])[ \t]+(?:issues? de sources?|according to sources?|from sources?)"
            r"[ \t]*(?=[.,;]|$)",
            "",
            value,
        )
        value = re.sub(r",[ \t]*(?=\.)", "", value)
        return re.sub(r"(?<![ \t])[ \t]+([.,])", r"\1", value)

    return "".join(
        part if index % 2 else clean(part)
        for index, part in enumerate(split_code(text))
    )


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

    text = _WEB_LINKS.sub(markdown, text)

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
    return _REFERENCE_RUNS.sub(
        lambda match: ", ".join(
            rendered
            for part in REFERENCE.finditer(match[0])
            if (rendered := reference(part))
        ),
        text,
    )


# What each page read in this turn said (search titles and snippets, fetched
# texts), set per request by chat_pipeline.process: the consulted-sources list
# reads it to leave out the pages that are about nothing the turn asked or said.
page_texts: ContextVar[dict[str, list[str]] | None] = ContextVar(
    "atlas_page_texts", default=None
)
PAGE_CHARACTERS = 20_000


def record_page(url: str, text: str) -> None:
    pages = page_texts.get()
    if pages is not None and text.strip():
        held = pages.setdefault(url, [])
        if sum(map(len, held)) < PAGE_CHARACTERS:
            held.append(text[:PAGE_CHARACTERS])


def unrelated_pages(urls: list[str], question: str, answer: str) -> set[str]:
    """Pages read that are about neither the question nor what the answer states.

    A page stays listed when the answer cites it, when it holds one of the
    answer's distinctive details (a name, a title, a date, a year, a number of
    three digits or more) that the person did not give, or when it names as
    many of the question's subject words as a page about the subject does (the
    rule by which trip answers list their pages, travel_rebuild.unrelated). A
    page whose text the turn never saw is kept. Staging 2026-11-20: a
    percentage question listed a stadium, a pharmacy and two economics articles,
    every hit of a search that found nothing on it.
    """
    pages = page_texts.get() or {}
    if not pages or not urls:
        return set()
    from services.orchestrator import unsourced_details as details
    from services.orchestrator.grounding import STOP, normalized
    from services.orchestrator.link_requests import own_words

    asked = own_words(question)
    wanted = {
        word
        for word in normalized(asked).split()
        if len(word) > 2
        and not word.isdigit()
        and word not in STOP
        and word not in details.ASK_WORDS
    }
    if not wanted:
        return set()
    body = answer.split("<details>", 1)[0]
    given = details.Evidence.of([asked])
    nothing = details.Evidence()
    distinctive = [
        detail
        for detail in details.extract(body)
        if (detail.kind != "number" or len(re.sub(r"\D", "", detail.text)) >= 3)
        and not details.supported(detail, nothing, given)
    ]
    shared: dict[str, int] = {}
    used: set[str] = set()
    for url in dict.fromkeys(urls):
        texts = pages.get(url)
        if not texts:
            continue
        text = " ".join(texts)
        shared[url] = len(wanted & set(normalized(text).split()))
        if url in body:
            used.add(url)
        elif distinctive:
            held = details.Evidence.of([text])
            if any(details.supported(d, held, nothing) for d in distinctive):
                used.add(url)
    best = max(shared.values(), default=0)
    if not best:
        # No page names any word of the question (pages in another language):
        # the words cannot tell the pages apart, all stay listed.
        return set()
    floor = min(2, best, len(wanted))
    return {url for url, count in shared.items() if count < floor and url not in used}


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
