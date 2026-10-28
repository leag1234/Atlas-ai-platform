"""Pure HTML-shell injection contract for the owned Open WebUI overlay."""

import html as html_module
import json
import os

ATLAS_UI_TAGS = (
    b'<link rel="stylesheet" href="/api/atlas/keys/style.css">'
    b'<script defer src="/api/atlas/keys/client.js"></script>'
    b'<link rel="stylesheet" href="/static/atlas/chat-ui.css">'
    b'<script src="/static/atlas/brand-ui.js"></script>'
    b'<script src="/static/atlas/chat-ui.js"></script>'
    b'<script src="/static/atlas/guest-chat.js"></script>'
    b'<script type="module" src="/static/atlas/settings-ui.js"></script>'
    b'<link rel="stylesheet" href="/project-mode/style.css">'
    b'<script defer src="/api/atlas/user-memory/client.js"></script>'
    b'<script defer src="/project-mode/locale.js"></script>'
    b'<script defer src="/project-mode/client.js"></script>'
)


def instance_notice() -> str:
    """Return the bounded operator notice, or no notice when it is invalid.

    The value is either one plain text or a JSON object mapping language codes
    to texts (for example {"en": "...", "fr": "..."}); the browser picks the
    user's language and falls back to English.
    """
    notice = os.environ.get("ATLAS_INSTANCE_NOTICE", "")
    if not notice.lstrip().startswith("{"):
        return notice if len(notice) <= 200 else ""
    try:
        texts = json.loads(notice)
    except ValueError:
        return ""
    if (
        not isinstance(texts, dict)
        or not texts
        or len(texts) > 8
        or not all(
            isinstance(key, str)
            and 2 <= len(key) <= 5
            and isinstance(value, str)
            and 0 < len(value) <= 200
            for key, value in texts.items()
        )
    ):
        return ""
    return json.dumps(texts, ensure_ascii=False, separators=(",", ":"))


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
