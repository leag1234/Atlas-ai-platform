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
    empty_notices = {
        "fr": "Aucun fichier n'a pu être publié et aucun contenu n'a pu être produit pour cette demande.",
        "de": "Keine Datei konnte veröffentlicht werden, und für diese Anfrage konnte kein Inhalt erstellt werden.",
        "es": "No se pudo publicar ningún archivo ni producir contenido para esta solicitud.",
        "it": "Nessun file è stato pubblicato e non è stato possibile produrre contenuti per questa richiesta.",
        "en": "No file was produced for download, and no content could be produced for this request.",
    }
    content = answer_content(text, preserve_code=preserve_code)
    if not content.strip():
        # Never point at content above when there is none.
        return empty_notices[language]
    return content + "\n\n" + (partial_notices if partial else notices)[language]


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


LINK_WORDS = {
    "fr": ("Télécharger", "Aperçu"),
    "de": ("Herunterladen", "Vorschau"),
    "es": ("Descargar", "Vista previa"),
    "it": ("Scarica", "Anteprima"),
}


def project_deliverable_links(
    deliverable: dict[str, object], org: str, project: str, lang: str = "en"
) -> str:
    """Only server-returned, scope-matching canonical routes reach the transcript."""
    return " · ".join(
        link for _, link in _deliverable_links(deliverable, org, project, lang)
    )


def append_deliverable_links(
    answer: str,
    deliverable: dict[str, object],
    org: str,
    project: str,
    lang: str = "en",
) -> str:
    """Add the links of a saved file to an answer, each route once.

    The model reads the same routes in the tool result and often repeats the
    download link in its own sentence; adding the full pair after it showed two
    download links for one file (staging 2026-11-12). A route the answer
    already links is not added again, so the preview stays one click away
    without a second download link.
    """
    links = _deliverable_links(deliverable, org, project, lang)
    # A verbatim copy of the tool result carries its English labels.
    answer = answer.replace(
        project_deliverable_links(deliverable, org, project),
        " · ".join(link for _, link in links),
    )
    for route, _ in links:
        # A model may put an invented host before a relative UI route.
        answer = re.sub(
            r"\]\(\s*(?:https?://[^/\s)]+)?" + re.escape(route) + r"\s*\)",
            # The route is made of UUIDs and fixed words: safe as a replacement.
            f"]({route})",
            answer,
        )
    missing = [link for route, link in links if f"]({route})" not in answer]
    return answer + "\n\n" + " · ".join(missing) if missing else answer


def _deliverable_links(
    deliverable: dict[str, object], org: str, project: str, lang: str
) -> tuple[tuple[str, str], tuple[str, str]]:
    """The verified (route, markdown link) pairs of a file: download, then preview."""
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
    download, preview = LINK_WORDS.get(lang, ("Download", "Preview"))
    # Name the file: a Work turn may save several, and a code file is not a "document".
    name = str(
        deliverable.get("relative_path") or deliverable.get("name") or ""
    ).strip()
    label = f"{download} {name}" if name else download
    label = label.replace("[", "(").replace("]", ")")
    return (
        (f"{prefix}/download", f"[{label}]({prefix}/download)"),
        (f"{prefix}/preview", f"[{preview}]({prefix}/preview)"),
    )
