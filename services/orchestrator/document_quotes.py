"""Quotations attributed to the user's documents, checked word for word.

An answer that quotes an attached file, a project or corpus passage, or text
pasted in the conversation ("the contract states: « ... »", "> ..." after "page 3
reads:") presents those words as the document's own. Code checks that each such
quotation appears in the text the turn actually holds; the model decides what to
quote, code never chooses wording.

Formatting is all that may differ: Unicode compatibility forms (ligatures),
typographic apostrophes, quotation marks and dashes, case, whitespace and line
breaks, a word hyphenated at a line break, grouped thousands, punctuation between
words and markdown emphasis, and what PDF extraction puts inside a sentence (a
page break with its footnotes, page number and running header, a footnote call,
a word split in two). Words and their order must be identical. An
ellipsis ("...", "…", "[…]", "(...)") or a bracketed insertion ("[the Tenant]")
separates fragments that must each appear, in that order, in the same document.

A quotation is checked only when it is attributed to a document: a cue before it
(a verb such as "states" or "stipule", a noun such as "article", "page" or
"contrat", the file name) or a request for quotations in the user's message, with
no later cue that it is the answer's own wording (an example, a suggestion, a
translation). Quotations of fewer than four words, inside code, of a named
external law, or found in other evidence of the turn (a fetched web page, which
stays under the D7 source rules) are left as written.

Measured on 2026-11-16 (tools/document_quotes_eval.py, 20 requests on four
documents, two profiles, 40 live turns): 104 document quotations checked, 2 not
found, both passages stitched out of order or attributed to the wrong clause; no
correct quotation flagged. A bounded correction call given the closest passage
was compared on those cases: it truncated one quotation and returned no usable
quotation for the other, at the cost of a call and a pause in the stream; the
note keeps the model's words and only withdraws the verbatim claim.

ATLAS_DOCUMENT_QUOTES selects the behaviour: "mark" (default) shows a quotation
that is not found without its quotation marks, followed by a short localized note
(the words stay, they are no longer presented as verbatim); "log" only counts;
"off" disables the check. Every turn holding a document logs its counts at
WARNING level, never any text.

Streaming: the Gate holds an open quotation (at most MAX_QUOTE characters) or a
block quote until it closes, then releases it checked; text outside quotations
streams unchanged. The same Rewriter produces the final answer from the complete
text, so the streamed and stored answers agree. Every step is a single pass over
the text; sources are normalized once per turn.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
import json
import logging
import os
import re
import unicodedata
from bisect import bisect_right

LOGGER = logging.getLogger(__name__)

# Characters of document text kept per turn (all sources together).
SOURCE_LIMIT = 4_000_000
# Evidence that is not a document (tool results such as fetched pages).
EVIDENCE_LIMIT = 2_000_000
# A user message at least this long is pasted material: it makes the turn a
# document turn. Shorter messages are still sources (a quoted user sentence is
# found), but they alone never start the check.
PASTED_MINIMUM = 400
# A quotation of fewer words is a term or a name, not a passage.
MIN_WORDS = 4
# An open quotation is held at most this long, then released unchecked.
MAX_QUOTE = 2_000
MAX_BLOCK = 6_000
# A sentence running over a PDF page break: its first words end at most PAGE_TAIL
# characters before the next page (footnotes), the rest starts at most PAGE_HEAD
# characters after it (page number, running header).
PAGE_TAIL = 1_500
PAGE_HEAD = 300
ACROSS_TRIES = 40
# Context before a quotation read for attribution cues.
CONTEXT = 220
MAX_QUOTES = 60

NOTES = {
    "fr": "citation non retrouvée mot pour mot dans les documents fournis",
    "en": "quotation not found word for word in the documents provided",
    "de": "Zitat nicht wörtlich in den bereitgestellten Dokumenten gefunden",
    "es": "cita no encontrada literalmente en los documentos proporcionados",
    "it": "citazione non trovata testualmente nei documenti forniti",
}

# Opening quotation mark -> closing marks.
CLOSERS = {
    "«": "»",
    "“": "”",
    "„": "“”",
    '"': '"',
    "‘": "’",
    "‚": "‘’",
    "‹": "›",
}
SPECIAL = re.compile(r"[«“„\"‘‚‹`>\n]")

_FORMAT = str.maketrans(
    {
        "’": "'",
        "‘": "'",
        "‛": "'",
        "ʼ": "'",
        "´": "'",
        "`": "'",
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        "­": None,
    }
)
# A word hyphenated at a line break: the hyphen, the break and the indentation.
_LINE_HYPHEN = re.compile(r"(?<=[^\W\d_])-[ \t]*+\r?\n[ \t]*+(?=[^\W\d_])")
# A space inside a grouped number ("1 000"), after NFKC turned narrow spaces into spaces.
_THOUSANDS = re.compile(r"(?<=\d) (?=\d\d\d(?!\d))")
# A word: letters and digits, joined by an apostrophe or a hyphen, or by a decimal
# point or comma between digits. Possessive: never re-split.
_WORD = re.compile(r"[^\W_]++(?:['\-][^\W_]++|(?<=\d)[.,](?=\d)[^\W_]++)*+")
# An ellipsis or a bracketed insertion: a gap between verbatim fragments.
_GAP = re.compile(r"\[[^\]\n]{0,60}\]|\((?:…|\.\.\.?)\)|…|\.\.+")
_PARAGRAPH = re.compile(r"\n[ \t]*\n")
# The page line the attachment reader puts before each PDF page.
_PAGE_MARK = re.compile(r"(?m)^\[Page \d{1,6}\]$")
# A footnote call or page number left by PDF extraction (see Text).
_FOOTNOTE_CALL = re.compile(
    r"(?<=[^\W\d_])\d{1,3}(?!\w)"
    r"|(?m:^[ \t]*\d{1,3}[ \t]*$)"
    r"|(?<=[^\W\d_]) \d{1,3}(?=[.,;:]|[ \t]*\n)"
)
# A block quotation that is a note, not a quotation ("> **Note:** ...", "> [!TIP]").
_CALLOUT = re.compile(
    r"(?i)\s*(?:\[!|\*\*\s*(?:note|remarque|attention|important|warning|tip|"
    r"hinweis|achtung|tipp|nota|avertissement|conseil|astuce|aviso|consejo|avviso)\b)"
)
# A leading bold label of a quoted paragraph ("**Article 3** :", "**(1)**").
_LABEL = re.compile(r"\s*\*\*[^*\n]{1,60}\*\*[ \t]*[:.–—-]?[ \t]*")

# Words that attribute the quotation to a document, in the five UI languages.
_ATTRIBUTION = re.compile(
    r"(?i)(?:§|\b(?:"
    # French
    r"stipul\w*+|dispos\w*+|prévoi\w*+|prévu\w*+|indiqu\w*+|précis\w*+|mentionn\w*+|"
    r"énonc\w*+|affirm\w*+|déclar\w*+|dit|disent|lit|lire|lit-on|conclu\w*+|"
    r"soulign\w*+|rappell?\w*+|cit\w*+|extrait\w*+|passage\w*+|texte\w*+|termes|"
    r"libellé\w*+|intitulé\w*+|mot pour mot|textuellement|verbatim|document\w*+|"
    r"contrat\w*+|rapport\w*+|article\w*+|art|clause\w*+|paragraphe\w*+|alinéa\w*+|"
    r"section\w*+|pages?|p|chapitre\w*+|annexe\w*+|fichier\w*+|pdf|loi\w*+|décret\w*+|"
    r"préambule|considérant\w*+|"
    # English
    r"states?|stated|says|said|reads?|provides?|provided|stipulat\w*+|specif\w*+|"
    r"mentions?|mentioned|notes?|noted|declares?|declared|"
    r"concludes?|concluded|quot\w*+|excerpt\w*+|wording|word for word|literally|"
    r"contract\w*+|report\w*+|paragraph\w*+|chapter\w*+|annex\w*+|appendix|files?|"
    r"law|act|text|terms|titled|entitled|"
    # German
    r"heißt|lautet|laut|besagt|bestimmt|vorgesehen|sieht|steht|erklärt|"
    r"wörtlich|zitat\w*+|zitiert|auszug|wortlaut|dokument\w*+|vertrag\w*+|verträg\w*+|"
    r"bericht\w*+|artikel\w*+|absatz\w*+|abschnitt\w*+|seite\w*+|kapitel\w*+|"
    r"gesetz\w*+|paragraf\w*+|datei\w*+|klausel\w*+|anhang\w*+|"
    # Spanish
    r"establece\w*+|dice|dicen|estipula\w*+|dispone\w*+|indica\w*+|señala\w*+|"
    r"afirma\w*+|menciona\w*+|textual\w*+|literal\w*+|contrato\w*+|informe\w*+|"
    r"artículo\w*+|apartado\w*+|página\w*+|ley\w*+|cláusula\w*+|capítulo\w*+|"
    r"anexo\w*+|archivo\w*+|texto\w*+|"
    # Italian
    r"stabilisce|prevede|recita|afferma|testual\w*+|letteral\w*+|contratt\w*+|"
    r"relazione\w*+|articol\w*+|comma|pagin\w*+|legge|clausol\w*+|capitol\w*+|"
    r"allegat\w*+|testo"
    r")\b)"
)
# Words that make the quotation the answer's own wording: an example, a
# suggested or rewritten text, a translation.
_OWN_WORDING = re.compile(
    r"(?i)\b(?:"
    r"par exemple|exemple de|vous pourriez|vous pouvez (?:écrire|dire|répondre|"
    r"formuler|indiquer)|je (?:vous )?(?:propose|suggère)|nous (?:proposons|suggérons)|"
    r"formulation (?:proposée|possible|suggérée)|reformul\w*+|proposition de "
    r"(?:texte|rédaction|formulation|réponse|clause)|traduction\w*+|tradui\w*+|"
    r"modèle de|"
    r"for example|for instance|e\.g|example of|you could|you might|you can (?:say|"
    r"write|reply)|i (?:would )?(?:suggest|propose|recommend)|we (?:suggest|propose)|"
    r"(?:suggested|proposed|possible|alternative) wording|rephras\w*+|reword\w*+|"
    r"translation\w*+|translated|draft\w*+|rewrit\w*+|template|"
    r"zum beispiel|z\. ?b|beispielsweise|vorschlag\w*+|vorgeschlagen\w*+|"
    r"formulierungsvorschlag\w*+|umformul\w*+|übersetz\w*+|sie könnten|könnten sie|"
    r"por ejemplo|ejemplo de|podría (?:escribir|decir)|propongo|sugiero|"
    r"sugerencia\w*+|traducción|traducido|plantilla|reformul\w*+|"
    r"per esempio|ad esempio|potrebbe (?:scrivere|dire)|suggerisc\w*+|"
    r"riformul\w*+|traduzione|tradott\w*+"
    r")\b"
)
# A named external legal text: a quotation of it is not a quotation of the
# user's document unless the document itself names it.
_EXTERNAL = re.compile(
    r"(?i)\b(?:code (?:civil|pénal|du travail|de commerce|de la consommation|"
    r"de procédure (?:civile|pénale)|général des impôts)|bgb|hgb|stgb|zpo|"
    r"grundgesetz|código (?:civil|penal)|codice (?:civile|penale)|civil code|"
    r"criminal code|constitution|rgpd|gdpr|dsgvo)\b"
)
# A request for quotations in the user's message.
_ASKED = re.compile(
    r"(?i)\b(?:cite[rsz]?|citant|citation\w*+|citations?|quot\w*+|verbatim|"
    r"mot pour mot|textuel\w*+|word for word|exact (?:words|wording|passage\w*+)|"
    r"extrait\w*+|excerpt\w*+|passages?|wörtlich\w*+|zitier\w*+|zitat\w*+|auszug\w*+|"
    r"textual\w*+|literal\w*+|cita\w*+|testual\w*+|letteral\w*+|passo|passi)\b"
)


def mode() -> str:
    value = os.environ.get("ATLAS_DOCUMENT_QUOTES", "mark")
    return value if value in {"off", "log", "mark"} else "mark"


def canonical(text: str) -> str:
    """Words of a text with formatting removed, joined by single spaces."""
    text = unicodedata.normalize("NFKC", text).translate(_FORMAT)
    text = _LINE_HYPHEN.sub("", text)
    text = _THOUSANDS.sub("", text.casefold())
    return " ".join(word[0].replace("-", "") for word in _WORD.finditer(text))


def fragments(content: str) -> tuple[list[str], bool]:
    """Canonical fragments of a quotation, and whether it holds a gap marker."""
    parts = _GAP.split(content)
    found = [canonical(part) for part in parts]
    return [part for part in found if part], len(parts) > 1


class Text:
    """One source in the forms quotations are compared with.

    ``spaced``: canonical words, page by page, joined (the extractor's "[Page N]"
    lines are dropped). ``loose``: the same after removing what PDF extraction
    leaves of footnote calls and page numbers: one to three digits glued to a
    word ("adequate30"), alone on a line, or after a word and before
    punctuation ("mandate 22."); a number inside a sentence ("the 27 Member
    States") stays. ``joined``: each of them without spaces, for a word split or
    glued by extraction ("prévis ible", "main -d'œuvre"). ``starts`` and
    ``loose_starts`` are the offsets where each page begins.
    """

    def __init__(self, text: str) -> None:
        raw = [page for page in _PAGE_MARK.split(text) if page.strip()]
        self.spaced, self.starts = _paged([canonical(page) for page in raw])
        self.loose, self.loose_starts = _paged(
            [canonical(_FOOTNOTE_CALL.sub(" ", page)) for page in raw]
        )
        self.joined = (self.spaced.replace(" ", ""), self.loose.replace(" ", ""))

    def holds(self, parts: list[str]) -> bool:
        return (
            _ordered(parts, self.spaced, self.starts)
            or _ordered(parts, self.loose, self.loose_starts)
            or any(_ordered_joined(parts, joined) for joined in self.joined)
        )


def _paged(pages: list[str]) -> tuple[str, list[int]]:
    starts: list[int] = []
    position = 0
    for page in pages:
        starts.append(position)
        position += len(page) + 1
    return " " + " ".join(pages) + " ", starts


def _ordered(parts: list[str], text: str, starts: list[int]) -> bool:
    """Every fragment in order; one may run over a page break (see _across)."""
    position = 0
    for part in parts:
        needle = " " + part + " "
        found = text.find(needle, position)
        if found >= 0:
            position = found + len(needle) - 1
            continue
        position = _across(part, text, starts, position)
        if position < 0:
            return False
    return True


def _across(part: str, text: str, starts: list[int], position: int) -> int:
    """End of a fragment interrupted by a page break, or -1.

    PDF extraction puts the footnotes and the page number of a page, then the
    running header of the next one, inside a sentence that runs over the break.
    The fragment matches when its first words end within PAGE_TAIL characters
    before a page start and all its other words start within PAGE_HEAD
    characters after it: the words are the same, in the same order, only page
    furniture lies between them. Candidates are the places where the
    fragment's first words occur (at most ACROSS_TRIES of them): one pass over
    the text, then short checks.
    """
    words = part.split(" ")
    if len(words) < 2:
        return -1
    opening = " " + " ".join(words[: min(3, len(words) - 1)]) + " "
    at = text.find(opening, position)
    tries = 0
    while at >= 0 and tries < ACROSS_TRIES:
        tries += 1
        page = bisect_right(starts, at)
        if page >= len(starts):
            return -1
        start = starts[page]
        if start - at <= PAGE_TAIL + len(part):
            # The longest beginning written at this place, before the page start.
            low, high = 1, len(words) - 1
            while low < high:
                middle = (low + high + 1) // 2
                head = " " + " ".join(words[:middle]) + " "
                if text.startswith(head, at) and at + len(head) <= start + 1:
                    low = middle
                else:
                    high = middle - 1
            for split in range(low, max(0, low - 3), -1):
                rest = " " + " ".join(words[split:]) + " "
                found = text.find(rest, start, start + PAGE_HEAD + len(rest))
                if found >= 0:
                    return found + len(rest) - 1
        at = text.find(opening, at + 1)
    return -1


def _ordered_joined(parts: list[str], text: str) -> bool:
    position = 0
    for part in parts:
        needle = part.replace(" ", "")
        found = text.find(needle, position)
        if found < 0:
            return False
        position = found + len(needle)
    return True


class Sources:
    """Text the turn holds: documents, the conversation, other evidence.

    Bounded, kept in memory for the turn only, never logged or sent anywhere.
    """

    def __init__(self) -> None:
        self.documents: list[str] = []
        self.names: list[str] = []
        self.conversation: list[str] = []
        self.evidence: list[str] = []
        self.characters = 0
        self.evidence_characters = 0
        self._texts: list[Text] | None = None
        self._evidence: list[str] | None = None
        self._names: str | None = None

    def add_document(self, text: object, name: object = "") -> None:
        if not isinstance(text, str) or not text.strip():
            return
        text = text[: max(0, SOURCE_LIMIT - self.characters)]
        if not text:
            return
        self.characters += len(text)
        self.documents.append(text)
        if isinstance(name, str) and name:
            self.names.append(name)
        self._texts = None
        self._names = None

    def add_conversation(self, text: str) -> None:
        text = text[: max(0, SOURCE_LIMIT - self.characters)]
        if text.strip():
            self.characters += len(text)
            self.conversation.append(text)
            self._texts = None

    def add_evidence(self, text: str) -> None:
        text = text[: max(0, EVIDENCE_LIMIT - self.evidence_characters)]
        if text.strip():
            self.evidence_characters += len(text)
            self.evidence.append(text)
            self._evidence = None

    def collect(self, value: object) -> None:
        """Strings of a tool result, as evidence that is not a document."""
        if isinstance(value, dict):
            if value.get("error"):
                return
            for item in value.values():
                self.collect(item)
        elif isinstance(value, list):
            for item in value:
                self.collect(item)
        elif isinstance(value, str):
            self.add_evidence(value)

    def active(self) -> bool:
        return bool(self.documents) or any(
            len(text) >= PASTED_MINIMUM for text in self.conversation
        )

    def count(self) -> int:
        return len(self.documents) + sum(
            len(text) >= PASTED_MINIMUM for text in self.conversation
        )

    def texts(self) -> list[Text]:
        """Documents and conversation, normalized once per turn."""
        if self._texts is None:
            self._texts = [Text(text) for text in [*self.documents, *self.conversation]]
        return self._texts

    def holds(self, parts: list[str]) -> bool:
        return any(text.holds(parts) for text in self.texts())

    def names_text(self, name: str) -> bool:
        """True when a document or the conversation contains these words."""
        return any(" " + name + " " in text.spaced for text in self.texts())

    def canonical_evidence(self) -> list[str]:
        if self._evidence is None:
            self._evidence = [" " + canonical(text) + " " for text in self.evidence]
        return self._evidence

    def canonical_names(self) -> str:
        if self._names is None:
            self._names = " ".join(canonical(name) for name in self.names)
        return self._names


def located(parts: list[str], texts: Iterable[str]) -> bool:
    """True when every fragment appears, in order, in one of the texts."""
    return any(_ordered(parts, text, []) for text in texts)


def attributed(before: str, asked: bool, sources: Sources) -> str:
    """ "document", or why the quotation is not a quotation of a document."""
    # A block quotation follows its cue after a blank line: trailing space is
    # dropped before the window is cut at the last paragraph break.
    window = before.rstrip()[-CONTEXT:]
    paragraphs = list(_PARAGRAPH.finditer(window))
    if paragraphs:
        window = window[paragraphs[-1].end() :]
    cue = -1
    for match in _ATTRIBUTION.finditer(window):
        cue = match.end()
    padded = " " + canonical(window) + " "
    if any(
        " " + word + " " in padded
        for word in sources.canonical_names().split(" ")
        if len(word) > 3 and not word.isdigit()
    ):
        cue = max(cue, 0)
    own = -1
    for match in _OWN_WORDING.finditer(window):
        own = match.end()
    if own >= 0 and own >= cue:
        return "own_wording"
    if cue < 0 and not asked:
        return "unattributed"
    external = _EXTERNAL.search(window)
    if external is not None:
        name = canonical(external[0])
        if not sources.names_text(name):
            return "external"
    return "document"


def verdict(before: str, content: str, asked: bool, sources: Sources) -> str:
    parts, gap = fragments(content)
    if sum(part.count(" ") + 1 for part in parts) < MIN_WORDS:
        return "short"
    reason = attributed(before, asked, sources)
    if reason != "document":
        return reason
    if sources.holds(parts):
        return "ellipsis" if gap else "exact"
    if located(parts, sources.canonical_evidence()):
        return "elsewhere"
    return "not_found"


def asked_for_quotes(question: str) -> bool:
    return _ASKED.search(question[:4000]) is not None


class Rewriter:
    """One pass over an answer, chunk by chunk: checks and marks quotations.

    feed() returns the text that can be shown now; finish() the rest. Text
    outside a quotation is returned at once; a quotation is returned once it
    closes (checked) or once it exceeds its bound (unchanged).
    """

    def __init__(
        self,
        sources: Sources,
        *,
        language: str = "en",
        asked: bool = False,
        mark: bool = True,
        blocks: bool = True,
    ) -> None:
        self.sources = sources
        self.note = NOTES.get(language, NOTES["en"])
        self.asked = asked
        self.mark = mark
        self.blocks = blocks
        self.counts: Counter[str] = Counter()
        self.tail = ""
        self.state = "text"
        self.held: list[str] = []
        self.held_size = 0
        self.before = ""
        self.closers = ""
        self.apostrophe = False
        self.block_break = False
        self.block_spaces = ""
        self.line_head = ""
        self.indent_only = True
        self.fence = False
        self.code = False

    # Output bookkeeping -------------------------------------------------

    def _emit(self, out: list[str], text: str, raw: str | None = None) -> None:
        out.append(text)
        self.tail = (self.tail + (text if raw is None else raw))[-CONTEXT * 2 :]

    def _line(self, char: str) -> None:
        """Track line starts, code fences and inline code for one character."""
        if char == "\n":
            self.line_head = ""
            self.indent_only = True
            self.code = False
        else:
            if char not in " \t":
                self.indent_only = False
                if len(self.line_head) < 3:
                    self.line_head += char
                    if self.line_head == "```":
                        self.fence = not self.fence
                        self.code = False
                        return
            if char == "`" and not self.fence:
                self.code = not self.code

    # Feeding --------------------------------------------------------------

    def feed(self, text: str) -> str:
        out: list[str] = []
        position = 0
        size = len(text)
        while position < size:
            if self.state == "text":
                found = SPECIAL.search(text, position)
                end = size if found is None else found.start()
                if end > position:
                    self._plain(out, text[position:end])
                    position = end
                    continue
                char = text[position]
                position += 1
                self._text_char(out, char)
            elif self.state == "quote":
                char = text[position]
                position += 1
                self._quote_char(out, char)
            else:
                char = text[position]
                position += 1
                self._block_char(out, char)
        return "".join(out)

    def finish(self) -> str:
        out: list[str] = []
        if self.state == "quote":
            if self.apostrophe:
                self._close_quote(out, "")
            else:
                self._release(out)
        elif self.state == "block":
            self._close_block(out)
        return "".join(out)

    def _plain(self, out: list[str], run: str) -> None:
        """A run without special characters: one emission, line state updated."""
        visible = run.lstrip(" \t")
        if visible:
            self.indent_only = False
            if len(self.line_head) < 3:
                self.line_head = (self.line_head + visible)[:3]
        self._emit(out, run)

    def _text_char(self, out: list[str], char: str) -> None:
        starts_line = self.indent_only
        self._line(char)
        if (
            self.fence
            or self.code
            or char in "`\n"
            or not self.sources.active()
            or sum(self.counts.values()) >= MAX_QUOTES
        ):
            self._emit(out, char)
            return
        if char == ">" and starts_line and self.blocks:
            self.state = "block"
            self.before = self.tail
            self.held = [char]
            self.held_size = 1
            self.block_break = False
            self.block_spaces = ""
            return
        if char in CLOSERS:
            prior = self.tail[-1:] if self.tail else "\n"
            if char in '"‘' and prior.isalnum():
                self._emit(out, char)
                return
            self.state = "quote"
            self.before = self.tail
            self.closers = CLOSERS[char]
            self.held = [char]
            self.held_size = 1
            self.apostrophe = False
            return
        self._emit(out, char)

    # Inline quotations ---------------------------------------------------

    def _quote_char(self, out: list[str], char: str) -> None:
        if self.apostrophe:
            # "’" closes a quotation only when no letter follows (else: apostrophe).
            self.apostrophe = False
            if not char.isalpha():
                self._close_quote(out, char)
                return
        self.held.append(char)
        self.held_size += 1
        if char in self.closers:
            if char == "’":
                self.apostrophe = True
                return
            self._close_quote(out, "")
            return
        if self.held_size > MAX_QUOTE or (
            char == "\n" and len(self.held) > 1 and self.held[-2] == "\n"
        ):
            self._release(out)

    def _release(self, out: list[str]) -> None:
        """An unclosed or oversized quotation: shown unchanged, never checked."""
        raw = "".join(self.held)
        self.state = "text"
        self.held = []
        self.held_size = 0
        self.apostrophe = False
        for char in raw:
            self._line(char)
        self._emit(out, raw)

    def _close_quote(self, out: list[str], following: str) -> None:
        raw = "".join(self.held)
        content = raw[1:-1]
        self.state = "text"
        self.held = []
        self.held_size = 0
        result = verdict(self.before, content, self.asked, self.sources)
        self.counts[result] += 1
        if result == "not_found" and self.mark:
            self._emit(out, content.strip() + f" *({self.note})*", raw)
        else:
            self._emit(out, raw)
        if following:
            out.append(self.feed(following))

    # Block quotations ----------------------------------------------------

    def _block_char(self, out: list[str], char: str) -> None:
        if self.block_break:
            if char in " \t":
                self.block_spaces += char
                return
            if char == ">":
                self.held.append(self.block_spaces + char)
                self.held_size += len(self.block_spaces) + 1
                self.block_spaces = ""
                self.block_break = False
                return
            spaces = self.block_spaces
            self._close_block(out)
            self.indent_only = True
            self.line_head = ""
            self.code = False
            if spaces:
                self._emit(out, spaces)
            if self.state == "text":
                self._text_char(out, char)
            return
        self.held.append(char)
        self.held_size += 1
        if char == "\n":
            self.block_break = True
            self.block_spaces = ""
        if self.held_size > MAX_BLOCK:
            raw = "".join(self.held)
            self.state = "text"
            self.held = []
            self.held_size = 0
            self.block_break = False
            self._emit(out, raw)

    def _close_block(self, out: list[str]) -> None:
        """A block quotation, paragraph by paragraph.

        A paragraph is checked whole (without a leading bold label such as
        "**Article 3**" or a closing reference such as "(page 2)"); when it is
        not found and holds quotation marks, the quotations inside it are
        checked instead (commentary around quoted words). Only a paragraph that
        is not found loses its block quotation and gets the note.
        """
        raw = "".join(self.held)
        self.state = "text"
        self.held = []
        self.held_size = 0
        self.block_break = False
        ending = "\n" if raw.endswith("\n") else ""
        body = raw[: len(raw) - len(ending)]
        content = "\n".join(_unquoted_line(line) for line in body.split("\n"))
        if _CALLOUT.match(content):
            self.counts["callout"] += 1
            self._emit(out, raw)
            return
        rendered: list[str] = []
        changed = False
        for paragraph in _PARAGRAPH.split(content):
            # Every paragraph of the block is attributed by the text before it.
            text, result = self._paragraph(self.before, paragraph)
            if result:
                self.counts[result] += 1
            if text is None:
                rendered.append(
                    "\n".join("> " + line for line in paragraph.split("\n"))
                )
            else:
                changed = True
                rendered.append(text)
        if not changed:
            self._emit(out, raw)
            return
        self._emit(out, "\n\n".join(rendered) + ending, raw)

    def _paragraph(self, before: str, paragraph: str) -> tuple[str | None, str]:
        """(replacement text or None to keep it quoted, verdict or "")."""
        candidates = [_without_reference(paragraph)]
        label = _LABEL.match(candidates[0])
        if label is not None:
            candidates.append(candidates[0][label.end() :])
        results = [
            verdict(before, text, self.asked, self.sources) for text in candidates
        ]
        for result in ("exact", "ellipsis"):
            if result in results:
                return None, result
        result = results[-1]
        if result == "short":
            return None, ""
        if result != "not_found":
            return None, result
        if any(mark in paragraph for mark in CLOSERS if mark != "‘"):
            inner = Rewriter(
                self.sources, asked=self.asked, mark=self.mark, blocks=False
            )
            inner.note = self.note
            inner.tail = before
            rewritten = inner.feed(paragraph) + inner.finish()
            if any(inner.counts[name] for name in ("exact", "ellipsis", "not_found")):
                self.counts.update(inner.counts)
                if rewritten == paragraph:
                    return None, ""
                return "\n".join("> " + line for line in rewritten.split("\n")), ""
        if not self.mark:
            return None, result
        return paragraph.strip() + f"\n\n*({self.note})*", result

    def report(self) -> dict[str, int]:
        return dict(self.counts)


def _unquoted_line(line: str) -> str:
    text = line.lstrip(" \t")
    if text.startswith(">"):
        text = text[1:]
        if text.startswith(" "):
            text = text[1:]
    return text


def _without_reference(content: str) -> str:
    """A block quotation without its closing reference line or parenthesis."""
    lines = content.rstrip().split("\n")
    if len(lines) > 1 and lines[-1].lstrip().startswith(("—", "–", "-- ", "Source")):
        lines = lines[:-1]
    text = "\n".join(lines).rstrip()
    if text.endswith(")") and not text.endswith(("(…)", "(...)", "(..)")):
        opening = text.rfind("(")
        if opening >= 0 and len(text) - opening <= 80:
            text = text[:opening].rstrip()
    return text


current: ContextVar[Sources | None] = ContextVar("document_quotes", default=None)


def add_document(text: object, name: object = "") -> None:
    """Record a document text the turn's answer may quote."""
    if (sources := current.get()) is not None:
        sources.add_document(text, name)


def observe(output: dict[str, object]) -> dict[str, object]:
    """Record a tool result as non-document evidence and return it unchanged."""
    if (sources := current.get()) is not None:
        sources.collect(output)
    return output


Sink = Callable[[dict[str, object]], Awaitable[None]]


class Gate:
    """Streams answer text through a Rewriter; every other event passes."""

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
        await self.sink(event)

    async def flush(self) -> None:
        text = self.rewriter.finish()
        if text:
            await self.sink({"delta": {"content": text}})


def rewriter(sources: Sources, language: str, question: str) -> Rewriter:
    return Rewriter(
        sources,
        language=language,
        asked=asked_for_quotes(question),
        mark=mode() == "mark",
    )


def checked(
    sources: Sources, answer: str, language: str, question: str
) -> tuple[str, dict[str, int]]:
    """The complete answer with its quotations checked, and the counts."""
    pass_ = rewriter(sources, language, question)
    text = pass_.feed(answer) + pass_.finish()
    return text, pass_.report()


def log(counts: dict[str, int], sources: Sources, task_type: str) -> None:
    """Counts only: answer and document text never reach the log."""
    fields = {
        name: counts.get(name, 0)
        for name in (
            "exact",
            "ellipsis",
            "not_found",
            "elsewhere",
            "short",
            "unattributed",
            "own_wording",
            "external",
            "callout",
        )
    }
    LOGGER.warning(
        "document_quotes %s",
        json.dumps(
            {
                **fields,
                "documents": sources.count(),
                "mode": mode(),
                "task_type": task_type,
            },
            sort_keys=True,
        ),
    )
