"""The person's own words and the text they pasted, told apart by the layout.

A pasted text (an email, an article, notes, a passage) never decides what a
request asks for nor the language of the answer: only the person's own words
around it do (owner rulings 2026-11-17 and 2026-11-19). A pasted text is found
by its length (a long paragraph), by its delimiters (a code fence, a quote
block, a long quotation) or by the person's words that announce it ("Summarize
this email:", "Write a summary of these notes", "TL;DR this" after the text).

This module is copied alone into the chat UI image with packages/language.py
(infra/docker/Dockerfile.webui, serving.py mounts), so it uses no other package.
Every expression is linear on any input: no repeat inside a repeat.
"""

from bisect import bisect_right
import re
import sys
import unicodedata

# A paragraph this long is a pasted text. Sizes are UTF-8 bytes, so that a
# dense script (Japanese, Chinese) counts as much text as its Latin translation.
PARAGRAPH_BYTES = 200
# The person's lines that announce a pasted text are at most this long.
REQUEST_BYTES = 300
# A text that the person's words announce is pasted from this size on, or from
# two lines on: a single short line after "Is this right?" stays their own.
BODY_BYTES = 120

_BLANK_LINE = re.compile(r"\n[ \t]*+\n")
_LINE = re.compile(r"\n")
# A blank line, or a horizontal rule ("---", "***", "===", "___") on its own line.
_BREAK = re.compile(r"\n[ \t]*+(?:(?:-{3,}+|\*{3,}+|_{3,}+|={3,}+)[ \t]*+)?+\n")
_RULE_ONLY = re.compile(r"[-*_=\s]*+")
_FENCE = re.compile(r"^[ \t]{0,3}+(?:`{3,}+|~{3,}+|\"{3}+)", re.M)
_QUOTE_LINE = re.compile(r"^[ \t]{0,3}+>(?:[ \t][^\n]*+)?+$", re.M)
_LEAD = re.compile(r"[^\n:\uff1a]{1,200}[:\uff1a](?=\s|$)")
# Quotation marks around a quoted passage, with their closing mark.
_QUOTES = {
    '"': '"',
    "\u201c": "\u201d",
    "\u00ab": "\u00bb",
    "\u201e": "\u201c\u201d",
}
_QUOTE_OPENING = re.compile('["\u201c\u00ab\u201e]')
QUOTED_BYTES = 100

# Words of the person that name the text they give: a text noun after a
# determiner ("this email", "the notes", "ce texte", "diesen Text"), a place in
# the message ("below:", "ci-dessus.") or "TL;DR".
_DETERMINERS = (
    r"this|these|that|those|the|my|our|following|"
    r"ce|cet|cette|ces|le|la|les|l|mon|ma|mes|notre|suivant\w*|"
    r"dies\w*|der|die|das|den|dem|mein\w*|unser\w*|folgend\w*|"
    r"questo|questa|questi|queste|il|lo|gli|mio|mia|miei|seguent\w*|"
    r"este|esta|estos|estas|el|los|las|mi|mis|siguiente\w*|meu|minha"
)
_TEXT_NOUNS = (
    r"texts?|e-?mails?|mails?|messages?|articles?|passages?|paragraphs?|excerpts?|"
    r"extracts?|notes?|letters?|documents?|poems?|transcripts?|reports?|"
    r"summary|summaries|drafts?|"
    r"textes?|courriels?|lettres?|extraits?|rapports?|compte-rendu|resumes?|"
    r"brouillons?|paragraphes?|poemes?|"
    r"nachricht\w*|artikel\w*|absatz\w*|abschnitt\w*|bericht\w*|auszug\w*|"
    r"entwurf\w*|protokoll\w*|zusammenfassung\w*|notiz\w*|mail\w*|"
    r"testo|testi|lettera|lettere|messaggio|messaggi|articolo|articoli|brano|brani|"
    r"estratto|paragrafo|bozza|riassunto|verbale|"
    r"texto|textos|mensaje|mensajes|articulo|articulos|correo|correos|"
    r"fragmento|informe|parrafo|borrador|resumen|mensagem|artigo"
)
_POSITIONS = (
    r"following|below|above|hereafter|"
    r"suivant\w*|ci-dessous|ci-dessus|ci-apres|"
    r"folgend\w*|nachfolgend\w*|unten|oben|"
    r"seguent\w*|sotto|sopra|"
    r"siguiente\w*|abajo|arriba|"
    r"seguinte\w*|acima"
)
_TEXT_REFERENCE = re.compile(
    r"\b(?:" + _DETERMINERS + r")(?:\s+|')(?:[\w-]+\s+){0,2}?(?:" + _TEXT_NOUNS + r")\b"
    r"|\b(?:" + _POSITIONS + r")\s*+(?:[.:!?,;)]|$)"
    r"|\btl\s?;?\s?dr\b",
    re.M,
)
# "this" as the object of a request on the text: "TL;DR this", "Summarize
# this in three sentences", "Kannst du das zusammenfassen?", "Resume ca".
_PRONOUN = re.compile(
    r"\b(?:this|these|ceci|cela|ca|das|dies|questo|questa|esto|esta|isto|isso)"
    r"(?:\s*+(?:[.:!?,;)]|$)"
    r"|\s++(?:in|into|to|for|as|please|pls|plz|en|pour|stp|svp|auf|ins|bitte|fur|"
    r"per|para|por|em)\b"
    r"|\s++\w+en\s*+(?:[.:!?]|$))",
    re.M,
)
# Verbs of a request on a given text (summarise, translate, check, answer...).
_TASK = re.compile(
    r"\b(?:summar\w*|resum\w*|tl\s?;?\s?dr|translat\w*|tradu\w*|uberset\w*|"
    r"uebersetz\w*|zusammenfass\w*|riassum\w*|sintetizz\w*|rewrite|reword\w*|"
    r"rephrase|paraphras\w*|reformul\w*|umformulier\w*|riformul\w*|proofread\w*|"
    r"correct\w*|corrig\w*|korrigier\w*|corregg\w*|correg\w*|improve|amelior\w*|"
    r"verbesser\w*|miglior\w*|mejor\w*|shorten|raccourc\w*|kurz\w*|simplif\w*|"
    r"vereinfach\w*|semplific\w*|explain|expliqu\w*|erklar\w*|erklaer\w*|spiega\w*|"
    r"explica\w*|analy[sz]\w*|review|relis|relire|check|verif\w*|prufe\w*|"
    r"pruefe\w*|controll\w*|comprueb\w*|reply|answer|respond\w*|repond\w*|"
    r"antwort\w*|rispond\w*|write|ecri\w*|redige\w*|schreib\w*|scrivi\w*|"
    r"escrib\w*|redact\w*|format\w*|extract\w*|extrai\w*|classif\w*|critique|"
    r"evaluat\w*|evalue\w*|bewert\w*|valuta\w*|evalua\w*)\b"
)


def _plain(text: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(c)
    )


def _announces(words: str, *, colon: bool) -> bool:
    """Whether the person's words name a text they give.

    A text noun, a place in the message or "TL;DR" names it anywhere; "this"
    names it with a verb of a request on a text; after a colon that ends the
    words, such a verb alone does ("Summarize:", "Corrige les fautes :").
    """
    if len(words.encode()) > REQUEST_BYTES:
        return False
    plain = _plain(words)
    if _TEXT_REFERENCE.search(plain):
        return True
    task = _TASK.search(plain) is not None
    return task and (colon or _PRONOUN.search(plain) is not None)


def _substantial(body: str) -> bool:
    """Whether the rest of the message is a text: two lines or BODY_BYTES.

    Lines that are all questions are the person's own questions ("Answer these
    questions:" then the questions), not a text they give.
    """
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if lines and all(line.endswith(("?", "\uff1f")) for line in lines):
        return False
    return len(body.strip().encode()) >= BODY_BYTES or len(lines) >= 2


def _spans(text: str, separator: re.Pattern[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in separator.finditer(text):
        if not _RULE_ONLY.fullmatch(text[start : match.start()]):
            spans.append((start, match.start()))
        start = match.end()
    if not _RULE_ONLY.fullmatch(text[start:]):
        spans.append((start, len(text)))
    return spans


def _inside(cuts: list[tuple[int, int]], position: int) -> bool:
    """Whether a position falls in one of the sorted, disjoint cuts."""
    index = bisect_right(cuts, (position, sys.maxsize)) - 1
    return index >= 0 and cuts[index][0] <= position < cuts[index][1]


def _delimited(text: str) -> tuple[str, list[str]]:
    """The text without its fenced blocks, quote blocks and long quotations."""
    cuts: list[tuple[int, int]] = []
    position = 0
    while (opening := _FENCE.search(text, position)) is not None:
        marker = opening[0].strip()
        line_end = text.find("\n", opening.end())
        if line_end < 0:
            break
        closing = re.compile(
            r"^[ \t]{0,3}+" + re.escape(marker[0]) + "{" + str(len(marker)) + ",}+",
            re.M,
        ).search(text, line_end + 1)
        end = len(text) if closing is None else closing.end()
        cuts.append((opening.start(), end))
        position = end
    # Consecutive quote lines ("> ...") make one block.
    runs: list[tuple[int, int]] = []
    for line in _QUOTE_LINE.finditer(text):
        if runs and runs[-1][1] + 1 == line.start():
            runs[-1] = (runs[-1][0], line.end())
        else:
            runs.append((line.start(), line.end()))
    cuts = sorted([*cuts, *(run for run in runs if not _inside(cuts, run[0]))])
    closed: set[str] = set()
    quoted: list[tuple[int, int]] = []
    position = 0
    while (found := _QUOTE_OPENING.search(text, position)) is not None:
        char = found[0]
        position = found.end()
        if char in closed or _inside(cuts, found.start()):
            continue
        ends = [
            index
            for closer in _QUOTES[char]
            if (index := text.find(closer, position)) >= 0
        ]
        if not ends:
            closed.add(char)
            continue
        end = min(ends) + 1
        inner = text[position : end - 1]
        if "\n" in inner.strip() or len(inner.encode()) >= QUOTED_BYTES:
            quoted.append((found.start(), end))
        position = end
    cuts = sorted([*cuts, *quoted])
    if not cuts:
        return text, []
    own: list[str] = []
    pasted: list[str] = []
    last = 0
    for start, end in cuts:
        if end <= last:
            continue
        start = max(start, last)
        own.append(text[last:start])
        pasted.append(text[start:end])
        last = end
    own.append(text[last:])
    return "".join(own), pasted


def _by_length(text: str) -> tuple[str, list[str]] | None:
    """Around the long paragraphs (or long lines): the person's own lines."""
    if len(text.encode()) < 2 * PARAGRAPH_BYTES:
        return None
    for separator, joiner in ((_BLANK_LINE, "\n\n"), (_LINE, "\n")):
        # Each part with its place in the text: the pasted block is cut out
        # of the text as it was written.
        spans = _spans(text, separator)
        long = [
            index
            for index, (begin, end) in enumerate(spans)
            if len(text[begin:end].encode()) > PARAGRAPH_BYTES
        ]
        if not long:
            return None
        kept = spans[: long[0]] + spans[long[-1] + 1 :]
        if kept:
            return joiner.join(text[begin:end] for begin, end in kept), [
                text[spans[long[0]][0] : spans[long[-1]][1]]
            ]
    lead = _LEAD.match(text)
    if lead is None:
        return text, []
    return lead.group(0), [text[lead.end() :]]


def _by_announcement(text: str) -> tuple[str, list[str]] | None:
    """The person's first or last lines when they announce the rest as a text.

    Words ending with a colon before the text announce it most plainly, then
    words after it ("TL;DR this"), then words before it without a colon ("Can
    you summarise this?").
    """
    for separator in (_BREAK, _LINE):
        spans = _spans(text, separator)
        if len(spans) < 2:
            continue
        head = text[spans[0][0] : spans[0][1]]
        rest = text[spans[1][0] : spans[-1][1]]
        tail = text[spans[-1][0] : spans[-1][1]]
        body = text[spans[0][0] : spans[-2][1]]
        colon = head.rstrip().endswith((":", "\uff1a"))
        if colon and _announces(head, colon=True) and _substantial(rest):
            return head, [rest]
        if _announces(tail, colon=False) and _substantial(body):
            return tail, [body]
        if _announces(head, colon=False) and _substantial(rest):
            return head, [rest]
    lead = _LEAD.match(text)
    if lead is not None and _announces(lead[0], colon=True):
        rest = text[lead.end() :]
        if _substantial(rest):
            return lead[0], [rest]
    return None


def split(text: str) -> tuple[str, list[str]]:
    """The person's own words, and the pasted texts around which they are read.

    Without any pasted text, or when nothing is left around it, the whole
    message is the person's own words.
    """
    own, pasted = _delimited(text)
    if not own.strip():
        return text, []
    found = _by_length(own) or _by_announcement(own)
    if found is None or not found[0].strip():
        return own, pasted
    return found[0], [*pasted, *found[1]]


def own_words(text: str) -> str:
    """The person's own words: the message without the text they pasted in it."""
    return split(text)[0]
