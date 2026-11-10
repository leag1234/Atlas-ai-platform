"""Declaration and execution of the terminal HTML artefact tool."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.artifact_html import quote_attributes, validate
from services.orchestrator import artifact_store
from services.orchestrator.artifact_store import ArtifactError
from services.orchestrator.loop import Call, Message


class CreateArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[^\n\r\[\]]+$")]
    filename: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,60}\.html$")]
    content: str = Field(min_length=1)
    summary: Annotated[str, Field(min_length=1, max_length=600)]
    artifact_id: Annotated[
        str | None, Field(default=None, pattern=r"^[0-9a-f]{32}$")
    ] = None


DESCRIPTION = (
    "Deliver a small interactive web app as one self-contained HTML file that runs "
    "in the Atlas preview. Inline all CSS and JavaScript; no external URL or network "
    "access. To change the current app, pass its artifact_id and the complete new file."
)

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
MAX_PROBLEMS = 10
MAX_DETAIL_CHARS = 80
PROBLEM_DETAILS = {
    "dialog_api": "show the message inside the page",
    "script_syntax": "this script does not parse (check braces, brackets, "
    "parentheses and quotes); send the complete corrected file",
}


def declarations() -> list[Message]:
    return [
        {
            "type": "function",
            "function": {
                "name": "create_artifact",
                "description": DESCRIPTION,
                "parameters": CreateArtifact.model_json_schema(),
            },
        }
    ]


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
    "Current app (update it with create_artifact and its artifact_id): "
)


def parse_history_reference(messages: list[Message]) -> ArtifactReference | None:
    reference: ArtifactReference | None = None
    for message in messages[:-1]:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        for match in HISTORY_REFERENCE_PATTERN.finditer(content):
            title_match = re.search(
                rf"\[([^\]]+)\]\({re.escape(match.group(0))}", content
            )
            title = title_match.group(1) if title_match else ""
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
    return CurrentArtifact(
        {
            "role": "system",
            "content": CURRENT_ARTIFACT_PREFIX
            + json.dumps({"current_artifact": artifact}, ensure_ascii=False),
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
) -> str:
    labels = FOOTER_LABELS.get(language, FOOTER_LABELS["en"])
    prefix = f"/api/atlas/artifacts/{artifact_id}/versions/{version}"
    fragment = quote(
        f"atlas-view=app&title={title}&artifact={artifact_id}&version={version}&slot=top",
        safe="=&",
    )
    open_label = labels["open"].format(title=title, version=version)
    download_label = labels["download"].format(filename=filename)
    text = (
        f"[{_markdown_escape(open_label)}]({prefix}/open#{fragment}) · "
        f"[{_markdown_escape(download_label)}]({prefix}/download)"
    )
    if version == 1:
        text += f"\n\n{labels['temporary']}"
    return text


def unchanged_footer(version: int, language: str) -> str:
    labels = FOOTER_LABELS.get(language, FOOTER_LABELS["en"])
    return labels["unchanged"].format(version=version)


class ArtifactToolRuntime:
    """Per-turn state for the terminal artefact tool."""

    def __init__(
        self,
        identity: str | None,
        request_id: str,
        current_artifact_id: str | None = None,
        current_version: int | None = None,
    ) -> None:
        self.identity = identity
        self.request_id = request_id
        self.current_artifact_id = current_artifact_id
        self.current_version = current_version
        self.calls = 0
        self.failures = 0
        self.offered = True
        self.forced = True

    def _failure(self, error: str, **details: object) -> Message:
        self.failures += 1
        if self.failures >= 2:
            self.offered = False
            return {
                "error": "artifact_rejected",
                "detail": "explain in one or two sentences why the app could not be produced",
            }
        return {"error": error, **details}

    async def execute(self, call: Call, timeout: float) -> Message:
        if not self.offered or self.calls >= 2:
            return self._failure("artifact_rejected")
        self.calls += 1
        self.forced = False
        try:
            arguments = CreateArtifact.model_validate_json(call.arguments)
        except ValidationError as error:
            return self._failure(
                "artifact_invalid",
                problems=[
                    {"code": "invalid_arguments", "line": 1, "detail": str(error)[:80]}
                ],
            )
        if arguments.artifact_id != self.current_artifact_id:
            return self._failure("artifact_unknown")
        arguments.content = quote_attributes(arguments.content)
        problems = validate(arguments.content)
        if problems:
            return self._failure(
                "artifact_invalid",
                problems=[
                    {
                        "code": problem.code,
                        "line": problem.line,
                        "detail": PROBLEM_DETAILS.get(
                            problem.code,
                            arguments.content.splitlines()[problem.line - 1][
                                :MAX_DETAIL_CHARS
                            ]
                            if problem.line <= len(arguments.content.splitlines())
                            else "",
                        ),
                    }
                    for problem in problems[:MAX_PROBLEMS]
                ],
            )
        content = arguments.content.encode("utf-8")
        try:
            if arguments.artifact_id is None:
                result = artifact_store.create(
                    self.identity,
                    arguments.title,
                    arguments.filename,
                    content,
                    self.request_id,
                )
            else:
                result = artifact_store.update(
                    arguments.artifact_id,
                    self.identity,
                    arguments.title,
                    arguments.filename,
                    content,
                    self.request_id,
                    self.current_version,
                )
        except ArtifactError as error:
            code = {
                "artifact_owner_required": "artifact_unknown",
                "artifact_invalid": "artifact_invalid",
                "artifact_not_found": "artifact_unknown",
                "artifact_conflict": "artifact_unknown",
                "artifact_quota": "artifact_quota",
                "artifact_store_unavailable": "artifact_unavailable",
                "artifact_unavailable": "artifact_unavailable",
            }.get(error.code, "artifact_unavailable")
            return self._failure(code)
        artifact_id = str(result["id"])
        version = int(result["version"])
        self.current_artifact_id = artifact_id
        self.current_version = version
        return {
            "ok": True,
            "artifact_id": artifact_id,
            "version": version,
            "bytes": len(content),
        }


def compose_answer(
    prose: str,
    summary: str,
    artifact_id: str,
    version: int,
    title: str,
    filename: str,
    language: str,
) -> str:
    cleaned = clean_summary(summary)
    parts = [part for part in (prose, cleaned) if part]
    parts.append(footer(artifact_id, version, title, filename, language))
    return "\n\n".join(parts)


def tool_message(content: Message) -> Message:
    return {
        "role": "tool",
        "tool_call_id": "",
        "content": json.dumps(content, ensure_ascii=False),
    }
