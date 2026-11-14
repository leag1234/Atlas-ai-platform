"""The request, rewritten for the CPU image model: one fixed illustration style.

One short JSON call turns a request in any language and wording into an
English prompt for the image model (spike round 25, rewrite v2: 0 of 116
pictures looked photographic; the raw request gave 15 of 19). Code, not the
model, guarantees the fixed style line and strips the words that ask for a
photograph. For a diagram the picture is wordless and the words to place
come back separately (`labels`, in the user's language), with a short title.
The same call refuses violent, cruel, hateful or sexual requests before any
picture is drawn; the image filter checks every picture afterwards.

Two details the image model alone loses (seen on staging): counted or
unusual features ("a sheep with 5 legs" gave two sheep with four legs) and
words to show ("a logo for « Le Pain d'Élise »" gave a logo without the
name). The rewrite states counted and unusual features first, in words,
and returns them as `checks` (short statements in the user's language that
a vision check compares with each picture, cpu_checks). Words to show come
back in `text`, kept exactly as the user wrote them (code takes them from
the request itself when the reply changed an accent, a quote or a capital);
the image model is asked for a plain empty band and code sets the words
(text_setting), never the image model.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

MAX_TOKENS = 900
TEMPERATURE = 0.2
MAX_LABELS = 10
MAX_LABEL_CHARS = 40
MAX_CHECKS = 6
MAX_CHECK_CHARS = 80
MAX_TEXT_LINES = 3
MAX_TEXT_CHARS = 60
MAX_PROMPT_CHARS = 1200
# Added by code when words are to be set: the image model leaves them room.
RESERVE = (
    "Leave a wide plain empty band of flat background colour across the top "
    "quarter of the picture, with nothing drawn in it"
)
STYLE = (
    "Flat vector illustration, clean bold outlines, flat soft colours, simple "
    "shapes, gentle storybook look, whole subjects visible, no text or "
    "lettering anywhere."
)
PHOTOGRAPHIC = (
    "photorealistic",
    "photographic",
    "photograph",
    "photo",
    "realistic",
    "camera",
    "lens",
    "bokeh",
)
# The measured round 25 system prompt (rules 1 to 6), plus the title and the
# refusal rule; the reply format gains "title" and "refuse".
SYSTEM = (
    "You write the prompt for a text-to-image model that only makes illustrations. "
    "Input: a user's request, in any language.\n"
    "Rules:\n"
    "1. Keep exactly the subjects, counts, colours, actions, positions and relations "
    "the user asked for. Add nothing else: no extra people, animals, objects, props, "
    "decorations or text. Add only the setting and light needed to place what was asked.\n"
    "2. English only. Fix spelling. Keep given first names.\n"
    "3. Two or three short concrete sentences: each subject the user named, its pose "
    "and the action asked, who is in front of or behind what, the setting. Never put "
    "an object in a hand, paw or mouth unless the user said so; an empty hand stays "
    "empty.\n"
    "4. The picture is always an illustration, never a photograph, even if the user "
    "writes photo, photograph, realistic or picture: describe the scene, not a "
    "camera. Never write the words photo, photograph, photographic, realistic, "
    "camera, lens, bokeh, film.\n"
    "5. If the user asks for a diagram, a schema or labelled parts: describe a clean "
    "wordless educational illustration of the parts, each part clearly separated "
    "and fully visible on a plain light background, with wide empty margins left "
    "and right, arrows only if the user asked for arrows or a cycle; exactly one "
    "drawing per part or stage the user listed, nothing more; no words, letters or "
    "numbers in the picture. Put the words to write on the picture in 'labels', in "
    "the user's language, one per part (if the user does not list them, choose the "
    "4 to 8 main parts).\n"
    "6. End the prompt with exactly this line: " + STYLE + "\n"
    "7. 'title': two to five words naming the picture, in the user's language.\n"
    "8. If the request asks for sexual content or nudity, graphic violence, gore, "
    "cruelty to people or animals, or hateful or demeaning content about people "
    "for who they are, write no prompt and set 'refuse' to true.\n"
    "9. Counts and unusual features: when the user gives a number (of subjects or "
    "of parts such as legs, heads, tails, wheels, chairs), a colour, or a feature "
    "that is not the usual one (a shape, a body part, a size), the first sentence "
    "starts with it, as the subject's main trait: a number the user wrote in words "
    "with 'exactly' (for example 'A single sheep with exactly five legs, all five "
    "legs clearly visible, stands in a meadow.'); a feature without a number keeps "
    "no number (for example 'A single red bicycle whose wheels are square'). Name "
    "each subject once; one subject asked is 'a single' subject, never plural. Put "
    "each number, colour or unusual feature the user wrote in 'checks', as a "
    "short statement in the user's language that can be checked by looking at "
    "the picture (for example ['un seul mouton', 'le mouton a cinq pattes']); "
    "never the setting, objects you chose, the empty area or the words to show; "
    "'checks' is empty when the user wrote no number, colour or unusual feature.\n"
    "10. Words to show: if the user wants words on the picture (a name, title or "
    "slogan on a logo, sign, poster, card, cover, banner, label, or any quoted "
    "words), put them in 'text', exactly as the user wrote them (same spelling, "
    "accents, apostrophes and capitals, without the quotation marks around them), "
    "one string per line, at most three lines; if the user wants words but does "
    "not give them (a birthday card), write short ones in the user's language. "
    "Never put these words in the prompt and never mention them (no sign, banner "
    "or label 'reading' anything); describe the design with a wide plain empty "
    "area across the top. 'text' is empty when nothing is to be "
    "written on the picture. A diagram's words go in 'labels', never in 'text'.\n"
    'Reply with JSON only: {"prompt": "...", "labels": [...], "diagram": true|false, '
    '"title": "...", "checks": [...], "text": [...], "refuse": false}. labels is '
    "empty when nothing is to be written."
)
LANGUAGES = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
}
ANSWER_LANGUAGE = (
    "\n\n(Write 'checks' in {language}. The prompt stays in English; 'text' "
    "stays as the user wrote it.)"
)
CHANGE = (
    "The picture was drawn from this request: {request}\n"
    "Its prompt was: {prompt}\n"
    "The user now asks for this change: {change}\n"
    "Write the prompt of the changed picture: apply the change and keep "
    "everything else as it was."
)


@dataclass
class Rewrite:
    prompt: str
    labels: list[str] = field(default_factory=list)
    diagram: bool = False
    title: str = ""
    refused: bool = False
    # Counted or unusual features to check on each picture (user's language).
    checks: list[str] = field(default_factory=list)
    # Words to set on the picture, one string per line, as the user wrote them.
    text: list[str] = field(default_factory=list)


def messages(
    request_text: str, *, previous: str = "", change: str = "", language: str = ""
) -> list[dict[str, str]]:
    """The rewrite call; with `change`, the prompt of a changed picture. The
    answer language names the language of the checks the answer quotes."""
    user = (
        CHANGE.format(request=request_text, prompt=previous, change=change)
        if change
        else request_text
    )
    if language in LANGUAGES:
        user += ANSWER_LANGUAGE.format(language=LANGUAGES[language])
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def enforce(prompt: str, *, reserve: bool = False) -> str:
    """The fixed style line, once, at the end; no photographic word anywhere;
    with `reserve`, the empty band for the words code sets afterwards."""
    text = prompt.replace(STYLE, "").replace(RESERVE, "").strip()
    for word in PHOTOGRAPHIC:
        text = re.sub(r"\b" + word + r"\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\.(?:\s*\.)+", ".", text)
    text = re.sub(r"[\s.]+$", "", text).strip()
    if not text:
        return ""
    return text + ". " + (RESERVE + ". " if reserve else "") + STYLE


# Quotation marks and spaces that differ between a reply and the request.
_SAME = str.maketrans(
    {
        "\u2019": "'",
        "\u2018": "'",
        "\u02bc": "'",
        "`": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00ab": '"',
        "\u00bb": '"',
        "\u201e": '"',
        "\u00a0": " ",
        "\u202f": " ",
    }
)
_OPENING = "\"'\u00ab\u201c\u2018\u201e"
_CLOSING = "\"'\u00bb\u201d\u2019\u201c"


def _folded(text: str) -> tuple[str, list[int]]:
    """`text` without accents, case or quote style, one space for any run of
    spaces, and the index in `text` of each folded character."""
    out: list[str] = []
    where: list[int] = []
    for index, char in enumerate(text):
        base = unicodedata.normalize("NFKD", char.translate(_SAME))
        for piece in "".join(
            c for c in base if not unicodedata.combining(c)
        ).casefold():
            if piece.isspace():
                if out and out[-1] == " ":
                    continue
                piece = " "
            out.append(piece)
            where.append(index)
    return "".join(out), where


def _unquoted(line: str) -> str:
    line = " ".join(line.split())
    while len(line) > 2 and line[0] in _OPENING and line[-1] in _CLOSING:
        line = line[1:-1].strip()
    return line


def verbatim(line: str, source: str) -> str:
    """The words as the user wrote them: when the request holds `line` up to
    accents, case, quote style or spacing, the request's own spelling."""
    line = _unquoted(line)
    if not line or not source or line in source:
        return line
    folded_source, where = _folded(source)
    folded_line = _folded(line)[0].strip()
    start = folded_source.find(folded_line) if folded_line else -1
    if start < 0:
        return line
    end = start + len(folded_line) - 1
    first, last = where[start], where[end] + 1
    found = source[first:last]
    before, after = source[:first].rstrip(), source[last:].lstrip()
    quoted = bool(before) and before[-1] in _OPENING and after[:1] in tuple(_CLOSING)
    if quoted or len(found) != len(line):
        return found
    # Words the user did not quote: the user's accents, the reply's capitals.
    return "".join(
        mine.upper() if theirs.isupper() else mine for mine, theirs in zip(found, line)
    )


def _strings(value: Any, limit: int, chars: int) -> list[str]:
    if isinstance(value, str):
        value = value.split("\n")
    found: list[str] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            clean = " ".join(item.split())[:chars]
            if clean and clean not in found:
                found.append(clean)
    return found[:limit]


def _object(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# What introduces words in a prompt, left dangling once the words are gone.
_INTRO = (
    r"(?:\b(?:reading|reads|saying|says|titled|named|called|with|for)\s+)?"
    r"(?:\bthe\s+(?:words?|name|title|text)\s*)?"
)


def _loose(line: str) -> str:
    """A pattern of `line` with or without its accents, any apostrophe."""
    out: list[str] = []
    for char in line:
        if char in "'\u2019\u2018\u02bc":
            out.append("['\u2019\u2018\u02bc]")
            continue
        base = "".join(
            c
            for c in unicodedata.normalize("NFKD", char)
            if not unicodedata.combining(c)
        )
        same = base == char or not base
        out.append(
            re.escape(char) if same else f"(?:{re.escape(char)}|{re.escape(base)})"
        )
    return r"\s+".join("".join(out).split("\\ "))


def _without_words(prompt: str, words: list[str]) -> str:
    """The prompt without the words to set, any quoted string or what
    introduced them: the image model would letter them (badly); code sets
    them."""
    for line in words:
        prompt = re.sub(
            _INTRO + r"[\u00ab\u201c\"']?\s*" + _loose(line) + r"\s*[\u00bb\u201d\"']?",
            "",
            prompt,
            flags=re.IGNORECASE,
        )
    prompt = re.sub(
        _INTRO + r"[\u00ab\u201c\"][^\u00bb\u201d\"]*[\u00bb\u201d\"]", "", prompt
    )
    prompt = re.sub(_INTRO + r"''", "", prompt)
    prompt = re.sub(r"\b(?:in|at|of|on|for|with|from)\s*(?=[,.;:])", "", prompt)
    prompt = re.sub(r"\s+([,.;:])", r"\1", prompt)
    return re.sub(r"[,;:]+(?=[.,;:])", "", prompt)


def parse(text: str, *, source: str = "") -> Rewrite | None:
    """The rewrite of one reply, or None when it cannot be used. `source` is
    the request (and change) the words to set must be taken from."""
    value = _object(text)
    if value is None:
        return None
    if value.get("refuse") is True:
        return Rewrite(prompt="", refused=True)
    raw = value.get("prompt")
    if not isinstance(raw, str):
        return None
    labels = _strings(value.get("labels"), MAX_LABELS, MAX_LABEL_CHARS)
    diagram = value.get("diagram") is True and bool(labels)
    words: list[str] = []
    if not diagram:
        for line in _strings(value.get("text"), MAX_TEXT_LINES, 4 * MAX_TEXT_CHARS):
            exact = verbatim(line, source)[:MAX_TEXT_CHARS]
            if exact and exact not in words:
                words.append(exact)
    raw = raw[:MAX_PROMPT_CHARS]
    if words:
        raw = _without_words(raw, words)
    prompt = enforce(raw, reserve=bool(words))
    if len(prompt) <= len(STYLE) + 2:
        return None
    title = value.get("title")
    return Rewrite(
        prompt=prompt,
        labels=labels,
        diagram=diagram,
        title=" ".join(title.split())[:80] if isinstance(title, str) else "",
        checks=_strings(value.get("checks"), MAX_CHECKS, MAX_CHECK_CHARS),
        text=words,
    )
