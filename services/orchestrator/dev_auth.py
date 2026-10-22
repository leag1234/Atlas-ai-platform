"""REQ-DEV-001/002: private keys and atomic daily reservations; no content storage."""

import argparse
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import secrets
import sqlite3


class QuotaError(ValueError):
    pass


def utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_mode & 0o077:
                raise PermissionError("private_database_required")
        finally:
            os.close(fd)
        schema = Path(__file__).resolve().parents[2] / "contracts/m15-storage.sql"
        with self.connection() as db:
            db.executescript(
                schema.read_text()
                .replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS")
                .replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS")
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def authenticate(self, key: str) -> str:
        if not 43 <= len(key) <= 100:
            raise PermissionError("unauthorized")
        with self.connection() as db:
            row = db.execute(
                "SELECT id FROM developers WHERE key_hash=? AND enabled=1",
                (hashlib.sha256(key.encode()).hexdigest(),),
            ).fetchone()
        if row is None:
            raise PermissionError("unauthorized")
        return str(row[0])

    def revoke(self, developer: str) -> None:
        with self.connection() as db:
            db.execute("UPDATE developers SET enabled=0 WHERE id=?", (developer,))

    def usage(self, developer: str) -> dict[str, object]:
        day = utc_day()
        with self.connection() as db:
            row = db.execute(
                "SELECT COUNT(*) requests, COALESCE(SUM(charged),0) charged_micro_eur, "
                "COALESCE(SUM(CASE WHEN state!='complete' THEN charged ELSE 0 END),0) "
                "unknown_micro_eur FROM requests WHERE developer=? AND day=?",
                (developer, day),
            ).fetchone()
        return {"developer": developer, "day": day, **dict(row)}

    def reserve(self, developer: str, amount: int) -> str:
        if type(amount) is not int or not 1 <= amount <= 50000:
            raise ValueError("invalid_reservation")
        request_id, day = secrets.token_hex(16), utc_day()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute(
                "SELECT * FROM developers WHERE id=? AND enabled=1", (developer,)
            ).fetchone()
            if owner is None:
                raise PermissionError("unauthorized")
            count, charged = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(charged),0) FROM requests "
                "WHERE developer=? AND day=?",
                (developer, day),
            ).fetchone()
            if (
                count >= owner["daily_requests"]
                or charged + amount > owner["daily_micro_eur"]
            ):
                raise QuotaError("quota_exceeded")
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,?,?,NULL,NULL,'reserved')",
                (request_id, developer, day, amount, amount),
            )
        return request_id

    def finish(
        self, request_id: str, charged: int | None, incoming: int = 0, outgoing: int = 0
    ) -> None:
        if any(type(v) is not int or v < 0 for v in (incoming, outgoing)) or (
            charged is not None and (type(charged) is not int or charged < 0)
        ):
            raise ValueError("invalid_usage")
        with self.connection() as db:
            cursor = db.execute(
                "UPDATE requests SET charged=COALESCE(?,charged),input_tokens=?,"
                "output_tokens=?,state=? WHERE id=? AND state='reserved'",
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


def provision(
    store: Store, developer: str, keyfile: Path, requests: int, budget: int
) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", developer) or any(
        type(v) is not int or v <= 0 for v in (requests, budget)
    ):
        raise ValueError("invalid_developer_or_quota")
    key = secrets.token_urlsafe(32)
    fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(key + "\n")
        with store.connection() as db:
            db.execute(
                "INSERT INTO developers VALUES (?,?,1,?,?)",
                (developer, hashlib.sha256(key.encode()).hexdigest(), requests, budget),
            )
    except (OSError, sqlite3.Error):
        keyfile.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local developer key administration; no secret stdout"
    )
    parser.add_argument("operation", choices=("create", "revoke"))
    parser.add_argument("developer")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--daily-requests", type=int, default=20)
    parser.add_argument("--daily-micro-eur", type=int, default=50000)
    args = parser.parse_args()
    store = Store(Path(os.environ.get("ATLAS_DEVAPI_DB", "BRAIN/devapi/usage.sqlite")))
    if args.operation == "revoke":
        store.revoke(args.developer)
    elif args.key_file is None:
        parser.error("--key-file required")
    else:
        provision(
            store,
            args.developer,
            args.key_file,
            args.daily_requests,
            args.daily_micro_eur,
        )


if __name__ == "__main__":
    main()
