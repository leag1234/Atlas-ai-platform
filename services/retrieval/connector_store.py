"""REQ-SHARE-07/08: separate connector broker state, encrypted with server AEAD key."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from collections.abc import Iterator
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.orchestrator.mcp_http import endpoint


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Connector(Closed):
    id: str
    name: str = Field(min_length=1, max_length=80, pattern=r"^[\w -]+$")
    kind: Literal["shared", "personal"]
    auth: Literal["none", "token", "oauth"] = "none"
    url: str = ""
    command_id: str = ""
    client_id: str = Field(default="", max_length=512)
    redirect_uri: str = ""

    @model_validator(mode="after")
    def valid(self) -> Connector:
        UUID(self.id)
        local = os.environ.get("ATLAS_MCP_ALLOW_LOCAL") == "1"
        if bool(self.url) == bool(self.command_id):
            raise ValueError("one_transport_required")
        if self.url:
            endpoint(self.url, local)
        if self.command_id:
            commands = json.loads(os.environ.get("ATLAS_MCP_COMMANDS", "{}"))
            if self.command_id not in commands or self.auth == "oauth":
                raise ValueError("mcp_command_denied")
        if self.auth == "oauth":
            endpoint(self.redirect_uri, local)
            if not self.client_id:
                raise ValueError("oauth_client_required")
        return self


class BrokerStore:
    def __init__(self, path: Path, key: bytes) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if len(key) != 32:
            raise ValueError("mcp_server_key_required")
        self.cipher = AESGCM(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(path, 0o600)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS entries (org TEXT, category TEXT, owner TEXT, id TEXT, value BLOB NOT NULL, PRIMARY KEY(org,category,owner,id))"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA secure_delete=ON")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def aad(org: str, category: str, owner: str, identifier: str) -> bytes:
        return json.dumps([org, category, owner, identifier]).encode()

    def put(
        self,
        org: str,
        category: str,
        owner: str,
        identifier: str,
        value: dict[str, Any],
    ) -> None:
        nonce = os.urandom(12)
        encrypted = nonce + self.cipher.encrypt(
            nonce,
            json.dumps(value).encode(),
            self.aad(org, category, owner, identifier),
        )
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?)",
                (org, category, owner, identifier, encrypted),
            )

    def get(
        self,
        org: str,
        category: str,
        owner: str,
        identifier: str,
        *,
        take: bool = False,
    ) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM entries WHERE org=? AND category=? AND owner=? AND id=?",
                (org, category, owner, identifier),
            ).fetchone()
            if row is None:
                return None
            if take:
                db.execute(
                    "DELETE FROM entries WHERE org=? AND category=? AND owner=? AND id=?",
                    (org, category, owner, identifier),
                )
            raw = bytes(row[0])
            value: dict[str, Any] = json.loads(
                self.cipher.decrypt(
                    raw[:12], raw[12:], self.aad(org, category, owner, identifier)
                )
            )
            return value

    def list(self, org: str, category: str, owner: str = "") -> list[dict[str, Any]]:
        with self.connect() as db:
            identifiers = db.execute(
                "SELECT id FROM entries WHERE org=? AND category=? AND owner=? ORDER BY id",
                (org, category, owner),
            ).fetchall()
        return [
            value
            for row in identifiers
            if (value := self.get(org, category, owner, row[0])) is not None
        ]

    def disconnect(self, org: str, member: str, connector: str) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM entries WHERE org=? AND category='credential' AND owner=? AND id=?",
                (org, member, connector),
            )
            # Revoke all outstanding authorizations for this member. They are short-lived.
            db.execute(
                "DELETE FROM entries WHERE org=? AND owner=? AND category IN ('oauth','approval')",
                (org, member),
            )


def configured_store() -> BrokerStore:
    path = os.environ.get("ATLAS_MCP_BROKER_DB", "")
    if not path:
        raise ValueError("mcp_broker_unconfigured")
    key = base64.b64decode(os.environ.get("ATLAS_MCP_BROKER_KEY", ""), validate=True)
    return BrokerStore(Path(path), key)


def allowed(
    org: list[str],
    connector: str,
    member_rule: str | None,
    group_rules: tuple[str, ...] = (),
    project: list[str] | None = None,
) -> bool:
    """Deny wins; absent member/group grant denies. Groups can be added unchanged."""
    rules = (member_rule, *group_rules)
    return (
        connector in org
        and "deny" not in rules
        and "allow" in rules
        and (project is None or connector in project)
    )
