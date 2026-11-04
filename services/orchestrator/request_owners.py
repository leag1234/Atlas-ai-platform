"""Owner of each request directory in the shared user-file terminal.

Every request writes its files under /home/user/requests/<request id>.  The files
terminal mounts all of them, so a download link alone must not be enough to read a
file: the front-door identity of the caller must be the identity that made the
request.  Identities are stored as SHA-256 digests of the normalized e-mail (the
front door asserts the same e-mail that Open WebUI forwards), never in clear.
"""

import hashlib
import hmac
import logging
import os
import re
import sqlite3
from pathlib import Path

REQUEST_ID = re.compile(r"chatcmpl-[A-Za-z0-9-]{1,80}")


def store_path() -> Path:
    return Path(os.environ.get("ATLAS_DATA_DIR", "BRAIN")) / "request-owners.sqlite"


def digest(identity: str) -> str:
    return hashlib.sha256(identity.strip().lower().encode()).hexdigest()


def usable(identity: str | None) -> bool:
    """An empty identity, or the empty front-door user "@host", names nobody."""
    if not isinstance(identity, str):
        return False
    value = identity.strip()
    return bool(value) and not value.startswith("@") and len(value) <= 320


def connect() -> sqlite3.Connection:
    path = store_path()
    if path.parent.is_symlink() or path.is_symlink():
        raise OSError("unsafe_request_owner_store")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path, timeout=5)
    db.execute(
        "CREATE TABLE IF NOT EXISTS request_owners "
        "(request_id TEXT PRIMARY KEY, owner TEXT NOT NULL)"
    )
    path.chmod(0o600)
    return db


def record(request_id: str, identity: str | None) -> bool:
    """Bind a request id to its first owner; later calls never change it.

    A store failure does not stop the answer: its downloads are then refused.
    """
    if not REQUEST_ID.fullmatch(request_id) or not usable(identity):
        return False
    assert identity is not None
    try:
        db = connect()
        try:
            with db:
                db.execute(
                    "INSERT OR IGNORE INTO request_owners (request_id, owner) "
                    "VALUES (?, ?)",
                    (request_id, digest(identity)),
                )
        finally:
            db.close()
    except (OSError, sqlite3.Error) as exc:
        logging.getLogger("atlas.request_owners").warning(
            "request_owner_unrecorded: %s", type(exc).__name__
        )
        return False
    return True


def owns(request_id: str, identity: str | None) -> bool:
    """Fail closed: an unknown request, an unusable identity or a store error is no."""
    if not REQUEST_ID.fullmatch(request_id) or not usable(identity):
        return False
    assert identity is not None
    try:
        db = connect()
        try:
            row = db.execute(
                "SELECT owner FROM request_owners WHERE request_id = ?", (request_id,)
            ).fetchone()
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        return False
    return row is not None and hmac.compare_digest(str(row[0]), digest(identity))
