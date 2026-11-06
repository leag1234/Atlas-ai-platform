"""Versioned local storage for generated HTML artefacts."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from services.orchestrator.request_owners import digest, usable


MAX_VERSIONS_PER_HOUR = 30
MAX_STORED_VERSIONS = 300
MAX_STORED_BYTES = 50 * 1024 * 1024
RETENTION_DAYS = 30


class ArtifactError(Exception):
    """A stable error code for callers and tests."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ArtifactVersion:
    artifact_id: str
    version: int
    filename: str
    content: bytes
    size_bytes: int
    sha256: str


def root() -> Path:
    return Path(os.environ.get("ATLAS_DATA_DIR", "BRAIN")) / "artifacts"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _limits() -> tuple[int, int, int, int]:
    return (
        _int_env("ATLAS_ARTIFACT_VERSIONS_PER_HOUR", MAX_VERSIONS_PER_HOUR),
        _int_env("ATLAS_ARTIFACT_MAX_VERSIONS", MAX_STORED_VERSIONS),
        _int_env("ATLAS_ARTIFACT_MAX_BYTES", MAX_STORED_BYTES),
        _int_env("ATLAS_ARTIFACT_RETENTION_DAYS", RETENTION_DAYS),
    )


def _safe_path(path: Path) -> bool:
    return not any(part.is_symlink() for part in path.parents) and not path.is_symlink()


def _connect() -> sqlite3.Connection:
    directory = root()
    if not _safe_path(directory):
        raise ArtifactError("unsafe_artifact_store")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / "index.sqlite"
    if path.is_symlink():
        raise ArtifactError("unsafe_artifact_store")
    db = sqlite3.connect(path, timeout=5)
    db.execute(
        "CREATE TABLE IF NOT EXISTS artifacts ("
        "id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL, "
        "filename TEXT NOT NULL, created_at INTEGER NOT NULL, "
        "updated_at INTEGER NOT NULL, current_version INTEGER NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS versions ("
        "artifact_id TEXT NOT NULL, version INTEGER NOT NULL, "
        "base_version INTEGER, created_at INTEGER NOT NULL, "
        "size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, "
        "request_id TEXT NOT NULL, PRIMARY KEY (artifact_id, version))"
    )
    path.chmod(0o600)
    return db


def _blob_path(artifact_id: str, version: int) -> Path:
    return root() / "blobs" / artifact_id / f"{version}.html"


def _write_blob(artifact_id: str, version: int, content: bytes) -> None:
    directory = _blob_path(artifact_id, version).parent
    if not _safe_path(directory):
        raise ArtifactError("unsafe_artifact_store")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory, prefix=f".{version}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_path, 0o600)
        destination = _blob_path(artifact_id, version)
        if not _safe_path(destination):
            raise ArtifactError("unsafe_artifact_store")
        os.replace(temporary_path, destination)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _quota(
    db: sqlite3.Connection, owner: str, size: int, now: int
) -> tuple[int, int, int, int]:
    limits = _limits()
    versions_per_hour, max_versions, max_bytes, _ = limits
    row = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(v.size_bytes), 0) "
        "FROM versions v JOIN artifacts a ON a.id = v.artifact_id "
        "WHERE a.owner = ? AND v.created_at >= ?",
        (owner, now - 3600),
    ).fetchone()
    stored = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(v.size_bytes), 0) FROM versions v "
        "JOIN artifacts a ON a.id = v.artifact_id WHERE a.owner = ?",
        (owner,),
    ).fetchone()
    return row[0], stored[0], stored[1], size


def create(
    identity: str | None,
    title: str,
    filename: str,
    content: bytes,
    request_id: str,
) -> dict[str, int | str]:
    return _save(None, identity, title, filename, content, request_id)


def update(
    artifact_id: str,
    identity: str | None,
    title: str,
    filename: str,
    content: bytes,
    request_id: str,
    base_version: int | None = None,
) -> dict[str, int | str]:
    return _save(
        artifact_id, identity, title, filename, content, request_id, base_version
    )


def _save(
    artifact_id: str | None,
    identity: str | None,
    title: str,
    filename: str,
    content: bytes,
    request_id: str,
    base_version: int | None = None,
) -> dict[str, int | str]:
    if not usable(identity):
        raise ArtifactError("artifact_owner_required")
    assert identity is not None
    if not isinstance(title, str) or not title.strip():
        raise ArtifactError("artifact_invalid")
    if not isinstance(filename, str) or not filename.strip():
        raise ArtifactError("artifact_invalid")
    if not isinstance(content, bytes) or not content:
        raise ArtifactError("artifact_invalid")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ArtifactError("artifact_invalid")
    owner = digest(identity)
    now = int(time.time())
    db = _connect()
    try:
        with db:
            if artifact_id is None:
                artifact_id = uuid4().hex
                version = 1
                _write_blob(artifact_id, version, content)
                db.execute(
                    "INSERT INTO artifacts "
                    "(id, owner, title, filename, created_at, updated_at, current_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (artifact_id, owner, title, filename, now, now, version),
                )
            else:
                row = db.execute(
                    "SELECT owner, current_version FROM artifacts WHERE id = ?",
                    (artifact_id,),
                ).fetchone()
                if row is None or not hmac.compare_digest(str(row[0]), owner):
                    raise ArtifactError("artifact_not_found")
                version = int(row[1]) + 1
                if base_version is not None and base_version != version - 1:
                    raise ArtifactError("artifact_conflict")
                _write_blob(artifact_id, version, content)
                db.execute(
                    "UPDATE artifacts SET title = ?, filename = ?, updated_at = ?, "
                    "current_version = ? WHERE id = ?",
                    (title, filename, now, version, artifact_id),
                )
            hourly, stored_count, stored_bytes, _ = _quota(db, owner, len(content), now)
            limits = _limits()
            if (
                hourly >= limits[0]
                or stored_count >= limits[1]
                or stored_bytes + len(content) > limits[2]
            ):
                raise ArtifactError("artifact_quota")
            db.execute(
                "INSERT INTO versions (artifact_id, version, base_version, created_at, "
                "size_bytes, sha256, request_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact_id,
                    version,
                    base_version,
                    now,
                    len(content),
                    hashlib.sha256(content).hexdigest(),
                    request_id,
                ),
            )
    except sqlite3.Error as exc:
        raise ArtifactError("artifact_store_unavailable") from exc
    finally:
        db.close()
    return {"id": artifact_id, "version": version}


def read(
    artifact_id: str, identity: str | None, version: int | None = None
) -> ArtifactVersion:
    if not usable(identity):
        raise ArtifactError("artifact_not_found")
    assert identity is not None
    owner = digest(identity)
    try:
        db = _connect()
        try:
            row = db.execute(
                "SELECT a.owner, a.current_version FROM artifacts a WHERE a.id = ?",
                (artifact_id,),
            ).fetchone()
            if row is None or not hmac.compare_digest(str(row[0]), owner):
                raise ArtifactError("artifact_not_found")
            selected = int(row[1]) if version is None else version
            version_row = db.execute(
                "SELECT a.filename, v.size_bytes, v.sha256, v.created_at "
                "FROM artifacts a JOIN versions v ON v.artifact_id = a.id "
                "AND v.version = ? WHERE a.id = ?",
                (selected, artifact_id),
            ).fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise ArtifactError("artifact_store_unavailable") from exc
    if version_row is None:
        raise ArtifactError("artifact_not_found")
    filename, size_bytes, expected_sha, created_at = version_row
    return _read_version(
        artifact_id, selected, filename, size_bytes, expected_sha, created_at
    )


def read_unowned(artifact_id: str, version: int) -> ArtifactVersion:
    """Read one token-bound version for the separated preview host."""

    try:
        db = _connect()
        try:
            version_row = db.execute(
                "SELECT a.filename, v.size_bytes, v.sha256, v.created_at "
                "FROM artifacts a JOIN versions v ON v.artifact_id = a.id "
                "AND v.version = ? WHERE a.id = ?",
                (version, artifact_id),
            ).fetchone()
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise ArtifactError("artifact_store_unavailable") from exc
    if version_row is None:
        raise ArtifactError("artifact_not_found")
    filename, size_bytes, expected_sha, created_at = version_row
    return _read_version(
        artifact_id, version, filename, size_bytes, expected_sha, created_at
    )


def _read_version(
    artifact_id: str,
    version: int,
    filename: str,
    size_bytes: int,
    expected_sha: str,
    created_at: int,
) -> ArtifactVersion:
    if created_at + _limits()[3] * 86400 <= int(time.time()):
        raise ArtifactError("artifact_not_found")
    path = _blob_path(artifact_id, version)
    if not _safe_path(path) or not path.is_file():
        raise ArtifactError("artifact_unavailable")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ArtifactError("artifact_unavailable") from exc
    if (
        len(content) != size_bytes
        or hashlib.sha256(content).hexdigest() != expected_sha
    ):
        raise ArtifactError("artifact_unavailable")
    return ArtifactVersion(
        artifact_id, version, filename, content, len(content), str(expected_sha)
    )


def purge(dry_run: bool = False) -> dict[str, int]:
    cutoff = int(time.time()) - _limits()[3] * 86400
    db = _connect()
    try:
        with db:
            rows = db.execute(
                "SELECT artifact_id, version FROM versions WHERE created_at <= ?",
                (cutoff,),
            ).fetchall()
            if dry_run:
                return {"versions": len(rows), "artifacts": 0}
            for artifact_id, version in rows:
                db.execute(
                    "DELETE FROM versions WHERE artifact_id = ? AND version = ?",
                    (artifact_id, version),
                )
            empty = db.execute(
                "SELECT id FROM artifacts WHERE id NOT IN (SELECT artifact_id FROM versions)"
            ).fetchall()
            for (artifact_id,) in empty:
                db.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
        for artifact_id, version in rows:
            try:
                _blob_path(artifact_id, version).unlink()
            except FileNotFoundError:
                pass
        return {"versions": len(rows), "artifacts": len(empty)}
    except sqlite3.Error as exc:
        raise ArtifactError("artifact_store_unavailable") from exc
    finally:
        db.close()


def _seed(path_argument: str) -> int:
    identity = os.environ.get("ATLAS_ARTIFACT_OWNER")
    if not usable(identity):
        raise ArtifactError("artifact_owner_required")
    path = Path(path_argument)
    result = create(identity, "seed", path.name, path.read_bytes(), "seed")
    print(result["id"], result["version"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    purge_parser = commands.add_parser("purge")
    purge_parser.add_argument("--dry-run", action="store_true")
    seed_parser = commands.add_parser("seed")
    seed_parser.add_argument("path")
    args = parser.parse_args(argv)
    if args.command == "purge":
        counts = purge(args.dry_run)
        print(f"versions={counts['versions']} artifacts={counts['artifacts']}")
        return 0
    return _seed(str(args.path))


if __name__ == "__main__":
    raise SystemExit(main())
