"""A Work turn asked for a file publishes it, or says plainly that it did not.

Staging battery 2026-11-20: a project Work turn asked for a five-slide PowerPoint
file answered "the PowerPoint presentation was generated successfully ... the
file contains 5 slides" with the slide texts, and no deliverable existed. The
final answer of a Work turn was accepted whatever the turn had published: only a
requested run held it (work_execution.py), and the closing instruction applies
only after a publication (work_closing.py).

T8 (code does and checks, the model decides): whether the requested file exists
is a fact the code holds, the receipts of this turn's publications
(tools.deliverables: a receipt is added only once publish_document stored and
verified the file, of the format the request names). Like a requested run:

- the first final answer while the file is still missing goes back to the model
  once, with the fact (no file was published in this turn) and what to do:
  publish it with publish_document, or say plainly that it was not created;
- if the turn still ends without the file, the answer starts with a notice
  written by code, in the person's language: the file was not created, and why
  (no publication was made, or the publication failed); the lines of the answer
  that announce a file are left out (file_content.answer_content), and the
  content stays as an inline fallback.

The model's words are never searched to decide: the notice depends only on the
receipts and on the publication errors of the turn.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

# The tool that delivers a requested file (work_closing.PUBLISHING).
PUBLISHING = "publish_document"
# Set on the request when a Work turn ended without its requested file: the
# chat's plan then closes as stopped, its delivery step not done (chat_work.py).
MISSING_KEY = "work_file_missing"

NOT_CREATED = {
    "fr": "**Le fichier demandé{name} n'a pas été créé** : {why}. Aucun fichier n'est disponible au téléchargement ; le contenu ci-dessous n'existe que dans cette réponse.",
    "en": "**The requested file{name} was not created**: {why}. No file is available for download; the content below exists only in this answer.",
    "de": "**Die angeforderte Datei{name} wurde nicht erstellt**: {why}. Keine Datei steht zum Herunterladen bereit; der folgende Inhalt existiert nur in dieser Antwort.",
    "es": "**El archivo solicitado{name} no se ha creado**: {why}. No hay ningún archivo para descargar; el contenido siguiente solo existe en esta respuesta.",
    "it": "**Il file richiesto{name} non è stato creato**: {why}. Nessun file è disponibile per il download; il contenuto seguente esiste solo in questa risposta.",
}
WHY = {
    "fr": {
        "not_published": "aucune publication de fichier n'a été faite pendant ce tour",
        "failed": "sa publication a échoué pendant ce tour",
    },
    "en": {
        "not_published": "no file was published in this turn",
        "failed": "its publication failed in this turn",
    },
    "de": {
        "not_published": "in diesem Durchgang wurde keine Datei veröffentlicht",
        "failed": "ihre Veröffentlichung ist in diesem Durchgang fehlgeschlagen",
    },
    "es": {
        "not_published": "no se publicó ningún archivo en este turno",
        "failed": "su publicación falló en este turno",
    },
    "it": {
        "not_published": "in questo turno non è stato pubblicato alcun file",
        "failed": "la sua pubblicazione non è riuscita in questo turno",
    },
}
# Only the content is kept when the answer was nothing but announcements.
EMPTY = {
    "fr": "Aucun contenu n'a été produit pour cette demande.",
    "en": "No content was produced for this request.",
    "de": "Für diese Anfrage wurde kein Inhalt erstellt.",
    "es": "No se produjo ningún contenido para esta solicitud.",
    "it": "Per questa richiesta non è stato prodotto alcun contenuto.",
}

# A file name the request gives (presentation.pptx): named in the notice.
_FILE_NAME = re.compile(
    r"(?<![\w/.-])([\w-][\w.-]{0,80}\.(?:pptx|docx|xlsx|pdf|csv|md|txt|html))\b", re.I
)


def file_instruction(suffix: str | None, failure: str = "") -> str:
    """What the model is told, once, with a final answer and no file."""
    kind = f"a {suffix.lstrip('.').upper()} file" if suffix else "a file"
    fact = (
        f"The publication of the file failed ({failure[:300]})."
        if failure
        else "publish_document was not called successfully in this turn."
    )
    return (
        f"The user asked for {kind}, and no file has been published in this turn: "
        f"{fact} Nothing was created, generated or saved, and the user has no file. "
        "Create it now with publish_document (call switch_tool_group first if "
        "another group is selected), then answer. If it cannot be created, say "
        "plainly, in the language of the user's message, that the file was not "
        "created and why, and give its content in the answer. Never say that a "
        "file was created, generated or saved unless publish_document returned it."
    )


def requested_name(question: str) -> str:
    """The file name the request gives, or an empty string."""
    found = _FILE_NAME.search(question)
    return found.group(1) if found else ""


def file_guard(tools: Any, model: Any) -> DeliverableGuard | None:
    """The guard of a Work turn that asked for a file, or None."""
    if getattr(model, "produces_files", False) is not True:
        return None
    pending = getattr(getattr(tools, "selection", None), "deliverable_pending", None)
    if not callable(pending):
        return None
    failures = getattr(tools, "validation_failures", None)
    return DeliverableGuard(
        lambda: bool(pending()),
        getattr(tools, "requested_suffix", None),
        lambda: failures if isinstance(failures, dict) else {},
    )


class DeliverableGuard:
    """Holds the final answer of a file request until the file is published."""

    def __init__(
        self,
        pending: Callable[[], bool],
        suffix: str | None,
        failures: Callable[[], dict[str, str]] = dict,
    ) -> None:
        self.pending = pending
        self.suffix = suffix
        self.failures = failures
        self.reminded = False

    def failure(self) -> str:
        return str(self.failures().get(PUBLISHING) or "")

    def next_step(self) -> str | None:
        """The instruction to send back with a final answer, once, or None."""
        if self.reminded or not self.pending():
            return None
        self.reminded = True
        return file_instruction(self.suffix, self.failure())

    @property
    def missing(self) -> bool:
        return self.pending()

    def answer(self, text: str, lang: str, question: str) -> tuple[str, bool]:
        """The final answer, led by the not-created notice when the file is missing."""
        if not self.missing:
            return text, False
        from services.orchestrator.file_content import answer_content

        language = lang if lang in NOT_CREATED else "en"
        name = requested_name(question)
        notice = NOT_CREATED[language].format(
            name=f" ({name})" if name else "",
            why=WHY[language]["failed" if self.failure() else "not_published"],
        )
        content = answer_content(text, preserve_code=True)
        return "\n\n".join((notice, content.strip() or EMPTY[language])), True
