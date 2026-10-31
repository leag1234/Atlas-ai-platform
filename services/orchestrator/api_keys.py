"""REQ-KEY-01..06: private credentials and atomic, fail-closed daily reservations."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
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

MAX_CALL_RESERVATION = 5000000


@dataclass(frozen=True)
class Principal:
    id: str
    org_id: str
    member_id: str
    subject: str
    budget_user: str
    scopes: tuple[str, ...]
    privacy: str = "standard"


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
                (Path(__file__).resolve().parents[2] / "contracts/api-keys.sql")
                .read_text()
                .replace(
                    "CREATE INDEX IF NOT EXISTS api_key_weekly ON api_key_requests(key_id, week);",
                    "",
                )
            )
            self._upgrade_schema(db)

    @staticmethod
    def _columns(db: Any, table: str) -> dict[str, Any]:
        return {
            str(row["name"]): row for row in db.execute(f"PRAGMA table_info({table})")
        }

    def _upgrade_schema(self, db: Any) -> None:
        """Upgrade existing private key stores without changing their credentials."""
        keys = self._columns(db, "api_keys")
        if keys["daily_requests"]["notnull"] or keys["daily_micro_eur"]["notnull"]:
            # SQLite rewrites child foreign keys when a parent table is renamed, so
            # build the new table first and rename it onto the original name.
            db.commit()
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute(
                "CREATE TABLE api_keys_optional_caps ("
                "id TEXT PRIMARY KEY, org_id TEXT NOT NULL, member_id TEXT NOT NULL, "
                "subject TEXT NOT NULL, budget_user TEXT NOT NULL, name TEXT NOT NULL, "
                "prefix TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, "
                "hash_kind TEXT NOT NULL CHECK(hash_kind IN ('hmac-sha256','sha256-legacy')), "
                "scopes TEXT NOT NULL, expires_at INTEGER NOT NULL, "
                "daily_requests INTEGER CHECK(daily_requests > 0), "
                "daily_micro_eur INTEGER CHECK(daily_micro_eur > 0), "
                "weekly_micro_eur INTEGER CHECK(weekly_micro_eur > 0), "
                "own_budget INTEGER NOT NULL DEFAULT 0 CHECK(own_budget IN (0,1)), "
                "created_by TEXT NOT NULL, created_at INTEGER NOT NULL, "
                "last_used_at INTEGER, last_used_ip_prefix TEXT, revoked_at INTEGER)"
            )
            db.execute(
                "INSERT INTO api_keys_optional_caps (id,org_id,member_id,subject,budget_user,name,prefix,hash,hash_kind,scopes,expires_at,daily_requests,daily_micro_eur,created_by,created_at,last_used_at,last_used_ip_prefix,revoked_at) "
                "SELECT id,org_id,member_id,subject,budget_user,name,prefix,hash,hash_kind,scopes,expires_at,daily_requests,daily_micro_eur,created_by,created_at,last_used_at,last_used_ip_prefix,revoked_at FROM api_keys"
            )
            db.execute("DROP TABLE api_keys")
            db.execute("ALTER TABLE api_keys_optional_caps RENAME TO api_keys")
            db.commit()
            db.execute("PRAGMA foreign_keys=ON")
            keys = self._columns(db, "api_keys")
        self._repair_key_references(db)
        if "weekly_micro_eur" not in keys:
            db.execute("ALTER TABLE api_keys ADD COLUMN weekly_micro_eur INTEGER")
        if "own_budget" not in keys:
            db.execute(
                "ALTER TABLE api_keys ADD COLUMN own_budget INTEGER NOT NULL DEFAULT 0 CHECK(own_budget IN (0,1))"
            )
        if "privacy" not in keys:
            db.execute(
                "ALTER TABLE api_keys ADD COLUMN privacy TEXT NOT NULL DEFAULT 'standard' CHECK(privacy IN ('standard','strict'))"
            )
        requests = self._columns(db, "api_key_requests")
        if "week" not in requests:
            db.execute("ALTER TABLE api_key_requests ADD COLUMN week TEXT")
            for request_id, day in db.execute("SELECT id,day FROM api_key_requests"):
                db.execute(
                    "UPDATE api_key_requests SET week=? WHERE id=?",
                    (self.week_for_day(str(day)), str(request_id)),
                )
        db.execute(
            "CREATE INDEX IF NOT EXISTS api_key_daily ON api_key_requests(key_id, day)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS api_key_weekly ON api_key_requests(key_id, week)"
        )
        policy = self._columns(db, "api_key_policy")
        if (
            policy["daily_requests"]["notnull"]
            or policy["daily_micro_eur"]["notnull"]
            or "weekly_micro_eur" not in policy
        ):
            db.commit()
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute(
                "CREATE TABLE api_key_policy_optional_caps ("
                "org_id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('off','admin_only','self_service')), "
                "daily_requests INTEGER CHECK(daily_requests > 0), "
                "daily_micro_eur INTEGER CHECK(daily_micro_eur > 0), "
                "weekly_micro_eur INTEGER CHECK(weekly_micro_eur > 0), "
                "max_lifetime_days INTEGER NOT NULL CHECK(max_lifetime_days BETWEEN 7 AND 365), "
                "active_key_limit INTEGER NOT NULL CHECK(active_key_limit > 0))"
            )
            db.execute(
                "INSERT INTO api_key_policy_optional_caps "
                "(org_id,mode,daily_requests,daily_micro_eur,weekly_micro_eur,max_lifetime_days,active_key_limit) "
                "SELECT org_id,mode,daily_requests,daily_micro_eur,NULL,max_lifetime_days,active_key_limit "
                "FROM api_key_policy"
            )
            db.execute("DROP TABLE api_key_policy")
            db.execute(
                "ALTER TABLE api_key_policy_optional_caps RENAME TO api_key_policy"
            )
            db.commit()
            db.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _repair_key_references(db: Any) -> None:
        """Rebuild tables whose foreign keys point at the dropped pre-upgrade table."""
        stale = "api_keys_before_optional_caps"
        rows = db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql LIKE ?",
            (f"%{stale}%",),
        ).fetchall()
        for name, sql in rows:
            name = str(name)
            fixed = (
                str(sql).replace(f'"{stale}"', "api_keys").replace(stale, "api_keys")
            )
            fixed = fixed.replace(
                f"CREATE TABLE {name}", f"CREATE TABLE {name}_repair", 1
            )
            db.execute(fixed)
            db.execute(f"INSERT INTO {name}_repair SELECT * FROM {name}")
            db.execute(f"DROP TABLE {name}")
            db.execute(f"ALTER TABLE {name}_repair RENAME TO {name}")

    def policy(self, org_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM api_key_policy WHERE org_id=?", (org_id,)
            ).fetchone()
        if row is not None:
            return {k: v for k, v in dict(row).items() if k != "org_id"}
        stable = os.environ.get("ATLAS_STABLE") == "1"
        return dict(
            mode=os.environ.get(
                "ATLAS_API_KEY_POLICY",
                "self_service" if stable else "admin_only",
            ),
            daily_requests=None if stable else 200,
            daily_micro_eur=None if stable else 2000000,
            weekly_micro_eur=100000000 if stable else None,
            max_lifetime_days=90 if stable else 365,
            active_key_limit=5,
        )

    def set_policy(self, org_id: str, values: dict[str, Any], actor: str) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO api_key_policy VALUES (?,?,?,?,?,?,?) ON CONFLICT(org_id) DO UPDATE SET "
                "mode=excluded.mode,daily_requests=excluded.daily_requests,daily_micro_eur=excluded.daily_micro_eur,"
                "weekly_micro_eur=excluded.weekly_micro_eur,max_lifetime_days=excluded.max_lifetime_days,"
                "active_key_limit=excluded.active_key_limit",
                (
                    org_id,
                    values["mode"],
                    values["daily_requests"],
                    values["daily_micro_eur"],
                    values["weekly_micro_eur"],
                    values["max_lifetime_days"],
                    values["active_key_limit"],
                ),
            )
        self.audit("policy", None, org_id=org_id, actor=actor, **values)

    def digest(self, key: str) -> str:
        return hmac.new(self.pepper, key.encode(), hashlib.sha256).hexdigest()

    def day(self) -> str:
        return datetime.fromtimestamp(self.now(), timezone.utc).date().isoformat()

    @staticmethod
    def week_for_day(day: str) -> str:
        year, week, _ = date.fromisoformat(day).isocalendar()
        return f"{year}-W{week:02d}"

    def week(self) -> str:
        return self.week_for_day(self.day())

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
        daily_requests: int | None = None,
        daily_micro_eur: int | None = None,
        weekly_micro_eur: int | None = 100000000,
        own_budget: bool = False,
        privacy: str = "standard",
        lifetime_days: int = 90,
        browser_policy: bool = False,
        administrator: bool = False,
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
                v is not None and (type(v) is not int or v <= 0)
                for v in (daily_requests, daily_micro_eur, weekly_micro_eur)
            )
            or type(own_budget) is not bool
            or privacy not in ("standard", "strict")
        ):
            raise ValueError("invalid_key_policy")
        prefix = "atl_" + secrets.token_hex(4)
        key = prefix + "_" + secrets.token_urlsafe(32)
        identifier, now = secrets.token_hex(16), int(self.now())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if browser_policy:
                policy = self.policy(org_id)
                if not administrator and policy["mode"] != "self_service":
                    raise PermissionError("policy_denied")
                allowed = (
                    {7, 21, 30, 90}
                    if os.environ.get("ATLAS_STABLE") == "1"
                    else {30, 90, 365}
                )
                if (
                    lifetime_days not in allowed
                    or lifetime_days > policy["max_lifetime_days"]
                ):
                    raise ValueError("expiry_exceeds_policy")
                count = db.execute(
                    "SELECT COUNT(*) FROM api_keys WHERE org_id=? AND member_id=? "
                    "AND revoked_at IS NULL AND expires_at>?",
                    (org_id, member_id, now),
                ).fetchone()[0]
                if count >= policy["active_key_limit"]:
                    raise ValueError("active_key_limit")
                daily_requests = policy["daily_requests"]
                daily_micro_eur = policy["daily_micro_eur"]
                weekly_micro_eur = policy["weekly_micro_eur"]
                own_budget = policy["weekly_micro_eur"] is not None
            db.execute(
                "INSERT INTO api_keys (id,org_id,member_id,subject,budget_user,name,prefix,hash,hash_kind,scopes,expires_at,daily_requests,daily_micro_eur,weekly_micro_eur,own_budget,privacy,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    weekly_micro_eur,
                    int(own_budget),
                    privacy,
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
            (f"api-key:{row['id']}" if row["own_budget"] else str(row["budget_user"])),
            tuple(json.loads(row["scopes"])),
            str(row["privacy"]),
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
        day, week = self.day(), self.week()
        with self.connection() as db:
            row = db.execute(
                "SELECT COUNT(*) requests, COALESCE(SUM(charged),0) charged_micro_eur, "
                "COALESCE(SUM(CASE WHEN state!='complete' THEN charged ELSE 0 END),0) unknown_micro_eur "
                "FROM api_key_requests WHERE key_id=? AND day=?",
                (developer, self.day()),
            ).fetchone()
            weekly = db.execute(
                "SELECT COUNT(*) requests, COALESCE(SUM(charged),0) charged_micro_eur, "
                "COALESCE(SUM(CASE WHEN state!='complete' THEN charged ELSE 0 END),0) unknown_micro_eur "
                "FROM api_key_requests WHERE key_id=? AND week=?",
                (developer, week),
            ).fetchone()
        return dict(
            key_id=developer,
            day=day,
            **dict(row),
            daily_requests=owner["daily_requests"],
            daily_micro_eur=owner["daily_micro_eur"],
            week=week,
            week_usage=dict(weekly),
            weekly_micro_eur=owner["weekly_micro_eur"],
        )

    def reserve(self, developer: str, amount: int, *, monetary: bool = True) -> str:
        # Same atomic reserve/settle algorithm as dev_auth. The bound is the highest
        # per-call cap an operator may set (dev_gateway.call_cap, 5 EUR); the configured
        # cap itself is enforced by the gateway before any reservation reaches here.
        if type(amount) is not int or not 0 <= amount <= MAX_CALL_RESERVATION:
            raise ValueError("invalid_reservation")
        identifier, day, week = secrets.token_hex(16), self.day(), self.week()
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
            weekly_charged = db.execute(
                "SELECT COALESCE(SUM(charged),0) FROM api_key_requests WHERE key_id=? AND week=?",
                (developer, week),
            ).fetchone()[0]
            if (
                (
                    owner["daily_requests"] is not None
                    and count >= owner["daily_requests"]
                )
                or (
                    monetary
                    and owner["daily_micro_eur"] is not None
                    and charged + amount > owner["daily_micro_eur"]
                )
                or (
                    monetary
                    and owner["weekly_micro_eur"] is not None
                    and weekly_charged + amount > owner["weekly_micro_eur"]
                )
            ):
                failure = dict(
                    requests=count,
                    daily_requests=owner["daily_requests"],
                    charged_micro_eur=charged,
                    reserved_micro_eur=amount,
                    daily_micro_eur=owner["daily_micro_eur"],
                    week=week,
                    weekly_charged_micro_eur=weekly_charged,
                    weekly_micro_eur=owner["weekly_micro_eur"],
                )
            else:
                db.execute(
                    "INSERT INTO api_key_requests (id,key_id,day,week,reserved,charged,state) VALUES (?,?,?,?,?,?,?)",
                    (identifier, developer, day, week, amount, amount, "reserved"),
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
                "INSERT INTO api_keys (id,org_id,member_id,subject,budget_user,name,prefix,hash,hash_kind,scopes,expires_at,daily_requests,daily_micro_eur,created_by,created_at,last_used_at,last_used_ip_prefix,revoked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    "INSERT INTO api_key_requests (id,key_id,day,week,reserved,charged,input_tokens,output_tokens,state) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        row["id"],
                        identifier,
                        row["day"],
                        self.week_for_day(str(row["day"])),
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
