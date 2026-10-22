"""Render verified publication URLs and explicit transformation declarations."""

import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import unquote, urlsplit


def inline_fallback(
    text: str, language: str, *, preserve_code: bool = False, partial: bool = False
) -> str:
    notices = {
        "fr": "Aucun fichier n'a pu être publié. Le contenu ci-dessus est la solution de secours en texte.",
        "de": "Keine Datei konnte veröffentlicht werden. Der obige Inhalt dient als Ersatz in Textform.",
        "es": "No se pudo publicar ningún archivo. El contenido anterior es la alternativa en texto.",
        "it": "Nessun file è stato pubblicato. Il contenuto sopra è l'alternativa in testo.",
        "en": "No file was produced for download. The content above is the inline fallback.",
    }
    from services.orchestrator.file_content import answer_content

    partial_notices = {
        "fr": "Certains fichiers demandés n'ont pas pu être publiés. Leur contenu est fourni ci-dessus.",
        "de": "Einige angeforderte Dateien konnten nicht veröffentlicht werden. Ihr Inhalt steht oben.",
        "es": "No se pudieron publicar algunos archivos solicitados. Su contenido aparece arriba.",
        "it": "Alcuni file richiesti non sono stati pubblicati. Il loro contenuto è riportato sopra.",
        "en": "Some requested files could not be published. Their content is provided above.",
    }
    return (
        answer_content(text, preserve_code=preserve_code)
        + "\n\n"
        + (partial_notices if partial else notices)[language]
    )


def render_files(
    text: str, files: list[str], strategies: list[str], language: str
) -> str:
    return canonical_links(text, files) + file_footer(files, strategies, language)


def file_footer(files: list[str], strategies: list[str], language: str) -> str:
    labels = json.loads(Path("prompts/file-strategies.json").read_text())
    localized = labels.get(language, labels["en"])
    text = ""
    for url, strategy in zip(files, strategies, strict=True):
        filename = unquote(PurePosixPath(url).name)
        text += f"\n\n[{filename}]({url}) — {localized[strategy]}."
    return text


def canonical_links(text: str, files: list[str]) -> str:
    for url in files:
        filename = unquote(PurePosixPath(url).name)

        def canonical(match: re.Match[str]) -> str:
            target = unquote(PurePosixPath(urlsplit(match[2]).path).name)
            return f"[{match[1]}]({url})" if target == filename else match[0]

        # A model may turn a relative UI URL into an invented external host.
        text = re.sub(r"\[([^\]]+)\]\(([^\s)]+)\)", canonical, text)
    return text


def project_deliverable_links(
    deliverable: dict[str, object], org: str, project: str
) -> str:
    """Only server-returned, scope-matching canonical routes reach the transcript."""
    from uuid import UUID

    identifier = str(UUID(str(deliverable["id"])))
    prefix = f"/api/atlas/orgs/{str(UUID(org))}/projects/{str(UUID(project))}/deliverables/{identifier}"
    validation = deliverable.get("validation")
    if (
        deliverable.get("org_id") != org
        or deliverable.get("project_id") != project
        or deliverable.get("download_route") != prefix + "/download"
        or not isinstance(validation, dict)
        or validation.get("status") != "passed"
        or validation.get("verifier_exit") != 0
        or validation.get("artifact_sha256") != deliverable.get("sha256")
    ):
        raise ValueError("Unverified or foreign deliverable response")
    return f"[Download document]({prefix}/download) · [Preview]({prefix}/preview)"
