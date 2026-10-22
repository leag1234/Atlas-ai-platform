"""Pure HTML-shell injection contract for the owned Open WebUI overlay."""

ATLAS_UI_TAGS = (
    b'<link rel="stylesheet" href="/static/atlas/chat-ui.css">'
    b'<script src="/static/atlas/chat-ui.js"></script>'
)


def inject_atlas_ui(
    html: bytes, *, stable: bool = False, code_mode: bool = False
) -> bytes:
    """Add the owned assets once, leaving the Open WebUI shell otherwise byte-identical."""
    if b"/static/atlas/chat-ui.js" in html:
        return html
    tags = ATLAS_UI_TAGS
    if code_mode and not stable:
        tags = b'<meta name="atlas-code-mode" content="1">' + tags
    if stable:
        tags = b'<script src="/static/atlas/stable-auth.js"></script>' + tags
    return html.replace(b"</head>", tags + b"</head>", 1)
