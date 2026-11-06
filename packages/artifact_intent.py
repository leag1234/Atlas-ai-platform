"""Classification of chat turns for interactive app artefacts."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Literal


Intent = Literal["create", "modify", "available"]


_ARTIFACT_LINK = re.compile(
    r"/api/atlas/artifacts/[0-9a-f]{32}/versions/[1-9][0-9]{0,3}/open"
)
_NON_WEB = re.compile(
    r"\b(?:python|pygame|tkinter|java|kotlin|swift|c\+\+|c#|rust|golang|go"
    r"|unity|godot|android|ios|electron|flask|django|node server|backend)\b"
)
_EXPLANATION = re.compile(
    r"\b(?:comment|how|wie|como|come)\b.{0,40}\b(?:faire|make|machen|hacer|fare)\b"
    r"|\b(?:explique|explain|why|pourquoi|compare|erklare|spieg\w*|explica|perche|perche)\b"
)
_OFFICE = re.compile(r"\b(?:pdf|docx|xlsx|pptx|csv)\b")
_ATTACHMENT_OR_IMAGE = re.compile(
    r"\[attachment:|<attached_files>|\b(?:attachment|attached|piece jointe"
    r"|anhang|adjunto|allegato|image|imagen|immagine|bild|photo|foto)\b"
)
_DRAWING = re.compile(r"\b(?:dessine\w*|draw|zeichne\w*|dibuja\w*|disegna\w*)\b")
_TITLE_REQUEST = re.compile(r"\b(?:title|titre|titel|titulo|titolo)\b")
_PROJECT_OR_CODE_MODE = re.compile(
    r"\b(?:project|projet|projekt|proyecto|progetto|code mode|mode code"
    r"|api key|cle api|api-schlussel|clave api|chiave api)\b"
)
_QUESTION = re.compile(
    r"^\s*(?:can you|could you|pouvez vous|puedes|potresti|kannst du|why|pourquoi|what|quoi|was|que)\b|\?"
)
_ACTION = re.compile(
    r"\b(?:fais|cree|construis|code|programme|developpe|ecris|genere"
    r"|make|build|create|write|develop|generate"
    r"|erstelle|baue|programmiere|schreibe"
    r"|haz|crea|construye|programa|escribe"
    r"|fai|crea|costruisci|scrivi|programma)\b"
)
_APP = re.compile(
    r"\b(?:jeu|game|spiel|juego|gioco|application|app|outil|tool"
    r"|calculatrice|calculator|rechner|calculadora|calcolatrice"
    r"|minuteur|timer|chronometre|pomodoro|uhr"
    r"|quiz|flashcards|karteikarten|tarjetas|flashcard"
    r"|simulateur|simulator|simulador|simulatore"
    r"|convertisseur|converter|konverter|convertidor|convertitore"
    r"|visualisation|graphique interactif|chart|diagramme interactif"
    r"|interaktive visualisierung|grafico interactivo|grafico interattivo"
    r"|todo list|liste de taches|aufgabenliste|lista de tareas|lista di attivita"
    r"|page web|landing page|webseite|pagina web|pagina web"
    r"|tableau de bord|dashboard"
    r"|snake|tetris|pong|morpion|sudoku|2048|casse briques|demineur)\b"
    r"|\b(?:html|javascript|canvas)\b"
)
_CHANGE = re.compile(
    r"\b(?:change|modifie|ajoute|enleve|retire|remplace|corrige|rends|mets"
    r"|add|remove|replace|fix|make it"
    r"|andere|fuge|entferne|mache"
    r"|cambia|anade|quita|corrige"
    r"|cambia|aggiungi|rimuovi|togli|correggi)\b"
)


def app_intent(text: str, history: Sequence[Mapping[str, object]]) -> Intent | None:
    normalized = _normalized(text)
    if not normalized:
        return None
    if any(
        pattern.search(normalized)
        for pattern in (
            _NON_WEB,
            _EXPLANATION,
            _OFFICE,
            _ATTACHMENT_OR_IMAGE,
            _DRAWING,
            _TITLE_REQUEST,
            _PROJECT_OR_CODE_MODE,
        )
    ):
        return None
    if not _is_app_request(normalized):
        return None
    if _QUESTION.search(normalized):
        return None
    if not _has_artifact(history):
        return "create"
    if _CHANGE.search(normalized) and not _QUESTION.search(normalized):
        return "modify"
    return "available"


def _has_artifact(history: Sequence[Mapping[str, object]]) -> bool:
    for message in history:
        content = message.get("content", "")
        if isinstance(content, str) and _ARTIFACT_LINK.search(content):
            return True
    return False


def _is_app_request(normalized: str) -> bool:
    if _APP.search(normalized):
        return True
    return (
        _ACTION.search(normalized) is not None or _CHANGE.search(normalized) is not None
    )


def _normalized(text: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(character)
    )
