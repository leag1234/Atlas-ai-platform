"""Owned side-view transport for verified Open WebUI attachment references."""

from urllib.parse import urlencode

from services.orchestrator.chat_schema import ChatRequest


OPEN_LABELS = {
    "fr": "Ouvrir dans le panneau latéral",
    "en": "Open in side view",
    "de": "In der Seitenansicht öffnen",
    "es": "Abrir en la vista lateral",
    "it": "Apri nella vista laterale",
}


def pdf_placeholders(request: ChatRequest) -> str:
    """Open the first attached PDF; other files retain their inline links."""
    for attachment in request.documents:
        if (
            not attachment.reused
            and attachment.file_id
            and attachment.filename.lower().endswith(".pdf")
        ):
            source = f"/api/v1/files/{attachment.file_id}/content"
            fragment = urlencode(
                {"atlas-view": "pdf", "title": attachment.filename, "slot": "top"}
            )
            # The Markdown link is the whole transport: chat-ui.js turns it into the
            # side-view chip. A raw HTML marker showed as markup in sidebar previews
            # and notifications.
            label = OPEN_LABELS.get(request.lang, OPEN_LABELS["en"])
            return f"[{label}]({source}#{fragment})\n\n"
    return ""
