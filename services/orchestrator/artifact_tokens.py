"""Signed preview capability tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time


TOKEN_PATTERN = re.compile(
    r"^[0-9a-f]{32}\.[1-9][0-9]{0,3}\.[0-9]{10}\.[A-Za-z0-9_-]{43}$"
)
TOKEN_MAX_LENGTH = 128
DEFAULT_TOKEN_SECONDS = 900
SIGNING_DOMAIN = "atlas-preview-v1"


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _signature(
    key: str,
    artifact_id: str,
    version: str,
    expiry: str,
    audience: str,
) -> str:
    message = f"{SIGNING_DOMAIN}|{artifact_id}|{version}|{expiry}|{audience}"
    return _base64url(hmac.new(key.encode(), message.encode(), hashlib.sha256).digest())


def _audience() -> str:
    return os.environ.get("ATLAS_PREVIEW_ORIGIN", "").strip() or "same-origin"


def _token_seconds() -> int:
    try:
        seconds = int(os.environ.get("ATLAS_PREVIEW_TOKEN_SECONDS", ""))
        if seconds < 1:
            raise ValueError
    except ValueError:
        seconds = DEFAULT_TOKEN_SECONDS
    return seconds


def issue_token(
    artifact_id: str,
    version: int,
    *,
    now: int | None = None,
    audience: str | None = None,
) -> str:
    """Issue a token bound to one artifact version."""

    if not re.fullmatch(r"[0-9a-f]{32}", artifact_id):
        raise ValueError("invalid artifact id")
    if not 1 <= version <= 1000:
        raise ValueError("invalid artifact version")
    current_time = time.time() if now is None else now
    expiry = int(current_time) + _token_seconds()
    token_audience = _audience() if audience is None else audience
    signature = _signature(
        os.environ["ATLAS_PREVIEW_SIGNING_KEY"],
        artifact_id,
        str(version),
        str(expiry),
        token_audience,
    )
    return f"{artifact_id}.{version}.{expiry}.{signature}"


def verify_token(
    token: str,
    artifact_id: str,
    version: int,
    *,
    now: int | None = None,
    audience: str | None = None,
) -> bool:
    """Verify a token without revealing the failure reason."""

    if not re.fullmatch(r"[0-9a-f]{32}", artifact_id):
        return False
    if not 1 <= version <= 1000:
        return False
    current_time = int(time.time() if now is None else now)
    token_audience = _audience() if audience is None else audience
    if not isinstance(token, str) or len(token) > TOKEN_MAX_LENGTH:
        return False
    if not TOKEN_PATTERN.fullmatch(token):
        return False
    token_id, token_version, expiry_text, signature = token.split(".")
    if token_id != artifact_id or token_version != str(version):
        return False
    try:
        expiry = int(expiry_text)
    except ValueError:
        return False
    if expiry <= current_time or expiry > current_time + _token_seconds():
        return False
    if not os.environ.get("ATLAS_PREVIEW_SIGNING_KEY", ""):
        return False
    expected = _signature(
        os.environ.get("ATLAS_PREVIEW_SIGNING_KEY", ""),
        token_id,
        token_version,
        expiry_text,
        token_audience,
    )
    previous_key = os.environ.get("ATLAS_PREVIEW_SIGNING_KEY_PREVIOUS", "")
    previous = (
        _signature(
            previous_key,
            token_id,
            token_version,
            expiry_text,
            token_audience,
        )
        if previous_key
        else ""
    )
    return hmac.compare_digest(signature, expected) or (
        bool(previous) and hmac.compare_digest(signature, previous)
    )
