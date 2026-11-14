"""Privacy level of each chat conversation, chosen in the conversation and stored by Atlas.

The chat interface shows the level next to the composer and lets the user choose it
(``/api/atlas/privacy``). Atlas stores it against the user and the interface's own
conversation id, and the chat adapter applies it to every request of that conversation
(``bind``), together with the organisation minimum (packages.privacy).

Rules:

- A conversation's level is never lowered once it has messages: they were written at that
  level, and a lower level would send them on to a shared provider on the next turn. The
  user starts a new conversation instead.
- A new conversation has no id yet when the user picks its level: the choice is kept as
  the user's pending level and bound to the first conversation id that reaches Atlas.
- Levels below the organisation minimum, Enhanced where the operator has not enabled it,
  and levels with no server to honour them here (Strict without an owned server, Enhanced
  without the pseudonymiser or an owned server) cannot be chosen; the interface shows
  them disabled with the reason.

Only the level is stored (user id, conversation id, level, time); never any content.
"""

from contextlib import contextmanager
from collections.abc import Iterator
import os
from pathlib import Path
import re
import sqlite3
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages import privacy
from services.orchestrator.project_ui import public_error
from services.orchestrator.user_memory_ui import browser_user
from services.retrieval.project_identity import ProjectError

router = APIRouter()
CHAT_ID = re.compile(r"[A-Za-z0-9:_-]{1,120}")
PENDING = ""  # the conversation id of the user's next new conversation


def store_path() -> Path:
    configured = os.environ.get("ATLAS_CONVERSATION_PRIVACY_DB")
    if configured:
        return Path(configured)
    return (
        Path(os.environ.get("ATLAS_DATA_DIR", "BRAIN")) / "conversation-privacy.sqlite"
    )


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    path = store_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5, isolation_level="IMMEDIATE")
    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS conversation_privacy ("
            "user TEXT NOT NULL, chat TEXT NOT NULL, "
            "level TEXT NOT NULL CHECK(level IN ('standard','enhanced','strict')), "
            "used INTEGER NOT NULL DEFAULT 0 CHECK(used IN (0,1)), "
            "updated INTEGER NOT NULL, PRIMARY KEY(user, chat))"
        )
        with db:
            yield db
    finally:
        db.close()


def valid_user(user: str) -> bool:
    return isinstance(user, str) and 0 < len(user) <= 200 and user.strip() == user


def valid_chat(chat: str) -> bool:
    return isinstance(chat, str) and (chat == PENDING or bool(CHAT_ID.fullmatch(chat)))


def _row(db: sqlite3.Connection, user: str, chat: str) -> tuple[str, int] | None:
    row = db.execute(
        "SELECT level, used FROM conversation_privacy WHERE user=? AND chat=?",
        (user, chat),
    ).fetchone()
    return (str(row[0]), int(row[1])) if row else None


def conversation_level(user: str, chat: str) -> privacy.Level | None:
    """The stored level of a conversation (or of the pending new one), if any."""
    if not valid_user(user) or not valid_chat(chat):
        return None
    with connection() as db:
        row = _row(db, user, chat)
    return privacy.highest(row[0]) if row else None


def bind(user: str, chat: str) -> privacy.Level:
    """The level of a conversation request; marks the conversation as used.

    A conversation seen for the first time takes the user's pending level (then cleared).
    Without a conversation id, the pending level applies without being consumed.
    """
    if not valid_user(user):
        return privacy.STANDARD
    with connection() as db:
        if not chat or not valid_chat(chat):
            pending = _row(db, user, PENDING)
            return privacy.highest(pending[0]) if pending else privacy.STANDARD
        row = _row(db, user, chat)
        if row is None:
            pending = _row(db, user, PENDING)
            level = privacy.highest(pending[0]) if pending else privacy.STANDARD
            db.execute(
                "INSERT INTO conversation_privacy VALUES (?,?,?,1,?)",
                (user, chat, level, int(time.time())),
            )
            if pending:
                db.execute(
                    "DELETE FROM conversation_privacy WHERE user=? AND chat=?",
                    (user, PENDING),
                )
            return level
        if not row[1]:
            db.execute(
                "UPDATE conversation_privacy SET used=1 WHERE user=? AND chat=?",
                (user, chat),
            )
        return privacy.highest(row[0])


def availability(level: str) -> str | None:
    """Why ``level`` cannot be chosen on this server, or None."""
    if privacy.RANK[level] < privacy.RANK[privacy.minimum()]:
        return "organisation_minimum"
    if level == privacy.STRICT and not privacy.owned_available():
        return "no_owned_server"
    if level == privacy.ENHANCED and not (
        privacy.pseudonymiser_available() or privacy.owned_available()
    ):
        return "no_pseudonymiser"
    return None


def state(user: str, chat: str) -> dict[str, object]:
    with connection() as db:
        row = _row(db, user, chat)
        if row is None and chat != PENDING:
            # A conversation Atlas has not seen yet takes the pending choice on its
            # first message (bind): show that level.
            row = _row(db, user, PENDING)
            row = (row[0], 0) if row else None
    stored = privacy.highest(row[0]) if row else privacy.STANDARD
    locked = bool(row and row[1])
    level = privacy.highest(stored, privacy.minimum())
    levels = []
    for name in privacy.LEVELS:
        if not privacy.offered(name):
            continue
        reason = availability(name)
        if reason is None and locked and privacy.RANK[name] < privacy.RANK[stored]:
            reason = "conversation_started"
        levels.append({"id": name, "available": reason is None, "reason": reason})
    return {
        "chat": chat,
        "level": level,
        "minimum": privacy.minimum(),
        "locked": locked,
        "levels": levels,
    }


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    chat: str = Field(default=PENDING, max_length=120)
    level: str = Field(max_length=16)


def choose(user: str, choice: Choice) -> dict[str, object]:
    level = privacy.parse(choice.level)
    if level is None:
        raise privacy.InvalidPrivacyLevel()
    if not privacy.offered(level):
        raise ProjectError(
            403,
            privacy.ENHANCED_CODE,
            "Enhanced privacy is not offered on this server.",
        )
    reason = availability(level)
    with connection() as db:
        row = _row(db, user, choice.chat)
        if row and row[1] and privacy.RANK[level] < privacy.RANK[row[0]]:
            raise ProjectError(
                409,
                "privacy_level_locked",
                "This conversation already has messages written at a higher privacy "
                "level; start a new conversation to use a lower one.",
            )
        if reason is not None and not (row and row[0] == level):
            raise ProjectError(
                403,
                "privacy_level_unavailable",
                f"This privacy level cannot be chosen here ({reason}).",
            )
        db.execute(
            "INSERT INTO conversation_privacy VALUES (?,?,?,0,?) "
            "ON CONFLICT(user, chat) DO UPDATE SET level=excluded.level, "
            "updated=excluded.updated",
            (user, choice.chat, level, int(time.time())),
        )
    return state(user, choice.chat)


@router.get("/api/atlas/privacy")
async def read(request: Request, chat: str = PENDING) -> Response:
    try:
        user = await browser_user(request)
        if not valid_chat(chat):
            raise ProjectError(422, "invalid_chat")
        return JSONResponse(
            state(user, chat), headers={"Cache-Control": "private, no-store"}
        )
    except ProjectError as exc:
        return public_error(exc)


@router.put("/api/atlas/privacy")
async def write(request: Request) -> Response:
    try:
        user = await browser_user(request)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024:
                raise ProjectError(413, "request_size_exceeded")
        choice = Choice.model_validate_json(bytes(body))
        if not valid_chat(choice.chat):
            raise ProjectError(422, "invalid_chat")
        return JSONResponse(
            choose(user, choice), headers={"Cache-Control": "private, no-store"}
        )
    except privacy.InvalidPrivacyLevel as exc:
        return public_error(ProjectError(400, exc.code, exc.message))
    except ValidationError:
        return public_error(ProjectError(422, "invalid_request"))
    except ProjectError as exc:
        return public_error(exc)
