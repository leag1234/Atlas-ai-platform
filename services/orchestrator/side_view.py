"""Owned side-view transport for verified Open WebUI attachment references."""

from html import escape
from urllib.parse import urlencode

from services.orchestrator.chat_schema import ChatRequest


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
            return (
                '<span class="atlas-view" data-atlas-view="pdf" data-src="'
                + source
                + '" data-title="'
                + escape(attachment.filename, quote=True)
                + '" data-slot="top" hidden></span>\n\n'
                # Open WebUI strips hidden HTML spans. Markdown links use the
                # same native Markdown transport as the owned diagram images.
                + f"[Open in side view]({source}#{fragment})\n\n"
            )
    return ""
