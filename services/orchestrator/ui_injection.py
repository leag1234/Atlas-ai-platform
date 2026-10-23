"""Pure HTML-shell injection contract for the owned Open WebUI overlay."""

import html as html_module
import os

ATLAS_UI_TAGS = (
    b'<link rel="stylesheet" href="/static/atlas/chat-ui.css">'
    b'<script src="/static/atlas/chat-ui.js"></script>'
)


def instance_notice() -> str:
    """Return the bounded operator notice, or no notice when it is invalid."""
    notice = os.environ.get("ATLAS_INSTANCE_NOTICE", "")
    return notice if len(notice) <= 200 else ""


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
        notice = instance_notice()
        notice_attr = (
            f' data-instance-notice="{html_module.escape(notice, quote=True)}"'.encode()
            if notice
            else b""
        )
        tags = (
            b'<script src="/static/atlas/stable-auth.js"'
            + notice_attr
            + b"></script>"
            + tags
        )
    return html.replace(b"</head>", tags + b"</head>", 1)
