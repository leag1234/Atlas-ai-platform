"""Current app of a conversation, and the answer footer that links to it.

The app or drawing itself is written by the model in its answer and stored by
services/orchestrator/app_turn.py; no tool carries it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

from services.orchestrator import artifact_store
from services.orchestrator.artifact_store import ArtifactError
from services.orchestrator.loop import Message


FOOTER_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "open": 'Open "{title}" (version {version})',
        "download": "Download {filename}",
        "temporary": "Temporary preview: data entered in the app is not kept.",
        "unchanged": "The change could not be applied; version {version} is unchanged.",
    },
    "fr": {
        "open": "Ouvrir « {title} » (version {version})",
        "download": "Télécharger {filename}",
        "temporary": (
            "Aperçu temporaire : les données saisies dans l'application "
            "ne sont pas conservées."
        ),
        "unchanged": (
            "La modification n'a pas pu être appliquée ; la version {version} "
            "reste inchangée."
        ),
    },
    "de": {
        "open": "„{title}“ öffnen (Version {version})",
        "download": "{filename} herunterladen",
        "temporary": (
            "Temporäre Vorschau: In der App eingegebene Daten werden nicht gespeichert."
        ),
        "unchanged": (
            "Die Änderung konnte nicht übernommen werden; Version {version} "
            "bleibt unverändert."
        ),
    },
    "es": {
        "open": "Abrir «{title}» (versión {version})",
        "download": "Descargar {filename}",
        "temporary": (
            "Vista previa temporal: los datos introducidos en la aplicación "
            "no se conservan."
        ),
        "unchanged": ("No se pudo aplicar el cambio; la versión {version} no cambia."),
    },
    "it": {
        "open": "Apri «{title}» (versione {version})",
        "download": "Scarica {filename}",
        "temporary": (
            "Anteprima temporanea: i dati inseriti nell'app non vengono conservati."
        ),
        "unchanged": (
            "Non è stato possibile applicare la modifica; la versione {version} "
            "resta invariata."
        ),
    },
}

HISTORY_REFERENCE_PATTERN = re.compile(
    r"/api/atlas/artifacts/([0-9a-f]{32})/versions/([1-9][0-9]{0,3})/open"
)
MAX_CURRENT_ARTIFACT_BYTES = 96 * 1024
PROBLEM_DETAILS = {
    "dialog_api": "show the message inside the page",
    "script_syntax": "this script does not parse (check braces, brackets, "
    "parentheses and quotes); send the complete corrected file",
}


@dataclass(frozen=True)
class ArtifactReference:
    artifact_id: str
    version: int
    title: str


@dataclass(frozen=True)
class CurrentArtifact:
    message: Message
    artifact: dict[str, object] | None


CURRENT_ARTIFACT_PREFIX = (
    "Current app of this conversation (context only; Atlas applies changes to it): "
)
_FRAGMENT_TITLE = re.compile(r"#[^)\s]*?\btitle=([^&)\s]*)")


def parse_history_reference(messages: list[Message]) -> ArtifactReference | None:
    reference: ArtifactReference | None = None
    for message in messages[:-1]:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        for match in HISTORY_REFERENCE_PATTERN.finditer(content):
            # The footer link carries the stored title in its fragment; older
            # answers only have the link label.
            fragment = _FRAGMENT_TITLE.match(content, match.end())
            title_match = re.search(
                rf"\[([^\]]+)\]\({re.escape(match.group(0))}", content
            )
            title = (
                unquote(fragment.group(1))
                if fragment
                else title_match.group(1)
                if title_match
                else ""
            )
            reference = ArtifactReference(match.group(1), int(match.group(2)), title)
    return reference


def current_artifact_message(
    messages: list[Message], identity: str | None
) -> CurrentArtifact | None:
    reference = parse_history_reference(messages)
    if reference is None:
        return None
    try:
        version = artifact_store.read(
            reference.artifact_id, identity, reference.version
        )
    except ArtifactError:
        return CurrentArtifact(
            {
                "role": "system",
                "content": json.dumps(
                    {
                        "current_artifact": None,
                        "note": "previous app no longer stored (30-day retention)",
                    },
                    ensure_ascii=False,
                ),
            },
            None,
        )
    artifact: dict[str, object] = {
        "artifact_id": version.artifact_id,
        "version": version.version,
        "title": reference.title,
        "filename": version.filename,
    }
    if version.size_bytes <= MAX_CURRENT_ARTIFACT_BYTES:
        artifact["content"] = version.content.decode("utf-8")
    else:
        artifact["note"] = "source too large to include"
    visible = {key: value for key, value in artifact.items() if key != "artifact_id"}
    return CurrentArtifact(
        {
            "role": "system",
            "content": CURRENT_ARTIFACT_PREFIX
            + json.dumps({"current_artifact": visible}, ensure_ascii=False),
        },
        artifact,
    )


def clean_summary(summary: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", summary)
    text = re.sub(r"<[^>]*>", "", text)
    return text.strip()


def _markdown_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def footer(
    artifact_id: str,
    version: int,
    title: str,
    filename: str,
    language: str,
    *,
    temporary: bool = True,
) -> str:
    labels = FOOTER_LABELS.get(language, FOOTER_LABELS["en"])
    prefix = f"/api/atlas/artifacts/{artifact_id}/versions/{version}"
    fragment = (
        f"atlas-view=app&title={quote(title, safe='')}"
        f"&artifact={artifact_id}&version={version}&slot=top"
    )
    open_label = labels["open"].format(title=title, version=version)
    download_label = labels["download"].format(filename=filename)
    text = (
        f"[{_markdown_escape(open_label)}]({prefix}/open#{fragment}) · "
        f"[{_markdown_escape(download_label)}]({prefix}/download)"
    )
    if version == 1 and temporary:
        text += f"\n\n{labels['temporary']}"
    return text


def unchanged_footer(version: int, language: str) -> str:
    labels = FOOTER_LABELS.get(language, FOOTER_LABELS["en"])
    return labels["unchanged"].format(version=version)


def compose_answer(
    prose: str,
    summary: str,
    artifact_id: str,
    version: int,
    title: str,
    filename: str,
    language: str,
    *,
    temporary: bool = True,
) -> str:
    cleaned = clean_summary(summary)
    parts = [part for part in (prose, cleaned) if part]
    parts.append(
        footer(artifact_id, version, title, filename, language, temporary=temporary)
    )
    return "\n\n".join(parts)
