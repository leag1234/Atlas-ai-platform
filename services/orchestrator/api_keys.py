"""REQ-KEY-01..06: private credentials and atomic, fail-closed daily reservations."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any

from services.orchestrator.dev_auth import Store as DeveloperStore, QuotaError


@dataclass(frozen=True)
class Principal:
    id: str
    org_id: str
    member_id: str
    subject: str
    budget_user: str
    scopes: tuple[str, ...]


class KeyQuotaError(QuotaError):
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values
        super().__init__(json.dumps(values, sort_keys=True))


class Store(DeveloperStore):
    """Small store interface; callers never query tables or handle credential hashes."""

    def __init__(
        self, path: Path, pepper: str, now: Callable[[], float] = time.time
    ) -> None:
        if len(pepper.encode()) < 32:
            raise ValueError("ATLAS_API_KEY_PEPPER must contain at least 32 bytes")
        self.path, self.pepper, self.now = path, pepper.encode(), now
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise PermissionError("private_database_required")
        finally:
            os.close(fd)
        with self.connection() as db:
            db.executescript(
                (
                    Path(__file__).resolve().parents[2] / "contracts/api-keys.sql"
                ).read_text()
            )

    def digest(self, key: str) -> str:
        return hmac.new(self.pepper, key.encode(), hashlib.sha256).hexdigest()

    def day(self) -> str:
        return datetime.fromtimestamp(self.now(), timezone.utc).date().isoformat()

    def audit(self, event: str, key_id: str | None, **detail: object) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO api_key_audit(event,key_id,timestamp,detail) VALUES (?,?,?,?)",
                (event, key_id, int(self.now()), json.dumps(detail)),
            )

    def create(
        self,
        *,
        org_id: str,
        member_id: str,
        subject: str,
        budget_user: str,
        name: str,
        scopes: list[str],
        created_by: str,
        daily_requests: int = 20,
        daily_micro_eur: int = 1000000,
        lifetime_days: int = 90,
    ) -> tuple[dict[str, Any], str]:
        if (
            any(
                not v.strip() or len(v) > 320 or any(c in v for c in "\r\n")
                for v in (org_id, member_id, subject, budget_user, name, created_by)
            )
            or not scopes
            or set(scopes) - {"assistant", "model"}
            or type(lifetime_days) is not int
            or not 1 <= lifetime_days <= 365
            or any(
                type(v) is not int or v <= 0 for v in (daily_requests, daily_micro_eur)
            )
        ):
            raise ValueError("invalid_key_policy")
        prefix = "atl_" + secrets.token_hex(4)
        key = prefix + "_" + secrets.token_urlsafe(32)
        identifier, now = secrets.token_hex(16), int(self.now())
        with self.connection() as db:
            db.execute(
                "INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
                (
                    identifier,
                    org_id,
                    member_id,
                    subject,
                    budget_user,
                    name,
                    prefix,
                    self.digest(key),
                    "hmac-sha256",
                    json.dumps(sorted(set(scopes))),
                    now + lifetime_days * 86400,
                    daily_requests,
                    daily_micro_eur,
                    created_by,
                    now,
                ),
            )
        self.audit("create", identifier, created_by=created_by)
        return self.get(identifier), key

    def get(self, key_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
        if row is None:
            raise KeyError("key_not_found")
        result = dict(row)
        result.pop("hash")
        result["scopes"] = json.loads(result["scopes"])
        result["rotation_required"] = result.pop("hash_kind") == "sha256-legacy"
        return result

    def list_keys(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            ids = [
                str(r[0])
                for r in db.execute("SELECT id FROM api_keys ORDER BY created_at,id")
            ]
        return [self.get(identifier) for identifier in ids]

    def resolve(self, key: str, ip: str = "") -> Principal:
        if not 43 <= len(key) <= 100:
            self.audit("failure", None, reason="invalid")
            raise PermissionError("invalid")
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM api_keys WHERE (hash=? AND hash_kind='hmac-sha256') "
                "OR (hash=? AND hash_kind='sha256-legacy')",
                (self.digest(key), hashlib.sha256(key.encode()).hexdigest()),
            ).fetchone()
        reason = (
            "invalid"
            if row is None
            else "revoked"
            if row["revoked_at"] is not None
            else "expired"
            if row["expires_at"] <= self.now()
            else ""
        )
        if reason:
            self.audit("failure", str(row["id"]) if row else None, reason=reason)
            raise PermissionError(reason)
        assert row is not None
        try:
            address = ipaddress.ip_address(ip)
            ip_prefix = str(
                ipaddress.ip_network(
                    f"{address}/{24 if address.version == 4 else 48}", strict=False
                )
            )
        except ValueError:
            ip_prefix = None
        with self.connection() as db:
            db.execute(
                "UPDATE api_keys SET last_used_at=?,last_used_ip_prefix=? WHERE id=?",
                (int(self.now()), ip_prefix, row["id"]),
            )
        return Principal(
            str(row["id"]),
            str(row["org_id"]),
            str(row["member_id"]),
            str(row["subject"]),
            str(row["budget_user"]),
            tuple(json.loads(row["scopes"])),
        )

    def has_legacy(self, key: str) -> bool:
        with self.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM api_keys WHERE hash=? AND hash_kind='sha256-legacy'",
                    (hashlib.sha256(key.encode()).hexdigest(),),
                ).fetchone()
                is not None
            )

    def authenticate(self, key: str) -> str:
        return self.resolve(key).id

    def revoke(self, developer: str) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE api_keys SET revoked_at=COALESCE(revoked_at,?) WHERE id=?",
                (int(self.now()), developer),
            )
        self.audit("revoke", developer)

    def usage(self, developer: str) -> dict[str, object]:
        owner = self.get(developer)
        with self.connection() as db:
            row = db.execute(
                "SELECT COUNT(*) requests, COALESCE(SUM(charged),0) charged_micro_eur, "
                "COALESCE(SUM(CASE WHEN state!='complete' THEN charged ELSE 0 END),0) unknown_micro_eur "
                "FROM api_key_requests WHERE key_id=? AND day=?",
                (developer, self.day()),
            ).fetchone()
        return dict(
            key_id=developer,
            day=self.day(),
            **dict(row),
            daily_requests=owner["daily_requests"],
            daily_micro_eur=owner["daily_micro_eur"],
        )

    def reserve(self, developer: str, amount: int, *, monetary: bool = True) -> str:
        # Same atomic reserve/settle algorithm as dev_auth, with the assistant ceiling.
        if type(amount) is not int or not 0 <= amount <= 300000:
            raise ValueError("invalid_reservation")
        identifier, day = secrets.token_hex(16), self.day()
        failure: dict[str, object] | None = None
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute(
                "SELECT * FROM api_keys WHERE id=?", (developer,)
            ).fetchone()
            if (
                owner is None
                or owner["revoked_at"] is not None
                or owner["expires_at"] <= self.now()
            ):
                raise PermissionError("unauthorized")
            count, charged = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(charged),0) FROM api_key_requests WHERE key_id=? AND day=?",
                (developer, day),
            ).fetchone()
            if (
                count >= owner["daily_requests"]
                or charged + amount > owner["daily_micro_eur"]
            ):
                failure = dict(
                    requests=count,
                    daily_requests=owner["daily_requests"],
                    charged_micro_eur=charged,
                    reserved_micro_eur=amount,
                    daily_micro_eur=owner["daily_micro_eur"],
                )
            else:
                db.execute(
                    "INSERT INTO api_key_requests VALUES (?,?,?,?,?,NULL,NULL,'reserved')",
                    (identifier, developer, day, amount, amount),
                )
        if failure is not None:
            self.audit("failure", developer, reason="quota", **failure)
            raise KeyQuotaError(failure)
        self.summary(developer, day)
        return identifier

    def summary(self, key_id: str, day: str) -> None:
        with self.connection() as db:
            count, charged = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(charged),0) FROM api_key_requests WHERE key_id=? AND day=?",
                (key_id, day),
            ).fetchone()
            db.execute(
                "INSERT INTO api_key_daily_summary VALUES (?,?,?,?) ON CONFLICT(key_id,day) DO UPDATE SET requests=excluded.requests, charged_micro_eur=excluded.charged_micro_eur",
                (key_id, day, count, charged),
            )

    def finish(
        self, request_id: str, charged: int | None, incoming: int = 0, outgoing: int = 0
    ) -> None:
        if any(type(v) is not int or v < 0 for v in (incoming, outgoing)) or (
            charged is not None and (type(charged) is not int or charged < 0)
        ):
            raise ValueError("invalid_usage")
        with self.connection() as db:
            row = db.execute(
                "SELECT key_id,day FROM api_key_requests WHERE id=?", (request_id,)
            ).fetchone()
            cursor = db.execute(
                "UPDATE api_key_requests SET charged=COALESCE(?,charged),input_tokens=?,output_tokens=?,state=? WHERE id=? AND state='reserved'",
                (
                    charged,
                    incoming if charged is not None else None,
                    outgoing if charged is not None else None,
                    "unknown" if charged is None else "complete",
                    request_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("already_settled_or_unknown")
        self.summary(str(row[0]), str(row[1]))

    def import_developer(
        self,
        source: DeveloperStore,
        developer: str,
        *,
        org_id: str,
        member_id: str,
        subject: str,
        budget_user: str,
        created_by: str,
    ) -> dict[str, Any]:
        # Retain the SHA-256 credential and existing reservations; no cleartext needed.
        with source.connection() as db:
            owner = db.execute(
                "SELECT * FROM developers WHERE id=?", (developer,)
            ).fetchone()
            requests = db.execute(
                "SELECT * FROM requests WHERE developer=?", (developer,)
            ).fetchall()
        if owner is None:
            raise ValueError("developer_not_found")
        identifier = (
            "legacy-"
            + hashlib.sha256(
                (str(source.path.resolve()) + ":" + developer).encode()
            ).hexdigest()[:24]
        )
        now = int(self.now())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM api_keys WHERE id=?", (identifier,)
            ).fetchone():
                return self.get(identifier)
            db.execute(
                "INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    org_id,
                    member_id,
                    subject,
                    budget_user,
                    developer,
                    "legacy-" + owner["key_hash"][:8],
                    owner["key_hash"],
                    "sha256-legacy",
                    '["model"]',
                    now + 90 * 86400,
                    owner["daily_requests"],
                    owner["daily_micro_eur"],
                    created_by,
                    now,
                    None,
                    None,
                    None if owner["enabled"] else now,
                ),
            )
            for row in requests:
                db.execute(
                    "INSERT INTO api_key_requests VALUES (?,?,?,?,?,?,?,?)",
                    (
                        row["id"],
                        identifier,
                        row["day"],
                        row["reserved"],
                        row["charged"],
                        row["input_tokens"],
                        row["output_tokens"],
                        row["state"],
                    ),
                )
        self.audit("import", identifier, created_by=created_by, rotation_required=True)
        return self.get(identifier)


def configured_store() -> Store:
    return Store(
        Path(os.environ.get("ATLAS_API_KEYS_DB", "BRAIN/api-keys.sqlite")),
        os.environ.get("ATLAS_API_KEY_PEPPER", ""),
    )
