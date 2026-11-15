"""Regular expressions on user text, attachments and tool output stay linear.

A super-linear expression holds the interpreter while it runs: one crafted
message, attached file or fetched page then freezes every endpoint of the
instance (number provenance froze staging for 20 minutes on 2026-11-15). The
audit after that incident found such expressions outside the answer path too.
Each was rewritten to read its input once, with the same results.

Two checks per site: the rewritten form returns what the former form returned
on random inputs built from the characters that matter to it, and it stays
within a time budget on long inputs that made the former form backtrack.
"""

from collections.abc import Callable
import random
import re
import time
import unittest

import answer_content
import image_prompt
import image_scene
from packages import (
    artifact_html,
    artifact_source,
    attached_blocks,
    language,
    linear_scan,
    project_memory,
)
from packages.doc_render import cells
from services.orchestrator import (
    appearance,
    artifact_tools,
    capability_strategy,
    chat_pipeline,
    chat_schema,
    chat_stream,
    citation_cleanup,
    citation_fragments,
    code_verification,
    code_workspace,
    diagram,
    exact_tools,
    file_content,
    file_results,
    followup,
    image_verification,
    mermaid_check,
    nearby,
    project_citations,
    request_router,
    shopping_prices,
    source_truth,
    work_remote_tools,
)
from services.orchestrator.pictures import rewrite, sanitise, svg_doc, svg_text
from services.tts import service as tts

HASH = "0123456789abcdef" * 4

# The former forms, kept here as the reference of the differential checks.
FORMER_STYLE_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.I)
FORMER_STYLE_IMPORT = re.compile(r"@import\s+['\"]([^'\"]+)['\"]", re.I)
FORMER_STYLE_IMPORT_URL = re.compile(
    r"@import\s+url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.I
)
FORMER_FILE_LINK = re.compile(r"(?<!\[)\[([^\]]+)\]\(([^\s)]+)\)")
FORMER_CITATION_LINK = re.compile(
    rf"(?<!!){source_truth.LABEL_START}\[[^\]\n]+\]\((?:\[unverified source\]|https?://[^\s]+?)\)",
    re.I,
)
FORMER_WEB_LINK = re.compile(
    rf"(?<!!){source_truth.LABEL_START}\[[^\]]+\]\((https?://[^\s)]+)\)"
)
FORMER_SOURCE_LINK = re.compile(r"(?<!\[)\[[^\]]*\]\([^)]*/sources/[a-f0-9]{64}\)")
FORMER_HOLDS = r"(.+?) holds (.+?)'s (?:own )?hand(?: with his hand)?"
FORMER_SECRET = (
    r"(?i)(\b(?:[\w-]*(?:password|secret|api[_-]?key|token|private_key)[\w-]*)"
    r"[\"']?\s*[=:]\s*[\"']?)[^\s\"',;]+"
)
FORMER_UNIT_CUES = re.compile(
    exact_tools._UNIT_CUES.pattern.replace(exact_tools._NUMBER, r"\d+(?:[.,]\d+)?")
)
FORMER_HEREDOC = re.compile(
    r"([^\n<>]*\bpython(?:3)?(?:\.\d+)?)(?:\s+-)?\s*<<\s*"
    r"(['\"]?)([A-Za-z_][A-Za-z_0-9]*)\2\s*\n(.*?)\n\3\n?",
    re.S,
)
FORMER_PLACE_JOINER = re.compile(
    r"\s+(?:à|a|au|aux|in|at|near|bei|en|a la|in der)\s+", re.I
)
FORMER_EXPLICIT = (
    r"\s*(?:remember that|remember|retiens que|retiens|merke dir(?:,? dass)?|"
    r"recuerda que|recuerda|ricorda che|ricorda)\s*[:：]?\s+(.+?)\s*"
)


def spans(matches: object) -> list[tuple[tuple[int, int], tuple[str | None, ...]]]:
    return [(match.span(), match.groups()) for match in matches]  # type: ignore[attr-defined]


def groups(match: re.Match[str] | None) -> object:
    return None if match is None else (match.span(), match.groups())


LINK_PIECES = [
    "[",
    "]",
    "(",
    ")",
    "![",
    "[[",
    "](",
    " ",
    "\n",
    "a",
    "x",
    "!",
    "/",
    "http://",
    "https://e.test/p",
    "HTTPS://",
    "[unverified source]",
    "[unverified",
    " source]",
    "/sources/" + HASH,
    HASH,
    ")]",
    "[a](",
    "[b]",
    "](https://e.test/p)",
    "](http://x",
    "([unverified source])",
    "](/x/sources/" + HASH,
]
STYLE_PIECES = [
    "url(",
    "URL(",
    "@import ",
    "@import url(",
    " ",
    "\t",
    "\n",
    "'",
    '"',
    ")",
    "(",
    "a",
    "#x",
    "data:",
    "u",
    "url",
]

# Site name: (pieces, former, rewritten); each callable maps a text to a
# comparable result.
DIFFERENTIAL: dict[
    str, tuple[list[str], Callable[[str], object], Callable[[str], object]]
] = {
    "style url": (
        STYLE_PIECES,
        lambda t: spans(FORMER_STYLE_URL.finditer(t)),
        lambda t: spans(artifact_html.STYLE_URLS.finditer(t)),
    ),
    "style import": (
        STYLE_PIECES,
        lambda t: spans(FORMER_STYLE_IMPORT.finditer(t)),
        lambda t: spans(artifact_html.STYLE_IMPORT_PATTERN.finditer(t)),
    ),
    "style import url": (
        STYLE_PIECES,
        lambda t: spans(FORMER_STYLE_IMPORT_URL.finditer(t)),
        lambda t: spans(artifact_html.STYLE_IMPORT_URLS.finditer(t)),
    ),
    "file links": (
        LINK_PIECES,
        lambda t: spans(FORMER_FILE_LINK.finditer(t)),
        lambda t: spans(file_results._LINKS.finditer(t)),
    ),
    "person citation links": (
        LINK_PIECES,
        lambda t: spans(source_truth._MARKDOWN_LINK.finditer(t)),
        lambda t: spans(source_truth._MARKDOWN_LINKS.finditer(t)),
    ),
    "fabricated citation links": (
        LINK_PIECES,
        lambda t: spans(FORMER_CITATION_LINK.finditer(t)),
        lambda t: spans(source_truth._CITATION_LINKS.finditer(t)),
    ),
    "web links": (
        LINK_PIECES,
        lambda t: spans(FORMER_WEB_LINK.finditer(t)),
        lambda t: spans(source_truth._WEB_LINKS.finditer(t)),
    ),
    "retrieval source links": (
        LINK_PIECES,
        lambda t: FORMER_SOURCE_LINK.sub("", t),
        lambda t: chat_pipeline._SOURCE_LINKS.sub("", t),
    ),
    "held hand": (
        [
            " holds ",
            "'s ",
            "own ",
            "hand",
            " with his hand",
            "a",
            "B",
            " ",
            "\n",
            "'s",
            "s",
            "Ann holds Bo's hand",
            "'s own hand",
            " holds Bo",
        ],
        lambda t: groups(re.fullmatch(FORMER_HOLDS, t)),
        lambda t: groups(image_verification.HELD_HAND.fullmatch(t)),
    ),
    "secret masking": (
        [
            "password",
            "TOKEN",
            "api-key",
            "api_key",
            "x",
            "-",
            "_",
            "=",
            ":",
            " ",
            "'",
            '"',
            ",",
            ";",
            "v",
            "é",
            "\n",
        ],
        lambda t: re.sub(FORMER_SECRET, r"\1[REDACTED]", t),
        lambda t: code_workspace.SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", t),
    ),
    "python heredoc": (
        [
            "python3 - <<'EOF'\n",
            "python <<EOF",
            "python3.12 -",
            " <<",
            "<<",
            "'",
            '"',
            "EOF",
            "E",
            "\n",
            "\nEOF",
            "\nEOF\n",
            " ",
            "\t",
            "x",
            "print(1)",
            "<",
            "py",
        ],
        lambda t: (lambda m: m and m.groups())(FORMER_HEREDOC.fullmatch(t)),
        code_verification.heredoc,
    ),
    "unit cues": (
        [
            "1",
            "12",
            ".",
            ",",
            " ",
            "km",
            "m",
            "en",
            "in",
            "miles",
            "combien",
            "=",
            "a",
            "convert",
            "°c",
            "how many",
            "feet",
        ],
        lambda t: bool(FORMER_UNIT_CUES.search(t)),
        lambda t: bool(exact_tools._UNIT_CUES.search(t)),
    ),
    "place joiner": (
        [" ", "\n", "\t", "à", "a", "au", "in", "at", "a la", "in der", "x", "Caen"],
        lambda t: FORMER_PLACE_JOINER.sub(", ", t),
        lambda t: nearby.PLACE_JOINER.sub(", ", t),
    ),
    "explicit memory": (
        [
            "remember",
            "remember that",
            "retiens",
            "merke dir",
            ", dass",
            " ",
            "\n",
            ":",
            "：",
            "x",
            "fact",
            "\t",
        ],
        lambda t: groups(re.fullmatch(FORMER_EXPLICIT, t, re.I | re.S)),
        lambda t: groups(project_memory.EXPLICIT.fullmatch(t)),
    ),
    "amounts": (
        ["1", "23", " ", ".", ",", " ", "\n", "€", "$", "EUR", "usd", "x", "EURO"],
        lambda t: spans(shopping_prices.MONEY.finditer(t)),
        lambda t: spans(shopping_prices.MONEYS.finditer(t)),
    ),
}

# Long inputs that made the former forms backtrack (audit of 2026-11-15).
ADVERSARIAL: dict[str, list[str]] = {
    "style url": [
        "url(" + " " * 100_000,
        "url(" * 25_000,
        "url('" + " " * 100_000 + "x",
    ],
    "style import url": ["@import url(" + "\t" * 100_000, "@import url(" * 8_000],
    "file links": ["[a](" * 25_000 + " )", "[a ](" * 20_000 + "\n)"],
    "person citation links": ["[a](" * 25_000 + "\n)", "[a ](" * 20_000 + "\n)"],
    "fabricated citation links": [
        "[a](http://x" * 8_000 + " )",
        "[a](https://" * 8_000,
    ],
    "web links": ["[a](http://x" * 8_000 + " )", "[a](x" * 20_000 + ")"],
    "retrieval source links": [
        "[a](" * 25_000 + ")",
        "[a](" * 25_000 + "/sources/" + "g" * 64 + ")",
    ],
    "held hand": [" holds " * 14_000, "a holds " * 12_000 + "b's hand x"],
    "secret masking": ["p-" * 50_000, "token-" * 16_000, "password" * 12_000 + " ="],
    "python heredoc": [
        "python3 -\t<< a1" + "\n" * 100_000 + "x",
        "python3 - <<'EOF'\n" + "print(1)\n" * 12_000 + "EOF\n",
        "python3 - <<'EOF'\n" + "\n" * 100_000,
    ],
    "unit cues": ["1" * 100_000 + "x", "1." * 50_000, "1 " * 50_000],
    "place joiner": [" \n" * 50_000 + "x", "\n " * 50_000 + "at"],
    "explicit memory": [
        "merke dir, dass" + " " * 100_000,
        "remember " + " x" * 50_000 + " " * 1000,
    ],
    "amounts": ["1 " * 50_000 + "x", "1" * 100_000, "1." * 50_000 + "€"],
}


def former_search(pattern: str, flags: int = 0) -> Callable[[str], object]:
    compiled = re.compile(pattern, flags)
    return lambda t: groups(compiled.search(t))


def former_sub(
    pattern: str, replacement: str = "#", flags: int = 0
) -> Callable[[str], object]:
    compiled = re.compile(pattern, flags)
    return lambda t: compiled.sub(replacement, t)


def former_bool(pattern: str, flags: int = 0) -> Callable[[str], object]:
    compiled = re.compile(pattern, flags)
    return lambda t: compiled.search(t) is not None


def former_drawing_subject(text: str) -> str:
    match = re.search(
        r"<!--\s*commons-query:\s*([A-Za-z0-9 -]{2,80})\s*-->", text, re.I
    )
    return " ".join(match.group(1).split()) if match else ""


def former_payload(text: str) -> str | None:
    match = re.search(r"\b(?:pour|for|fur|para|per)\s*:?\s+(.+)$", text, re.I)
    return match[1] if match else None


def scan_groups(scan: linear_scan.Scan) -> Callable[[str], object]:
    return lambda t: spans(scan.finditer(t))


def regex_groups(pattern: str, flags: int = 0) -> Callable[[str], object]:
    compiled = re.compile(pattern, flags)
    return lambda t: spans(compiled.finditer(t))


def labelled(match: re.Match[str]) -> str:
    return "<" + "|".join(str(g) for g in match.groups()) + ">"


FORMER_DATE_CUES = re.compile(
    exact_tools._DATE_CUES.pattern.replace(
        r"\s*+(?:(?:h|am|pm)\s*+)?(?:cet", r"\s*(?:h|am|pm)?\s*(?:cet"
    )
)
FORMER_INLINE = re.compile(
    r"\\(?P<escaped>[\\`*\[\]])"
    r"|`(?P<code>[^`]*[^`\s][^`]*)`"
    r"|(?<![\w\]])!?\[(?P<label>[^\[\]]*[^\[\]\s][^\[\]]*)\]\((?P<url>"
    r"(?:https?://|mailto:)[^\s()]*(?:\([^\s()]*\)[^\s()]*)*"
    r")\)"
    r"|(?<![\w*])(?P<stars>\*{1,3})(?![\s*])(?P<inner>.{1,400}?)(?<![\s*])"
    r"(?P=stars)(?![\w*])"
)
TAG_PIECES = [
    "<",
    ">",
    "/",
    " ",
    "\n",
    "a",
    "x",
    "=",
    '"',
    "'",
    "?",
    "!",
    "-",
    "[",
    "]",
]
SVG_PIECES = TAG_PIECES + [
    "<svg",
    "<SVG ",
    "</svg>",
    "</svg >",
    "<svg>",
    "<g",
    "<g/>",
    "<!--",
    "-->",
    "{",
    "}",
    "<title>",
    "</title>",
    "<html>",
    "</html>",
    "<!doctype html>",
    "<?xml",
    "?>",
    "<!DOCTYPE",
]
WORD_PIECES = [
    " ",
    "\n",
    "x",
    "ne",
    "ne ",
    "pas",
    "not",
    "show",
    "image",
    "draw",
    "don't",
    "what does ",
    " look like",
    "how is ",
    " built",
    "teile",
    "drehen",
    "kein",
    "no ",
    "mostra",
    "sans image",
    "dessine",
    "do not ",
    "nicht ",
    "wie sieht ",
    " aus",
    "pezzi",
    "girano",
    "physically ",
]
FILE_PIECES = [
    " ",
    "\n",
    "x",
    "i'll",
    "je vais",
    "file",
    "pdf",
    "fichier",
    "generated",
    "disponible",
    "available",
    "document",
]
BLOCK_PIECES = [
    "<attached_files>",
    "</attached_files>",
    "<ATTACHED_FILES>",
    "</Attached_Files>",
    "[attachment: ",
    "[Attachment: ",
    "]",
    "\n",
    "x",
    " ",
    "<file type='filesystem'>",
    "<file ",
    'type="filesystem"',
    ">",
]

DIFFERENTIAL.update(
    {
        "svg external url": (
            ["url(", "URL(", " ", "\t", "'", '"', "#", "a", ")", "(", "#x", "\n"],
            former_sub(r"url\(\s*(['\"]?)\s*(?!#)[^)]*\)", "none", re.I),
            lambda t: sanitise._EXTERNAL_URL.sub("none", t),
        ),
        "svg doctype": (
            ["<!DOCTYPE", "<!doctype", " ", "[", ">", "a", "<", "]"],
            former_sub(r"<!DOCTYPE[^>\[]*>", "#", re.I),
            lambda t: sanitise._DOCTYPE.sub("#", t),
        ),
        "svg opening": (
            SVG_PIECES,
            former_search(r"<svg\b[^>]*>"),
            lambda t: groups(sanitise._SVG_OPENING.search(t)),
        ),
        "xml preamble": (
            [
                "<?xml",
                "<?XML",
                "?",
                ">",
                "<!DOCTYPE",
                "<!doctype ",
                "[",
                "]",
                " ",
                "a",
                "?>",
            ],
            former_sub(
                r"<\?xml\b[^>]*\?>|<!DOCTYPE\b(?:[^>\[]|\[[\s\S]*?\])*>", "", re.I
            ),
            appearance.strip_xml_preamble,
        ),
        "commons query": (
            [
                "<!--",
                " commons-query:",
                "commons-query:",
                " ",
                "  ",
                "\t",
                "\n",
                "a",
                "Bo",
                "-",
                "-->",
                ">",
                "x9",
                "<",
            ],
            former_drawing_subject,
            appearance.drawing_subject,
        ),
        "appearance cues": (
            WORD_PIECES,
            former_bool(
                r"\b(?:what does .+ look like|how is .+ (?:physically )?built|exploded view|"
                r"cutaway view|parts of|a quoi ca ressemble|a quoi ressemble(?:nt)?|les pieces|ce qui tourne|"
                r"vue eclatee|wie sieht .+ aus|teile von|was dreht sich|teile.*drehen|explosions(?:ansicht|zeichnung)|"
                r"como es por dentro|piezas de|que gira|vista explosionada|com[' ]e fatto|"
                r"pezzi di|cosa gira|pezzi.*girano|vista esplosa)\b",
                re.I,
            ),
            appearance._appearance,
        ),
        "no visual": (
            WORD_PIECES,
            former_bool(
                r"\b(?:ne\b.*\bpas|sans image|not\b.*\b(?:show|image|draw)|"
                r"don't\b.*\b(?:show|draw)|no image|nicht|kein\w*|no\s+\w*mostr\w*|"
                r"non\s+\w*mostr\w*)\b",
                re.I,
            ),
            appearance._no_visual,
        ),
        "spatial refusal": (
            WORD_PIECES,
            former_bool(
                r"\b(?:do not|don't|no|without|sans|pas de|ne .*pas|nicht|kein\w*|sin|senza)\b"
            ),
            lambda t: bool(
                appearance._SPATIAL_REFUSAL.search(t)
                or appearance._SPATIAL_NE_PAS.search(t)
            ),
        ),
        "not draw": (
            WORD_PIECES,
            former_bool(
                r"\b(?:do not|don't|ne .*pas|nicht|non)\s+(?:draw|dessine|zeichne|disegna)\b"
            ),
            lambda t: bool(
                diagram._NOT_DRAW.search(t) or diagram._NE_PAS_DRAW.search(t)
            ),
        ),
        "svg elements": (
            SVG_PIECES,
            regex_groups(r"<svg\b[^>]*>.*?</svg\s*>", re.I | re.S),
            scan_groups(appearance.SVG_ELEMENTS),
        ),
        "first numbered line": (
            [" ", "\n", "\t", "1", "12", ".", ")", "x", "1. ", "\n2) "],
            former_search(r"^\s*\d+[.)]\s+", re.M),
            lambda t: groups(appearance._FIRST_ITEM.search(t)),
        ),
        "svg tags": (
            TAG_PIECES + ["</x--", "é", "<g", "<svg", "/>"],
            regex_groups(
                r"<(?P<closing>/)?(?P<name>[A-Za-z_:][\w:.-]*)(?P<body>[^<>]*)>"
            ),
            regex_groups(
                r"<(?P<closing>/)?(?P<name>[A-Za-z_:][\w:.-]*+)(?P<body>[^<>]*+)>"
            ),
        ),
        "svg attributes": (
            [" ", "\t", "\n", "a", "b:c", "=", '"', "'", "x", '"v"', "'w'", "-"],
            regex_groups(r"\s+([A-Za-z_:][\w:.-]*)\s*=\s*(\"[^\"]*\"|'[^']*')"),
            regex_groups(
                r"(?<!\s)\s++([A-Za-z_:][\w:.-]*+)\s*+=\s*+(\"[^\"]*+\"|'[^']*+')"
            ),
        ),
        "attached blocks": (
            BLOCK_PIECES,
            former_sub(r"<attached_files>.*?</attached_files>", "#", re.S),
            lambda t: attached_blocks.BLOCKS.sub("#", t),
        ),
        "attached blocks any case": (
            BLOCK_PIECES,
            former_sub(r"<attached_files>.*?</attached_files>", "#", re.S | re.I),
            lambda t: attached_blocks.BLOCKS_ANY_CASE.sub("#", t),
        ),
        "attached contents": (
            BLOCK_PIECES,
            lambda t: re.findall(r"<attached_files>(.*?)</attached_files>", t, re.S),
            attached_blocks.CONTENTS.findall,
        ),
        "attachment tags": (
            BLOCK_PIECES,
            former_sub(
                r"\[attachment: [^\n]*\]|<attached_files>.*?</attached_files>",
                "#",
                re.S | re.I,
            ),
            lambda t: attached_blocks.TAGS.sub("#", t),
        ),
        "filesystem tag": (
            BLOCK_PIECES,
            former_search(r'<file\s[^>]*type=[\'"]filesystem[\'"]'),
            lambda t: groups(chat_schema._FILESYSTEM_TAG.search(t)),
        ),
        "file promises": (
            FILE_PIECES,
            former_bool(
                r"(?:je vais|je publie|i will|i'll|ich werde|voy a|procedero).*?(?:fichier|file|document|pptx|pdf|datei)"
            ),
            lambda t: file_content._PROMISED_FILE.search(t) is not None,
        ),
        "file announcements": (
            FILE_PIECES,
            former_bool(
                r"(?:fichier|file|document|pptx|pdf|datei).*(?:genere|generated|cree|created|publie|published|disponible|available)"
            ),
            lambda t: file_content._ANNOUNCED_FILE.search(t) is not None,
        ),
        "birth attributes": (
            ["holding", "Holding ", "baby", "newborn", "calm", "x", " ", "\n", "hold"],
            former_bool(
                r"pregnan|swaddl|clean|serene|calm|holding.*(?:baby|newborn)", re.I
            ),
            lambda t: bool(
                image_scene._BIRTH_ATTRIBUTE.search(t)
                or image_scene._HOLDING_BABY.search(t)
            ),
        ),
        "held baby": (
            [
                "holds",
                "holding",
                "cradl",
                "swaddl",
                "baby",
                "Jesus",
                "x",
                " ",
                "\n",
                "hold",
            ],
            former_bool(
                r"(?:holds|holding|cradl|swaddl).*(?:baby|newborn|Jesus)", re.I
            ),
            lambda t: image_scene._HELD_BABY.search(t) is not None,
        ),
        "qr quotes": (
            ['"', "“", "”", "a", " ", "\n", "x"],
            lambda t: re.findall(r'["“]([^"”]+)["”]', t),
            capability_strategy._QUOTED.findall,
        ),
        "qr payload": (
            [
                "for",
                "pour",
                "Para",
                "per",
                " ",
                "  ",
                "\n",
                ":",
                "\t",
                "x",
                "y",
                "format",
                "fur",
            ],
            former_payload,
            capability_strategy.precise_payload,
        ),
        "markdown image line": (
            ["![", "](", "]", "(", "a", " ", "!", "["],
            former_bool(r"!\[.*?\]\("),
            lambda t: re.match(r"(?>.*?!\[).*?\]\(", t) is not None,
        ),
        "redirect suffix": (
            [" ", "\t", "\n", "2>&1", "x", "2>", "&1"],
            former_sub(r"\s+2>&1\s*$", ""),
            lambda t: re.sub(r"(?<!\s)\s++2>&1\s*$", "", t),
        ),
        "prompt tail": (
            [" ", ".", "\n", "x", ". ", ".."],
            former_sub(r"[\s.]+$", ""),
            lambda t: re.sub(r"(?<![\s.])[\s.]+$", "", t),
        ),
        "space before punctuation": (
            [" ", "\t", "\n", ",", ".", ";", ":", "x"],
            former_sub(r"\s+([,.;:])", r"\1"),
            lambda t: re.sub(r"(?<!\s)\s++([,.;:])", r"\1", t),
        ),
        "tab before punctuation": (
            [" ", "\t", "\n", ",", ".", ";", "x"],
            former_sub(r"[ \t]+([.,;])", r"\1"),
            lambda t: re.sub(r"(?<![ \t])[ \t]++([.,;])", r"\1", t),
        ),
        "python lines": (
            [
                " ",
                "\n",
                "\t",
                "print(",
                "import os",
                "def f(",
                "from x import ",
                "x",
                "doc.add_",
            ],
            former_search(
                r"(?m)^\s*(?:from \w+ import |import \w+|def \w+\(|doc\.add_|print\()"
            ),
            lambda t: groups(answer_content._CODE_LINES.search(t)),
        ),
        "mermaid nodes": (
            ["a", "-", "_", "Z", "1", "[", "(", "{", "[[", "((", " ", "\n", "é"],
            regex_groups(
                r"\b[A-Za-z_][\w-]*(?P<open>\[\[|\[\(|\(\[|\(\(|\{\{|\[|\(|\{)"
            ),
            scan_groups(mermaid_check._NODE),
        ),
        "mermaid subgraphs": (
            [
                " ",
                "\n",
                "\t",
                "subgraph ",
                "subgraph",
                "a",
                "b-c",
                "[",
                "]",
                "x",
                "\n  ",
            ],
            lambda t: re.compile(
                r"(?ms)^(?P<prefix>\s*subgraph\s+[A-Za-z_][\w-]*\s*)\[(?P<label>.*?)\](?=\s*$)"
            ).sub(labelled, t),
            lambda t: mermaid_check._SUBGRAPHS.sub(labelled, t),
        ),
        "raw html": (
            SVG_PIECES,
            regex_groups(r"(?is)(<!doctype html\b.*?</html\s*>|<html\b.*?</html\s*>)"),
            scan_groups(artifact_source._RAW_HTML),
        ),
        "raw svg": (
            SVG_PIECES,
            regex_groups(r"(?is)<svg\b.*?</svg\s*>"),
            scan_groups(artifact_source._RAW_SVG),
        ),
        "title": (
            SVG_PIECES,
            regex_groups(r"(?is)<title[^>]*>(.*?)</title\s*>"),
            scan_groups(artifact_source._TITLE),
        ),
        "tags": (
            TAG_PIECES,
            former_sub(r"<[^>]*>"),
            lambda t: linear_scan.TAGS.sub("#", t),
        ),
        "answer svg": (
            SVG_PIECES,
            former_search(r"<svg\b.*</svg>", re.S),
            lambda t: groups(svg_text._SVG.search(t)),
        ),
        "answer json": (
            SVG_PIECES,
            former_search(r"\{.*\}", re.S),
            lambda t: groups(svg_text._JSON_OBJECT.search(t)),
        ),
        "svg comments": (
            SVG_PIECES,
            former_sub(r"<!--.*?-->", "#", re.S),
            lambda t: svg_text._COMMENTS.sub("#", t),
        ),
        "open groups": (
            SVG_PIECES,
            lambda t: len(re.findall(r"<g\b[^>]*?(?<!/)>", t)),
            lambda t: len(list(svg_text._CUT_OPENINGS["g"].finditer(t))),
        ),
        "citation runs": (
            [
                "[[atlas-reference:",
                "]]",
                "]",
                "x",
                "1",
                ",",
                "; ",
                ".example",
                "/p",
                " ",
                "e.test",
            ],
            regex_groups(citation_fragments._CITATION_RUN.pattern),
            scan_groups(citation_fragments._CITATION_RUNS),
        ),
        "reference runs": (
            ["[[atlas-reference:", "]]", "]", "x", "1", ",", "; ", " ", "\t"],
            regex_groups(source_truth._REFERENCE_RUNS.pattern.pattern),
            scan_groups(source_truth._REFERENCE_RUNS),
        ),
        "data images": (
            ["data:image/", "png", ";base64", ",", "QUJD", "=", " ", "\n", "x", "+/"],
            former_sub(r"data:image/[^,\s]+,[A-Za-z0-9+/=]+", "[IMAGE]"),
            lambda t: linear_scan.DATA_IMAGES.sub("[IMAGE]", t),
        ),
        "generated image links": (
            LINK_PIECES + ["/images/", "/x/images/a", "![x](/images/b)"],
            former_search(r"!\[[^\]]*\]\([^)]*/images/[^)]+\)", re.I),
            lambda t: groups(followup._IMAGE_CONTEXT.search(t)),
        ),
        "image targets": (
            LINK_PIECES,
            lambda t: re.findall(r"!\[[^\]]*\]\(([^)]+)\)", t),
            followup._IMAGE_TARGETS.findall,
        ),
        "picture marks": (
            LINK_PIECES + ["<svg", "<SVG>", "<svgx"],
            former_search(r"<svg\b|!\[[^\]]*\]\(", re.I),
            lambda t: groups(request_router._PICTURE.search(t)),
        ),
        "summary images": (
            LINK_PIECES,
            former_sub(r"!\[[^\]]*\]\([^)]*\)"),
            lambda t: artifact_tools._IMAGES.sub("#", t),
        ),
        "cd chain": (
            [" ", "\t", "\n", "cd", "cd ", "&&", "&", "x", "a b"],
            lambda t: re.match(r"\s*cd\s+.+&&", t) is not None,
            lambda t: work_remote_tools._CD_CHAIN.match(t) is not None,
        ),
        "emphasis": (
            ["*", "_", "`", "<b", "<b>", "</strong>", "<h1 x>", "<", ">", "x", " "],
            former_sub(r"[*_`]|</?(?:strong|b|h[1-6])[^>]*>"),
            lambda t: citation_cleanup._EMPHASIS.sub("#", t),
        ),
        "quotes": (
            ["`", '"', "«", "»", "x", " ", "\n"],
            former_sub(r'`[^`]*`|"[^"]*"|«[^»]*»'),
            lambda t: language._QUOTED.sub("#", t),
        ),
        "table rules": (
            ["|", "---", "-", ":", " ", "\n", "\t", "x", "|---|", ":---:", "  \n"],
            former_sub(
                r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", "#", re.M
            ),
            lambda t: tts.TABLE_RULES.sub("#", t),
        ),
        "commas": (
            [" ", "\n", ",", "1", "x", ", ", " ,"],
            former_sub(r"(?<!\d)\s*,\s*|\s*,\s*(?!\d)", ", "),
            lambda t: tts.COMMAS.sub(", ", t),
        ),
        "spoken links": (
            LINK_PIECES,
            former_sub(r"!?(\[([^\]]*)\]\([^)]*\))", r"\2"),
            lambda t: tts.LINKS.sub(r"\2", t),
        ),
        "document inline": (
            [
                "\\",
                "`",
                "*",
                "**",
                "[",
                "]",
                "(",
                ")",
                "!",
                " ",
                "a",
                "http://x",
                "mailto:a",
                "(b)",
                "\n",
                "_",
            ],
            lambda t: (
                spans(FORMER_INLINE.finditer(t)),
                groups(FORMER_INLINE.fullmatch(t)),
            ),
            lambda t: (
                spans(cells.INLINE.finditer(t)),
                groups(cells.INLINE.fullmatch(t)),
            ),
        ),
        "project references": (
            [
                "[",
                "]",
                "(",
                ")",
                HASH,
                "a",
                " ",
                "\n",
                "](",
                "](https://x)",
                "/api/atlas/orgs/o/sources/",
                "[" + HASH + "]",
                "(" + HASH + ")",
                "p",
            ],
            lambda t: project_citations.REFERENCE.sub(labelled, t),
            lambda t: project_citations.REFERENCES.sub(labelled, t),
        ),
        "date cues": (
            [
                "1",
                "12",
                ":30",
                " ",
                "\n",
                "h",
                "am",
                "pm",
                "cet",
                "est",
                "x",
                "utc",
                "in",
                "3 days",
            ],
            lambda t: FORMER_DATE_CUES.search(t) is not None,
            lambda t: exact_tools._DATE_CUES.search(t) is not None,
        ),
        "json fences": (
            ["```", "```json", " ", "\n", "x", "{}", "\t"],
            former_sub(r"^```(?:json)?\s*|\s*```$", ""),
            lambda t: re.sub(r"^```(?:json)?\s*|(?:(?<!\s)\s+)?```$", "", t),
        ),
    }
)

DIFFERENTIAL.update(
    {
        "stream url tail": (
            ["h", "ht", "http", "https:", "//x", " ", ")", "]", ">", "-", "a", "\n"],
            former_search(r"(?<!\w)h(?:t(?:t(?:ps?(?::[^\s)\]>]*)?)?)?)?$"),
            lambda t: groups(
                chat_stream._TRAILING_URL.search(t, chat_stream._after_last_stop(t))
            ),
        ),
        "stream link tail": (
            LINK_PIECES,
            former_search(r"\[[^\]]*$|\[[^\]]*\](?:\([^)]*)?$"),
            lambda t: groups(chat_stream._TRAILING_LINK.search(t)),
        ),
        "stream image tail": (
            LINK_PIECES + ["!"],
            former_search(r"!$|!\[[^\]]*$|!\[[^\]]*\](?:\([^)]*|\[[^\]]*)?$"),
            lambda t: groups(chat_stream._TRAILING_IMAGE.search(t)),
        ),
        "cut citation": (
            LINK_PIECES,
            former_bool(r"\[[^\]]*$|\[[^\]]*\]\([^)]*$"),
            lambda t: chat_schema._CUT_CITATION.search(t) is not None,
        ),
        "svg transforms": (
            ["matrix", "translate", "skewX", " ", "(", ")", "1", ",", "x", "scale ("],
            lambda t: re.findall(
                r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)", t
            ),
            lambda t: [m.groups() for m in svg_doc._TRANSFORMS.finditer(t)],
        ),
        "private keys": (
            [
                "-----BEGIN ",
                "PRIVATE KEY-----",
                "-----END ",
                "RSA ",
                "-",
                "x",
                "\n",
                " ",
                "-----BEGIN PRIVATE KEY-----",
                "-----END PRIVATE KEY-----",
            ],
            former_sub(
                r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
                "#",
                re.S,
            ),
            lambda t: code_workspace.PRIVATE_KEYS.sub("#", t),
        ),
        "delivered files": (
            [
                "http://",
                "https://x",
                ".pdf",
                ".md",
                ".docx)",
                "/deliverables/",
                " ",
                "\n",
                "a",
                ".mdx",
                "/",
                ".",
            ],
            former_search(
                r"/deliverables/|https?://\S+\.(docx|xlsx|pptx|pdf|csv|md)\b|\.(docx|xlsx|pptx|pdf|csv)\)",
                re.I,
            ),
            lambda t: groups(request_router._FILE.search(t)),
        ),
        "failed publication": (
            ["non", "pubblic", "nicht", "x", " ", "\n", "nessun", "failed"],
            former_bool(
                r"pas pu|impossible|echou|could not|cannot|failed|unable|keine|nicht|no se pudo|ningun|non.*pubblic|nessun"
            ),
            lambda t: bool(
                file_content._FAILED.search(t) or file_content._NOT_PUBLISHED.search(t)
            ),
        ),
        "prompt summary": (
            ["Subjects:", "Attributes:", "Scene:", "Style:", " ", "\n", "x", "y"],
            lambda t: re.fullmatch(
                r"\s*Subjects:\s*\S.*?Attributes:\s*\S.*?Scene:\s*\S.*?Style:\s*\S.*",
                t,
                re.S,
            )
            is not None,
            lambda t: re.fullmatch(
                r"\s*Subjects:\s*\S(?>.*?Attributes:)\s*\S(?>.*?Scene:)\s*\S(?>.*?Style:)\s*\S.*",
                t,
                re.S,
            )
            is not None,
        ),
        "quoted words": (
            [
                "reading ",
                "the words ",
                "with ",
                "“",
                "”",
                '"',
                "«",
                "»",
                "x",
                " ",
                "titled",
            ],
            former_sub(rewrite._INTRO + r"[«“\"][^»”\"]*[»”\"]"),
            lambda t: rewrite._QUOTED.sub("#", t),
        ),
    }
)
ADVERSARIAL.update(
    {
        "stream url tail": ["-https:" * 14_000, "https://" + "a" * 100_000],
        "stream link tail": ["[a](" * 20_000 + "x)", "[" * 100_000 + "]"],
        "stream image tail": ["![](" * 20_000 + "x)", "!x" * 50_000 + "]"],
        "cut citation": ["[a](x)" * 15_000, "[a" * 50_000 + "]"],
        "svg transforms": ["skewX(" * 15_000],
        "private keys": ["-----BEGIN PRIVATE KEY-----" * 3_000, "-----BEGIN " * 9_000],
        "delivered files": ["http://" * 14_000, "https://a.pdf" * 7_000 + " "],
        "failed publication": ["non " * 25_000],
        "prompt summary": ["Subjects: x Attributes: " * 4_000],
        "quoted words": ["“" * 100_000, "reading the words “" * 5_000],
    }
)
ADVERSARIAL.update(
    {
        "svg external url": [
            "url(" + " " * 100_000,
            "url(" * 25_000,
            "url(#" * 20_000 + ")",
        ],
        "svg doctype": ["<!DOCTYPE" * 11_000, "<!DOCTYPE x" * 9_000 + "["],
        "svg opening": ["<svg " * 20_000],
        "xml preamble": [
            "<!DOCTYPE [" * 9_000 + ">",
            "<!DOCTYPE [a]" * 7_000,
            "<?xml" * 20_000,
            "<!DOCTYPE[[]" * 8_000,
        ],
        "commons query": [
            "<!--\t\tcommons-query:\n\n" + " " * 100_000 + "x",
            "<!-- commons-query:" * 5_000 + "-->",
        ],
        "appearance cues": [
            "what does " * 10_000,
            "how is " * 14_000 + "x",
            "teile " * 16_000,
        ],
        "no visual": ["ne " * 30_000, "e n" * 30_000, "not " * 25_000],
        "spatial refusal": ["ne " * 30_000],
        "not draw": ["ne " * 30_000 + "pas"],
        "svg elements": ["<svg" * 25_000, "<svg>" * 20_000],
        "first numbered line": ["\n" * 100_000, " \n" * 50_000 + "x"],
        "svg tags": ["</x--" + "é" * 100_000, "<a" * 50_000],
        "svg attributes": ["\t" * 100_000 + "bx", " a" * 50_000],
        "attached blocks": ["<attached_files> " * 6_000],
        "attachment tags": ["[attachment: " * 8_000, "<attached_files>" * 6_000],
        "attached contents": ["<attached_files>" * 6_000],
        "filesystem tag": ["<file " * 20_000],
        "file promises": ["i'll " * 20_000],
        "file announcements": ["pdf " * 25_000],
        "birth attributes": ["holding " * 12_000],
        "held baby": ["holds " * 16_000],
        "qr quotes": ["“" * 100_000],
        "qr payload": ["pour\n" + "\n" * 100_000, "for " * 25_000 + "\nx"],
        "markdown image line": ["![" * 50_000],
        "redirect suffix": ["\t\t2>&1" + " " * 100_000 + "x"],
        "prompt tail": [". " * 50_000 + "x"],
        "space before punctuation": ["\n " * 50_000 + "x"],
        "tab before punctuation": [" \t" * 50_000 + "x"],
        "python lines": ["print(" + "\n" * 100_000 + "x"],
        "mermaid nodes": ["-Z" * 50_000, "a-" * 50_000],
        "mermaid subgraphs": [
            "\tsubgraph ax" + "\n" * 100_000,
            "subgraph a [\n" * 8_000,
        ],
        "raw html": ["<html>" * 16_000],
        "raw svg": ["<svg\n" * 20_000],
        "title": ["<title" * 16_000, "<title>" * 14_000],
        "tags": ["<" * 100_000],
        "answer svg": ["<svg " * 20_000],
        "answer json": ["{" * 100_000],
        "svg comments": ["<!--\n" * 20_000],
        "open groups": ["g<" * 50_000, "<g/" * 30_000],
        "citation runs": ["[[atlas-reference: " * 5_000],
        "reference runs": ["[[atlas-reference:" * 5_000],
        "data images": ["data:image/" * 9_000],
        "generated image links": ["![" * 50_000, "![a](" * 20_000 + ")"],
        "image targets": ["![" * 50_000, "![a](" * 20_000 + "\n"],
        "picture marks": ["[!" * 50_000],
        "summary images": ["![" * 50_000, "![a](" * 20_000],
        "cd chain": ["\tcd\t" + " " * 100_000],
        "emphasis": ["<b" * 50_000],
        "quotes": ["«" * 100_000, '"' + "`" * 50_000],
        "table rules": [" |" + "\n" * 12_000, "\n" * 12_000 + "|---" * 100],
        "commas": ["\n" * 12_000 + "x"],
        "spoken links": ["![" * 6_000, "[a](" * 3_000],
        "document inline": [
            "![" + "*" * 100_000,
            "`" + " " * 100_000,
            "[" + "a " * 50_000,
        ],
        "project references": ["[p" * 50_000 + HASH, "[" + HASH + "](" * 2_000],
        "date cues": ["1" + "\n" * 4_000],
        "json fences": ["x" + " " * 100_000 + "y"],
    }
)


# What production runs at each site (the cost check calls these).
PRODUCTION: dict[str, Callable[[str], object]] = {
    "style url": lambda t: list(artifact_html.STYLE_URLS.finditer(t)),
    "style import url": lambda t: list(artifact_html.STYLE_IMPORT_URLS.finditer(t)),
    "file links": lambda t: file_results.canonical_links(t, ["/files/report.pdf"]),
    "person citation links": lambda t: source_truth.person_citations(
        t, {"https://example.test/page"}
    ),
    "fabricated citation links": lambda t: source_truth.without_unretrieved(
        t, {"https://example.test/page"}
    ),
    "web links": lambda t: source_truth.inline_source_markers(
        t, ["https://example.test/page"]
    ),
    "retrieval source links": lambda t: chat_pipeline._SOURCE_LINKS.sub("", t),
    "held hand": lambda t: image_verification.verification_note(
        {"failed": ["hand"], "failed_details": [t]}, "fr"
    ),
    "secret masking": code_workspace.redact,
    "python heredoc": code_verification.inline_command,
    "unit cues": exact_tools.relevant,
    "place joiner": lambda t: nearby.PLACE_JOINER.sub(", ", t),
    "explicit memory": project_memory.explicit,
    "amounts": lambda t: shopping_prices.verified_prices(
        t, {"https://e.test/p": t}, "en"
    ),
    "svg external url": lambda t: sanitise._clean_css(t),
    "appearance cues": appearance.is_appearance_request,
    "no visual": appearance.is_appearance_request,
    "spatial refusal": appearance.is_spatial_drawing_request,
    "not draw": diagram.requested,
    "first numbered line": appearance.mechanical_prose,
    "attached blocks": diagram.requested,
    "prompt tail": rewrite.enforce,
    "json fences": image_prompt.candidates,
    "table rules": lambda t: tts.spoken_text(t[: tts.MAX_INPUT_CHARS], "en"),
    "commas": lambda t: tts.spoken_text(t[: tts.MAX_INPUT_CHARS], "en"),
    "spoken links": lambda t: tts.spoken_text(t[: tts.MAX_INPUT_CHARS], "en"),
    "date cues": exact_tools.relevant,
    "delivered files": request_router._delivered,
}

# Linear work on these inputs takes milliseconds; the budget leaves room for a
# slow CI machine. The former forms took seconds to hours on the same inputs.
BUDGET_SECONDS = 1.0
CASES = 3_000


class InputRegexCostTests(unittest.TestCase):
    def test_rewritten_forms_return_the_former_results(self) -> None:
        rng = random.Random(20261115)
        for site, (pieces, former, rewritten) in DIFFERENTIAL.items():
            with self.subTest(site=site):
                for _ in range(CASES):
                    text = "".join(
                        rng.choice(pieces) for _ in range(rng.randint(0, 16))
                    )
                    self.assertEqual(rewritten(text), former(text), repr(text))

    def test_rewritten_forms_stay_within_budget_on_long_inputs(self) -> None:
        slow = []
        for site, texts in ADVERSARIAL.items():
            run = PRODUCTION.get(site, DIFFERENTIAL[site][2])
            for text in texts:
                start = time.perf_counter()
                run(text)
                seconds = time.perf_counter() - start
                if seconds > BUDGET_SECONDS:
                    slow.append(f"{site} on {text[:24]!r}...: {seconds:.2f} s")
        self.assertEqual(slow, [])


if __name__ == "__main__":
    unittest.main()
