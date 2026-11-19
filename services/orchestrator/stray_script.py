"""A stray character of another script glued to a word of a Latin-script answer.

An open model can emit one character of another writing system in the middle of
its prose: "documentee comme scientifiquement" followed by a Han character and
"active" in a French study (staging, 2026-11-19). The reader sees a defect the
model never meant; the sentence reads right without it.

The check is code, applied to the stream and to the stored answer alike. In an
answer whose language is written in the Latin script, a run of one or two
characters of another script (Han, kana, Hangul, Cyrillic, Arabic, Hebrew,
Indic, Thai...) is removed when it touches a Latin letter, with no space or
punctuation between them. A Cyrillic letter inside a Latin word that has a
Latin twin (a, e, o, p, c...) is replaced by that twin instead, so the word
stays whole. Kept as written:

- a longer run of that script (a name or a phrase written in it);
- text inside a code block or inline code, a link, quotation marks or
  parentheses, where a name in its original script belongs;
- a character standing alone between spaces ("the character ... means water");
- any script the person used in the conversation (a language lesson, a name
  typed in its own script);
- an answer in a language not written in the Latin script, and Greek letters,
  which Latin-script science uses (micro, alpha, delta).

Only counts are logged, never the text.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
import json
import logging

# Languages written in the Latin script (ISO 639-1, region suffix ignored).
LATIN_LANGUAGES = frozenset(
    {
        "af", "az", "bs", "ca", "cs", "cy", "da", "de", "en", "eo", "es", "et",
        "eu", "fi", "fil", "fr", "ga", "gl", "hr", "ht", "hu", "id", "is", "it",
        "la", "lb", "lt", "lv", "mg", "ms", "mt", "nb", "nl", "nn", "no", "oc",
        "pl", "pt", "ro", "sk", "sl", "so", "sq", "sv", "sw", "tl", "tr", "uz",
        "vi", "wa", "yo", "zu",
    }
)  # fmt: skip

# (first code point, last code point, script group), sorted by first code point.
_RANGES = (
    (0x0400, 0x052F, "cyrillic"),
    (0x0530, 0x058F, "armenian"),
    (0x0590, 0x05FF, "hebrew"),
    (0x0600, 0x06FF, "arabic"),
    (0x0750, 0x077F, "arabic"),
    (0x08A0, 0x08FF, "arabic"),
    (0x0900, 0x0DFF, "indic"),
    (0x0E00, 0x0EFF, "thai"),
    (0x1000, 0x109F, "myanmar"),
    (0x10A0, 0x10FF, "georgian"),
    (0x1100, 0x11FF, "cjk"),
    (0x1200, 0x137F, "ethiopic"),
    (0x1780, 0x17FF, "khmer"),
    (0x2DE0, 0x2DFF, "cyrillic"),
    (0x2E80, 0x2FDF, "cjk"),
    (0x3040, 0x30FF, "cjk"),
    (0x3130, 0x318F, "cjk"),
    (0x31F0, 0x31FF, "cjk"),
    (0x3400, 0x4DBF, "cjk"),
    (0x4E00, 0x9FFF, "cjk"),
    (0xA640, 0xA69F, "cyrillic"),
    (0xAC00, 0xD7AF, "cjk"),
    (0xF900, 0xFAFF, "cjk"),
    (0xFB1D, 0xFB4F, "hebrew"),
    (0xFB50, 0xFDFF, "arabic"),
    (0xFE70, 0xFEFF, "arabic"),
    (0x20000, 0x2FA1F, "cjk"),
)
_STARTS = [start for start, _, _ in _RANGES]

# Latin letters: basic, Latin-1, extended A and B, IPA, additional, extended C/D.
_LATIN = (
    (0x41, 0x5A),
    (0x61, 0x7A),
    (0xC0, 0xD6),
    (0xD8, 0xF6),
    (0xF8, 0x24F),
    (0x1E00, 0x1EFF),
    (0x2C60, 0x2C7F),
    (0xA720, 0xA7FF),
)

# Cyrillic letters drawn like a Latin letter: inside a Latin word, the twin.
LATIN_TWINS = dict(
    zip(
        "\u0430\u0435\u043e\u0440\u0441\u0443\u0445\u0456\u0458\u0455\u0501\u051b"
        "\u051d\u0410\u0412\u0415\u041a\u041c\u041d\u041e\u0420\u0421\u0422\u0425"
        "\u0406\u0408\u0405",
        "aeopcyxijsdqwABEKMHOPCTXIJS",
        strict=True,
    )
)

LONGEST_STRAY = 2
_QUOTE_OPEN = frozenset("\u201c\u00ab\u201e\u300c\u300e\u2039")
_QUOTE_CLOSE = frozenset("\u201d\u00bb\u300d\u300f\u203a")
_PAREN_OPEN = frozenset("([\uff08\uff3b")
_PAREN_CLOSE = frozenset(")]\uff09\uff3d")
_TOKEN_BREAK = frozenset("(<[")
_URL_STARTS = ("http://", "https://", "www.")
# The sources block appended to an answer lists titles as they were published.
_SOURCES_BLOCK = "<details"
_HEAD = len(_SOURCES_BLOCK)


def script(char: str) -> str | None:
    """The script group of a character this check reads, or None."""
    point = ord(char)
    if point < 0x0400:
        return None
    index = bisect_right(_STARTS, point) - 1
    if index < 0:
        return None
    start, end, group = _RANGES[index]
    return group if start <= point <= end else None


def latin_letter(char: str) -> bool:
    point = ord(char)
    return any(start <= point <= end for start, end in _LATIN)


def latin_language(language: str) -> bool:
    return language.lower().replace("_", "-").split("-", 1)[0] in LATIN_LANGUAGES


def scripts_in(texts: Iterable[str]) -> frozenset[str]:
    """The script groups the person used: kept wherever they appear."""
    found: set[str] = set()
    for text in texts:
        for char in text:
            if ord(char) >= 0x0400 and (group := script(char)) is not None:
                found.add(group)
    return frozenset(found)


class Rewriter:
    """One pass over an answer, chunk by chunk.

    Text goes out as it comes, except a run of another script, held until the
    character after it says whether it is glued to a Latin word.
    """

    def __init__(self, language: str, allowed: frozenset[str] = frozenset()) -> None:
        self.active = latin_language(language)
        self.allowed = allowed
        self.previous = ""
        self.run = ""
        self.run_protected = False
        self.ticks = 0
        self.fence = False
        self.inline = False
        self.quotes = 0
        self.straight = False
        self.parens = 0
        self.head = ""
        self.passthrough = False
        self.counts: Counter[str] = Counter()

    def _protected(self) -> bool:
        return (
            self.fence
            or self.inline
            or self.quotes > 0
            or self.straight
            or self.parens > 0
            or self.head.startswith(_URL_STARTS)
        )

    def _settle_ticks(self) -> None:
        if self.ticks >= 3:
            self.fence = not self.fence
            self.inline = False
        elif self.ticks and not self.fence:
            self.inline = not self.inline
        self.ticks = 0

    def _track(self, char: str) -> None:
        """The context after ``char``: code, quotation, parentheses, link."""
        if char == "`":
            self.ticks += 1
            return
        self._settle_ticks()
        if char == "\n":
            self.quotes = self.parens = 0
            self.straight = False
        elif char in _QUOTE_OPEN:
            self.quotes += 1
        elif char in _QUOTE_CLOSE:
            self.quotes = max(0, self.quotes - 1)
        elif char == '"':
            self.straight = not self.straight
        elif char in _PAREN_OPEN:
            self.parens += 1
        elif char in _PAREN_CLOSE:
            self.parens = max(0, self.parens - 1)
        if char.isspace() or char in _TOKEN_BREAK:
            # A tag starts its own token: "<details" is read from its "<".
            self.head = "<" if char == "<" else ""
        elif len(self.head) < _HEAD:
            self.head += char
            if self.head == _SOURCES_BLOCK:
                self.passthrough = True

    def _settle_run(self, following: str) -> str:
        run, self.run = self.run, ""
        before = latin_letter(self.previous) if self.previous else False
        after = latin_letter(following) if following else False
        groups = {script(char) for char in run}
        if (
            self.run_protected
            or len(run) > LONGEST_STRAY
            or not (before or after)
            or groups & self.allowed
        ):
            return run
        group = next(iter(groups)) or ""
        if len(run) == 1 and before and after and run in LATIN_TWINS:
            self.counts["replaced"] += 1
            self.counts[group] += 1
            return LATIN_TWINS[run]
        self.counts["removed"] += len(run)
        self.counts[group] += len(run)
        return ""

    def feed(self, text: str) -> str:
        if not self.active or self.passthrough:
            return text
        out: list[str] = []
        for index, char in enumerate(text):
            if self.passthrough:
                out.append(text[index:])
                break
            if script(char) is not None:
                if not self.run:
                    self._settle_ticks()
                    self.run_protected = self._protected()
                self.run += char
                continue
            if self.run:
                kept = self._settle_run(char)
                out.append(kept)
                if kept:
                    self.previous = kept[-1]
            out.append(char)
            self.previous = char
            self._track(char)
        return "".join(out)

    def finish(self) -> str:
        return self._settle_run("") if self.run else ""

    def report(self) -> dict[str, int]:
        return dict(self.counts)


def clean(
    answer: str, language: str, allowed: frozenset[str] = frozenset()
) -> tuple[str, dict[str, int]]:
    """The complete answer without stray characters, and the counts."""
    rewriter = Rewriter(language, allowed)
    text = rewriter.feed(answer) + rewriter.finish()
    return text, rewriter.report()


def log(counts: dict[str, int], task_type: str) -> None:
    """Counts only: the answer never reaches the log."""
    if counts:
        logging.getLogger(__name__).warning(
            "stray_script %s",
            json.dumps({**counts, "task_type": task_type}, sort_keys=True),
        )


Sink = Callable[[dict[str, object]], Awaitable[None]]


class Gate:
    """Streams answer text through a Rewriter; other events keep their order."""

    def __init__(self, sink: Sink, rewriter: Rewriter) -> None:
        self.sink = sink
        self.rewriter = rewriter

    async def __call__(self, event: dict[str, object]) -> None:
        delta = event.get("delta")
        if (
            len(event) == 1
            and isinstance(delta, dict)
            and len(delta) == 1
            and isinstance(delta.get("content"), str)
        ):
            text = self.rewriter.feed(str(delta["content"]))
            if text:
                await self.sink({"delta": {"content": text}})
            return
        await self.flush()
        await self.sink(event)

    async def flush(self) -> None:
        text = self.rewriter.finish()
        if text:
            await self.sink({"delta": {"content": text}})
