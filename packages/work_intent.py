"""Which plain-chat turns need Work behind them.

The chat answers by itself whenever it can. A turn goes to Work only when the
answer needs something the chat cannot do: running code or tests, producing a
real file (Word, Excel, PowerPoint, PDF, CSV), or building a program on a
runtime other than a single web page. Precision comes first: an ordinary
question that merely contains "test", "code" or "application" stays in the chat.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Literal

Reason = Literal["run", "file", "build", "remote", "follow_up"]

_ATTACHMENT_TAG = re.compile(
    r"\[attachment: [^\n]*\]|<attached_files>.*?</attached_files>", re.S | re.I
)

# "How do I…", "explain…", "what is…": a question about the subject, not a request to do it.
_HOW_TO = re.compile(
    r"\b(?:comment (?:faire|creer|ecrire|lancer|executer|generer|on|puis|est|je|se|me|s')|how (?:do|does|can|to|would|should)"
    r"|explique\w*|explain\w*|pourquoi|why|c'est quoi|qu'est[- ]ce|what is|what are|what does"
    r"|difference|quelle est|quel est|wie kann|wie macht|warum|erklar\w*|como (?:se|puedo|hago)"
    r"|por que|come (?:si|posso)|perche|spiega\w*)\b"
)

_RUN_VERB = (
    r"(?:execute[sz]?|executer|lance[sz]?|lancer|fais tourner|faire tourner|teste[sz]?|tester"
    r"|run|runs|running|test|ejecuta\w*|prueba|fuhre|ausfuhr\w*|esegui\w*|prova)"
)
_CODE_OBJECT = (
    r"(?:scripts?|tests? unitaires?|unit tests?|(?:les|the|mes|my|ces|these) tests|pytest|unittest"
    r"|(?:ce|le|this|the|mon|my) (?:code|programme|program)|code python|python|bash|commande shell"
    r"|shell command|notebook|requete sql|sql query|benchmark)"
)
_RUN = re.compile(
    rf"\b{_RUN_VERB}\b.{{0,60}}\b{_CODE_OBJECT}\b"
    # "…et lance-le", "…and run it"
    rf"|\b(?:et|puis|and|then|und|y|e)\s+{_RUN_VERB}[- ]?(?:le|la|les|it|them|lo|es|sie)\b"
    # "make sure the tests pass", "vérifie que pytest passe"
    r"|\b(?:tests?|pytest|unittest)\b.{0,30}\b(?:pass(?:es|ent|e)?|reussi\w*|green|verts?)\b"
)

_CREATE_VERB = (
    r"(?:cree[sz]?|creer|genere[sz]?|generer|fais|faites|faire|redige[sz]?|rediger|ecris|ecrivez|ecrire"
    r"|prepare[sz]?|preparer|produis|produire|exporte[sz]?|exporter|fabrique[sz]?|etabli[sr]\w*"
    r"|donne[sz]?[- ]moi|fournis\w*|code[sz]?|coder|developpe[sz]?|developper|programme[sz]?|programmer"
    r"|construis|construire|make|create|generate|write|prepare|produce|export|build|draft|code"
    r"|develop|implement|give me|erstelle?|schreibe?|generiere|baue|crea|genera|escribe|prepara"
    r"|haz|redacta|scrivi|costruisci)"
)
_FILE_KIND = (
    r"(?:(?:document|fichier|format|lettre|rapport|courrier|file|datei|documento|archivo)\s+(?:word|excel|powerpoint|pdf|csv)"
    r"|(?:word|excel|powerpoint|pdf|csv)\s+(?:document|file|spreadsheet|sheet|workbook|presentation|datei|dokument)"
    r"|(?:tableau|classeur|feuille|tableur)\s+excel|presentation\s+powerpoint"
    r"|au format (?:word|excel|powerpoint|pdf|csv)|en (?:word|pdf|excel|powerpoint)"
    r"|(?:in|as|im|als|en|in formato)\s+(?:a\s+)?(?:word|pdf|excel|powerpoint)(?:\s+format)?"
    r"|docx|xlsx|pptx|powerpoint)"
)
# "un PDF", "une note Word", "an Excel": the bare kind right after the verb's article,
# with at most one noun between them ("fais un résumé du PDF" is about a PDF, not a request for one).
_ARTICLE = (
    r"(?:un|une|le|la|ce|cette|mon|ma|a|an|the|this|my|ein|eine|einen|el|il|uno|una)"
)
_BARE_KIND = (
    rf"(?:[- ](?:moi|nous|me|us|mir))?\s+{_ARTICLE}\s+(?:(?!(?:de|du|des|of|from|sur|about)\b)[\w'-]+\s+)?"
    r"(?:word|excel|pdf|powerpoint|csv)\b"
)
_FILE = re.compile(
    rf"\b{_CREATE_VERB}\b.{{0,160}}?(?:(?<![\w.]){_FILE_KIND}\b|[\w-]+\.(?:docx|xlsx|pptx|pdf|csv)\b)"
    rf"|\b{_CREATE_VERB}{_BARE_KIND}"
)

# A program that is more than one web page: a named non-web runtime or a server-side thing.
_PROGRAM = r"(?:jeu|game|spiel|juego|gioco|app|application|api|bot|serveur|server|backend|site|outil|tool|cli|scraper|projet|project|service)"
_RUNTIME = (
    r"(?:python|pygame|tkinter|flask|django|fastapi|node(?:\.?js)?|express|rust|golang|java|kotlin"
    r"|c\+\+|c#|php|ruby|postgres\w*|sqlite|mysql|docker\w*|discord|telegram|base de donnees|database|api rest|rest api)"
)
_BUILD = re.compile(
    rf"\b{_CREATE_VERB}\b.{{0,80}}?\b{_PROGRAM}\b.{{0,80}}?(?<!\w){_RUNTIME}(?!\w)"
    rf"|\b{_CREATE_VERB}\b.{{0,80}}?(?<!\w){_RUNTIME}(?!\w).{{0,40}}?\b{_PROGRAM}\b"
    r"|\b(?:scrape[sz]?|scraper|crawle?[sz]?)\b.{0,40}\b(?:site|page|url|website)\b"
)

# Work on one of the person's own machines, over SSH (owner decision 2026-11-11: the chat
# reaches the machines a member declared, like a terminal assistant would).
_MACHINE = r"(?:serveurs?|servers?|vps|machines?|hosts?|box)"
_REMOTE = re.compile(
    rf"\b(?:connecte[sz]?(?:[- ](?:toi|vous))?|connect|log ?in|ssh)\b.{{0,40}}\b{_MACHINE}\b"
    rf"|\b(?:sur|on|dans|depuis|from)\s+(?:mon|ma|mes|my|notre|our)\s+{_MACHINE}\b"
    r"|\b(?:en|via|par|over|through|with)\s+ssh\b"
)

# Removing, forgetting or disconnecting one of the person's machines is done by
# the machine tools: answered by the chat alone, "forget my laptop" (in French)
# got "noted" and nothing was removed (staging 2026-11-14). The request opens
# the message (an imperative, possibly after "please" or "can you"), so "I left
# my laptop on the train" is not one. Stems and optional letters admit the
# usual typos ("suprime", "forgt", "deconecte").
_MANAGE = re.compile(
    r"^\W*(?:(?:please|pls|stp|svp|bitte|por favor|per favore|peux[- ]tu|pouvez[- ]vous"
    r"|tu peux|can you|could you|kannst du|konnen sie|puedes|puoi)\W+)?"
    r"(?:oubli\w*|forg[eo]?t\w*|remov\w*|delet\w*|disc?onn?ect\w*|unlink\w*"
    r"|deconn?ect\w*|retir\w*|supp?r?im\w*|enlev\w*|vergi?ss\w*|entfern\w*|trenn\w*"
    r"|loe?sch\w*|olvid\w*|elimin\w*|borr\w*|desconn?ect\w*|dimentic\w*|rimuov\w*"
    r"|scolleg\w*)"
    r"\W+(?:\w+\W+){0,3}?"
    r"(?:ordi\w*|portab\w*|portat\w*|lap?to?p\w*|labtop\w*|notebook\w*|comput\w*"
    r"|compt\w*|pc|macbook\w*|mac|imac|machin\w*|macchin\w*|maschin\w*|rechner\w*"
    r"|serv(?:er|eur|idor)\w*|vps|nas|desktop\w*|ordenador\w*)\b"
)
# What a document edit names instead ("delete the server section of my CV").
_NOT_A_MACHINE = re.compile(
    r"\b(?:cv|resume|documents?|fichiers?|files?|sections?|paragraph\w*|slides?|photos?"
    r"|images?|e?mails?|messages?|textes?|text|tableau|table|charts?|graph\w*|lignes?|lines?)\b"
)


def manages_machine(text: str) -> bool:
    """A short request to remove, forget or disconnect one of the person's machines."""
    normalized = _normalized(_ATTACHMENT_TAG.sub(" ", text)).strip()
    return (
        len(normalized.split()) <= 10
        and _MANAGE.search(normalized) is not None
        and _NOT_A_MACHINE.search(normalized) is None
    )


# Later turns of a conversation that already has its Work session: a change or a rerun stays there.
_FOLLOW_UP = re.compile(
    r"\b(?:modifi\w*|ajout\w*|corrig\w*|chang\w*|remplac\w*|supprim\w*|enlev\w*|amelior\w*|refai[st]\w*"
    r"|relanc\w*|reexecut\w*|mets|mettre|renomm\w*|complet\w*|continu\w*|update\w*|add|fix\w*|change\w*"
    r"|replace\w*|remove\w*|improve\w*|redo|rerun|rename\w*|andere|fuge|korrigier\w*|anade|cambia\w*"
    r"|aggiungi|correggi)\b"
)


def _normalized(text: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(character)
    )


def work_reason(
    text: str, history: Sequence[Mapping[str, object]] = ()
) -> Reason | None:
    """Why this plain-chat turn needs Work, or None when the chat answers it."""
    normalized = _normalized(_ATTACHMENT_TAG.sub(" ", text))
    if not normalized.strip() or _HOW_TO.search(normalized):
        return None
    if _REMOTE.search(normalized) or manages_machine(normalized):
        return "remote"
    if _RUN.search(normalized):
        return "run"
    if _FILE.search(normalized):
        return "file"
    if _BUILD.search(normalized):
        return "build"
    return None


def needs_work(text: str, history: Sequence[Mapping[str, object]] = ()) -> bool:
    return work_reason(text, history) is not None


def continues_work(text: str) -> bool:
    """A change or rerun request, for a conversation that already has a Work session."""
    normalized = _normalized(_ATTACHMENT_TAG.sub(" ", text))
    return bool(normalized.strip()) and (
        work_reason(text) is not None
        or (
            _FOLLOW_UP.search(normalized) is not None and not _HOW_TO.search(normalized)
        )
    )


_OFFICE_KINDS = (
    ("docx", re.compile(r"\bdocx\b|\bword\b")),
    ("xlsx", re.compile(r"\bxlsx\b|\bexcel\b")),
    ("pptx", re.compile(r"\bpptx\b|\bpowerpoint\b")),
    ("pdf", re.compile(r"\bpdf\b")),
)


def named_office_format(text: str) -> str | None:
    """The one office format a file request names ("a Word note" is a .docx).

    None when the turn is not a file request or names several formats: only an
    unambiguous request constrains what may be published (a Word note asked in
    the chat came back as Markdown, staging 2026-11-11).
    """
    if work_reason(text) != "file":
        return None
    normalized = _normalized(_ATTACHMENT_TAG.sub(" ", text))
    named = [
        extension for extension, pattern in _OFFICE_KINDS if pattern.search(normalized)
    ]
    return named[0] if len(named) == 1 else None
