"""REQ-PROJ-03: explicit, resumable project migration; never run at startup.

Operator usage (from the repository, with ATLAS_RETRIEVAL_DSN set privately):
  python -m scripts.migrate_projects dry-run --legacy-db PATH --code-root PATH \
      --code-state PATH --recovery PATH --data-root PATH
  python -m scripts.migrate_projects migrate <same options> --writers-quiesced
  python -m scripts.migrate_projects resume --recovery PATH --data-root PATH \
      --writers-quiesced
  python -m scripts.migrate_projects restore --recovery PATH --data-root PATH \
      --restore-to EMPTY_PATH --writers-quiesced

--browser-import accepts an administrator-exported JSON list of per-chat browser
states: {id: session UUID, messages: [{user, answer, events}], queue: [text]}.
No client-supplied organization or owner is trusted. --owners optionally maps
legacy project/session UUIDs to active members of the installation organization.
Absent overrides use the installation administrator, never a product preset.
--exports-root points to a stopped terminal's /home/user/requests export tree.
Audited export URLs map to scoped immutable versions; unmapped bytes stay in the
private snapshot. Missing referenced exports quarantine the affected session.

The operator must stop legacy AND P0 writers before mutating commands. Source
stores remain untouched. Snapshots and receipts contain private project content;
keep the recovery directory private. Quarantined records block alias cutover.
Conversation, turn, session and alias adapters use the existing scoped legacy_rows
seam; files, memory, extraction and ledger use tasks B/C/D. No runtime schema change.
Restore is an offline recovery bundle in a NEW directory, including a transactionally
consistent P0 row export and current blobs/workspaces. It never rolls back a live
database, drops tombstones or discards post-migration P0 writes. The operator must
reapply expiry/tombstones with the normal readers before serving a restored store.
Use restore-database --restore-to BUNDLE with a DIFFERENT empty database DSN to
rehydrate that bundle. Existing schemas are rejected; this never replaces a live DB.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from typing import Any, Iterator
from uuid import UUID, uuid5

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from services.retrieval.project_documents import ScopedProjectDocuments
from services.retrieval.project_files import ProjectFiles
from services.retrieval.project_identity import Identity, ProjectError
from services.retrieval.project_policy import intersect
from services.retrieval.project_store import DB, Scope, entity
from services.retrieval.store import Chunk

TABLES = ("projects", "conversations", "turns", "facts", "project_documents")
DOWNLOAD_PREFIX = "/api/v1/terminals/atlas-files/files/serve/home/user/requests/"
USAGE = dict(
    tool_calls=0,
    model_calls=0,
    elapsed_seconds=0,
    cost_eur=0,
    reserved_eur=0,
    unknown_eur=0,
    stored_bytes=0,
    input_tokens=0,
    output_tokens=0,
    tokens_estimated=False,
)


def encoded(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        default=str,
    ).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def identifier(scope: str, key: str) -> str:
    return str(uuid5(UUID(scope), key))


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".pending")
    with temporary.open("wb") as handle:
        os.chmod(temporary, 0o600)
        handle.write(encoded(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def inventory(root: Path) -> dict[str, Any]:
    """Hash bytes and link text without ever following workspace symlinks."""
    result: dict[str, Any] = {}
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Expected a real directory")
    for current, directories, names in os.walk(root, followlinks=False):
        for name in sorted(directories + names):
            path = Path(current) / name
            mode = path.lstat().st_mode
            item: dict[str, Any] = dict(mode=stat.S_IMODE(mode))
            if stat.S_ISLNK(mode):
                item.update(kind="link", target=os.readlink(path))
            elif stat.S_ISREG(mode):
                item.update(
                    kind="file",
                    bytes=path.stat().st_size,
                    sha256=digest(path.read_bytes()),
                )
            elif stat.S_ISDIR(mode):
                item.update(kind="directory")
            else:
                raise ValueError("Special files cannot be migrated")
            result[path.relative_to(root).as_posix()] = item
    return result


def copy_tree(source: Path, destination: Path) -> None:
    before = inventory(source)
    shutil.copytree(source, destination, symlinks=True)
    if inventory(source) != before or inventory(destination) != before:
        raise ValueError("Source changed or copied bytes failed verification")
    for name, item in before.items():
        if item["kind"] == "file":
            with (destination / name).open("rb") as handle:
                os.fsync(handle.fileno())


def legacy_rows(path: Path) -> dict[str, list[dict[str, Any]]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Legacy database must be a regular file")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not set(TABLES[:-1]).issubset(tables):
            raise ValueError("Incomplete legacy project database")
        return {
            table: [
                dict(row)
                for row in db.execute(
                    f"SELECT rowid AS ordinal,* FROM {table} ORDER BY rowid"
                )
            ]
            if table in tables
            else []
            for table in TABLES
        }


def snapshot_database(source: Path, destination: Path) -> None:
    with closing(
        sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        with closing(sqlite3.connect(destination)) as target:
            db.backup(target)
    os.chmod(destination, 0o600)


@contextmanager
def operator(dsn: str, *, snapshot: bool = False) -> Iterator[DB]:
    with psycopg.connect(dsn, row_factory=dict_row) as db:
        if snapshot:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        role = db.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        if not role or not role["rolsuper"]:
            raise ValueError("Migration requires the database operator")
        yield db


def installation(db: DB) -> tuple[dict[str, Any], dict[str, Any]]:
    row = db.execute("SELECT * FROM atlas_projects.installation").fetchone()
    if row is None:
        raise ValueError("Run administrator installation first")
    member = db.execute(
        "SELECT * FROM atlas_projects.members WHERE org_id=%s AND id=%s AND status='active' AND role='admin'",
        (row["initial_org"], row["initial_admin"]),
    ).fetchone()
    if member is None:
        raise ValueError("Installation administrator must be active")
    return row, member


def plan(
    dsn: str,
    legacy: Path,
    code: Path,
    state: Path,
    browser: Path | None = None,
    owners: dict[str, str] | None = None,
    exports: Path | None = None,
) -> dict[str, Any]:
    rows = legacy_rows(legacy)
    with operator(dsn) as db:
        install, admin = installation(db)
        org = str(install["initial_org"])
        active = {
            str(row["id"])
            for row in db.execute(
                "SELECT id FROM atlas_projects.members WHERE org_id=%s AND status='active'",
                (org,),
            )
        }
    browser_data = json.loads(browser.read_bytes()) if browser else []
    if not isinstance(browser_data, list):
        raise ValueError("Browser import must be a list of per-chat states")
    code_inventory, state_inventory = inventory(code), inventory(state)
    exports_inventory = inventory(exports) if exports else {}
    mapped_exports: set[str] = set()
    browser_ids: set[str] = set()
    for item in browser_data:
        session = str(UUID(item["id"]))
        if (
            session in browser_ids
            or not isinstance(item.get("messages"), list)
            or not isinstance(item.get("queue"), list)
        ):
            raise ValueError("Duplicate or invalid browser import")
        browser_ids.add(session)
    sessions = {path.split("/")[0] for path in code_inventory}
    sessions.update(path.split("/")[0].split(".")[0] for path in state_inventory)
    sessions.update(UUID(key).hex for key in browser_ids)
    projects = []
    quarantined = []
    known = {row["id"] for row in rows["projects"]}
    for table in TABLES[1:]:
        for row in rows[table]:
            if row["project"] not in known:
                quarantined.append(
                    dict(
                        kind=table,
                        legacy_id=str(row.get("id", row["ordinal"])),
                        reason="unknown_project",
                    )
                )
    for kind, key, old in [("document", row["id"], row) for row in rows["projects"]] + [
        ("code", key, {}) for key in sorted(sessions)
    ]:
        try:
            legacy_id = str(UUID(key))
        except ValueError:
            quarantined.append(
                dict(kind=kind, legacy_id=key, reason="invalid_legacy_id")
            )
            continue
        owner = (owners or {}).get(legacy_id, str(admin["id"]))
        reason = None
        downloads: dict[str, str] = {}
        if owner not in active:
            reason = "unknown_ownership"
        if kind == "code":
            key = UUID(legacy_id).hex
            metadata = state / (key + ".json")
            audit = state / (key + ".jsonl")
            workspace = code / key
            if (
                not metadata.is_file()
                or metadata.is_symlink()
                or not audit.is_file()
                or audit.is_symlink()
                or not workspace.is_dir()
                or workspace.is_symlink()
            ):
                reason = "missing_code_state"
            else:
                try:
                    if (workspace / ".git").is_symlink():
                        raise ValueError("Git metadata must stay in the workspace")
                    for name, entry in inventory(workspace).items():
                        if entry["kind"] == "link" and not (
                            workspace / name
                        ).resolve().is_relative_to(workspace.resolve()):
                            raise ValueError("Workspace link escapes its session")
                    meta = json.loads(metadata.read_bytes())
                    baseline = meta["baseline"]
                    if (
                        not isinstance(baseline, str)
                        or len(baseline) not in (40, 64)
                        or any(c not in "0123456789abcdef" for c in baseline)
                    ):
                        raise ValueError("Invalid baseline")
                    for line in audit.read_text().splitlines():
                        event = json.loads(line)
                        if event.get("type") == "export":
                            for url in event.get("output", {}).get("files", []):
                                relative = url.removeprefix(DOWNLOAD_PREFIX)
                                if (
                                    not url.startswith(DOWNLOAD_PREFIX)
                                    or not re.fullmatch(
                                        r"chatcmpl-[a-f0-9]{32}/[A-Za-z0-9][A-Za-z0-9_.-]*",
                                        relative,
                                    )
                                    or relative.split("/")[0] != event.get("turn_id")
                                ):
                                    raise ValueError(
                                        "Export URL does not belong to the audited turn"
                                    )
                                downloads[url] = relative
                                if (
                                    exports_inventory.get(relative, {}).get("kind")
                                    != "file"
                                ):
                                    reason = "missing_export"
                                mapped_exports.add(relative)
                    check = subprocess.run(
                        [
                            "git",
                            "--git-dir",
                            str(workspace / ".git"),
                            "cat-file",
                            "-e",
                            baseline + "^{commit}",
                        ],
                        capture_output=True,
                    )
                    if check.returncode:
                        reason = "missing_baseline"
                    if not isinstance(meta.get("plan", []), list):
                        reason = "invalid_plan"
                except (ValueError, KeyError, TypeError):
                    reason = "invalid_code_state"
        if reason:
            quarantined.append(dict(kind=kind, legacy_id=legacy_id, reason=reason))
            continue
        projects.append(
            dict(
                kind=kind,
                legacy_id=legacy_id,
                owner=owner,
                project_id=legacy_id
                if kind == "document"
                else identifier(org, "code:" + legacy_id),
                name=old.get("name", "Imported Code session " + legacy_id[:8]),
                downloads=downloads,
            )
        )
    counts = {table: len(values) for table, values in rows.items()}
    counts.update(
        code_sessions=len(sessions),
        browser_chats=len(browser_data),
        chunks=sum(len(json.loads(row["chunks"])) for row in rows["project_documents"]),
    )
    return dict(
        format=1,
        org_id=org,
        administrator_member_id=str(admin["id"]),
        projects=projects,
        quarantined=quarantined,
        counts=counts,
        legacy_sha256=digest(encoded(rows)),
        code_inventory=code_inventory,
        state_inventory=state_inventory,
        exports_inventory=exports_inventory,
        unmapped_exports=sorted(
            name
            for name, entry in exports_inventory.items()
            if entry["kind"] == "file" and name not in mapped_exports
        ),
        browser_sha256=digest(encoded(browser_data)),
        browser_history="available_client_import" if browser else "unavailable",
        owners=owners or {},
        completed=[],
        status="planned",
    )


def prepare(
    dsn: str,
    legacy: Path,
    code: Path,
    state: Path,
    recovery: Path,
    data_root: Path,
    *,
    browser: Path | None = None,
    owners: dict[str, str] | None = None,
    exports: Path | None = None,
    quiesced: bool = False,
) -> dict[str, Any]:
    if not quiesced:
        raise ValueError("Quiesce legacy and P0 writers before migration")
    result = plan(dsn, legacy, code, state, browser, owners, exports)
    for source in (
        legacy,
        code,
        state,
        *([exports] if exports else []),
        *([browser] if browser else []),
    ):
        for target in (recovery, data_root):
            if (
                source.resolve() == target.resolve()
                or source.resolve() in target.resolve().parents
                or target.resolve() in source.resolve().parents
            ):
                raise ValueError("Source, recovery and destination must be disjoint")
    if (
        recovery.resolve() == data_root.resolve()
        or recovery.resolve() in data_root.resolve().parents
        or data_root.resolve() in recovery.resolve().parents
    ):
        raise ValueError("Recovery and destination must be disjoint")
    recovery.mkdir(mode=0o700)
    snapshot = recovery / "snapshot"
    snapshot.mkdir(mode=0o700)
    snapshot_database(legacy, snapshot / "projects.sqlite")
    copy_tree(code, snapshot / "code")
    copy_tree(state, snapshot / "state")
    if exports:
        copy_tree(exports, snapshot / "exports")
    else:
        (snapshot / "exports").mkdir(mode=0o700)
    write_json(
        snapshot / "browser.json", json.loads(browser.read_bytes()) if browser else []
    )
    if (
        digest(encoded(legacy_rows(snapshot / "projects.sqlite")))
        != result["legacy_sha256"]
        or inventory(snapshot / "code") != result["code_inventory"]
        or inventory(snapshot / "state") != result["state_inventory"]
        or inventory(snapshot / "exports") != result["exports_inventory"]
        or digest((snapshot / "browser.json").read_bytes()) != result["browser_sha256"]
    ):
        raise ValueError("Sources changed during snapshot; keep writers quiesced")
    result.update(
        snapshot_inventory=inventory(snapshot), data_root=str(data_root.resolve())
    )
    # Rehearse a byte-for-byte restore before allowing any target publication.
    with tempfile.TemporaryDirectory(dir=recovery) as temporary:
        copy_tree(snapshot, Path(temporary) / "restored")
    result.update(restore_rehearsed=True, status="snapshotted")
    write_json(recovery / "manifest.json", result)
    return result


def saved(recovery: Path, data_root: Path) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((recovery / "manifest.json").read_bytes())
    if (
        result["data_root"] != str(data_root.resolve())
        or inventory(recovery / "snapshot") != result["snapshot_inventory"]
    ):
        raise ValueError("Recovery snapshot or destination differs from manifest")
    return result


def row_put(
    db: DB,
    org: str,
    project: str,
    kind: str,
    key: str,
    ordinal: int,
    data: dict[str, Any],
) -> None:
    previous = db.execute(
        "SELECT data,ordinal FROM atlas_projects.legacy_rows WHERE org_id=%s AND project_id=%s AND kind=%s AND id=%s",
        (org, project, kind, key),
    ).fetchone()
    if previous:
        if previous["data"] != data or previous["ordinal"] != ordinal:
            raise ValueError(
                "Existing scoped legacy record differs; preserve and investigate"
            )
        return
    db.execute(
        "INSERT INTO atlas_projects.legacy_rows VALUES(%s,%s,%s,%s,%s,%s)",
        (org, project, kind, key, ordinal, Jsonb(data)),
    )


def scope_for(db: DB, manifest: dict[str, Any], item: dict[str, Any]) -> Scope:
    org, owner, project = manifest["org_id"], item["owner"], item["project_id"]
    install, _ = installation(db)
    if (
        str(install["initial_org"]) != org
        or str(install["initial_admin"]) != manifest["administrator_member_id"]
    ):
        raise ValueError("Installation ownership changed")
    organization = db.execute(
        "SELECT * FROM atlas_projects.organizations WHERE org_id=%s", (org,)
    ).fetchone()
    member = db.execute(
        "SELECT * FROM atlas_projects.members WHERE org_id=%s AND id=%s AND status='active'",
        (org, owner),
    ).fetchone()
    if organization is None or member is None:
        raise ValueError("Owner is no longer active")
    identity = Identity(
        subject=member["subject"],
        member=owner,
        org=org,
        project=project,
        membership_revision=member["revision"],
        policy_revision=organization["revision"],
        expires=0,
    )
    for key, value in (
        ("org", org),
        ("member", owner),
        ("subject", member["subject"]),
        ("project", project),
    ):
        db.execute("SELECT set_config(%s,%s,true)", ("atlas." + key, value))
    scope = Scope(
        db,
        identity,
        member,
        organization,
        intersect(install["ceilings"], organization["policy"]),
    )
    scope.role = "owner"
    scope.project = db.execute(
        "SELECT * FROM atlas_projects.projects WHERE org_id=%s AND id=%s",
        (org, project),
    ).fetchone()
    return scope


def create_project(
    scope: Scope, item: dict[str, Any], instructions: str, revision: int
) -> ProjectFiles:
    db, org, owner = scope.db, scope.identity.org, scope.identity.member
    project = item["project_id"]
    if scope.project is None:
        scope.check_quota("active_projects", 1)
        value = entity(owner, org, project)
        value.update(
            name=item["name"],
            owner_member_id=owner,
            state="active",
            default_action="chat",
            instructions_file_id=identifier(project, "instructions"),
            memory_revision=revision,
            deliverables_folder_id=identifier(project, "deliverables"),
            retention_override=None,
            policy_revision=scope.org["revision"],
        )
        db.execute(
            "INSERT INTO atlas_projects.projects(org_id,id,owner_member_id,state,data,instructions) VALUES(%s,%s,%s,'active',%s,%s)",
            (org, project, owner, Jsonb(value), instructions),
        )
        grant = dict(
            entity(owner, org),
            project_id=project,
            member_id=owner,
            role="owner",
            revision=1,
        )
        db.execute(
            "INSERT INTO atlas_projects.grants VALUES(%s,%s,%s,'owner',1,%s)",
            (org, project, owner, Jsonb(grant)),
        )
        scope.project = dict(id=project, data=value, state="active", revision=0)
    else:
        existing = scope.project
        if (
            str(existing["owner_member_id"]) != owner
            or existing["instructions"] != instructions
            or existing["data"]["name"] != item["name"]
            or existing["data"]["memory_revision"] != revision
            or existing["revision"] != 0
            or existing["state"] != "active"
        ):
            raise ValueError("Existing project differs from legacy snapshot")
        if db.execute(
            "SELECT 1 FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s LIMIT 1",
            (org, project),
        ).fetchone():
            raise ValueError(
                "Existing project contains P0 writes without a migration receipt"
            )
    return ProjectFiles(scope)


def conversation(
    files: ProjectFiles, key: str, title: str, alias: str | None, ordinal: int = 0
) -> str:
    value = dict(
        files.new(key),
        title=title,
        owner_member_id=files.scope.identity.member,
        last_action="chat",
        legacy_chat_id=alias,
    )
    row_put(files.db, files.org, files.project, "conversation", key, ordinal, value)
    return key


def turn(
    files: ProjectFiles, old: dict[str, Any], conversation_id: str, provenance: str
) -> None:
    ordinal = old["ordinal"]
    key = identifier(files.project, provenance + ":turn:" + str(ordinal))
    revision = old.get("revision", -1)
    value = dict(
        files.new(key),
        conversation_id=conversation_id,
        action="chat",
        request_id=identifier(key, "request"),
        content_refs=[
            dict(kind=kind, text=old[field], file_version_id=None)
            for kind, field in (("question", "question"), ("answer", "answer"))
            if old.get(field)
        ],
        file_version_ids=[],
        context_revision=revision,
        state="stale" if revision == -1 else "completed",
        usage=USAGE,
        persistability="content",
    )
    row_put(files.db, files.org, files.project, "turn", key, ordinal, value)


def document(files: ProjectFiles, old: dict[str, Any], folder: dict[str, Any]) -> None:
    chunks = json.loads(old["chunks"])
    for chunk in chunks:
        Chunk.model_validate(chunk)
        if chunk["doc_id"] != old["id"]:
            raise ValueError("Chunk belongs to a different document")
    file, version = (
        files.new(old["id"]),
        files.new(identifier(old["id"], "legacy-version")),
    )
    metadata = dict(
        mime="application/octet-stream",
        size_bytes=0,
        sha256=None,
        origin="legacy_extracted",
        availability="not_retained",
        source_locator=None,
        index_status="ready",
    )
    # Original names stay verbatim in provenance; IDs avoid illegal paths/collisions.
    file.update(
        metadata,
        folder_id=folder["id"],
        relative_path=old["id"],
        version_id=version["id"],
        classification="internal",
        locality="server",
    )
    version.update(metadata, file_id=file["id"], parent_version_id=None)
    files.put(
        "file",
        file,
        folder["id"],
        private=dict(legacy_name=old["name"], legacy_id=old["id"]),
    )
    files.put("version", version, file["id"])
    derived = dict(
        files.new(identifier(old["id"], "legacy-extraction")),
        chunks=[
            dict(
                file_id=file["id"],
                version_id=version["id"],
                chunk_id=chunk["chunk_id"],
                locator=dict(kind="paragraph", paragraph=chunk["position"]),
                text=chunk["text"],
            )
            for chunk in chunks
        ],
    )
    size = len(old["chunks"].encode())
    files.scope.reserve(derived["id"], {"derived_bytes": size})
    files.scope.reconcile(derived["id"], "derived_bytes", size)
    files.put(
        "derived",
        derived,
        version["id"],
        retention="E",
        private=dict(legacy_chunks=chunks, legacy_chunks_json=old["chunks"]),
    )
    files.emit("extraction_completed", [version["id"]])
    for chunk in chunks:
        row_put(
            files.db,
            files.org,
            files.project,
            "source_alias",
            chunk["chunk_id"],
            chunk["position"],
            dict(
                file_id=file["id"], version_id=version["id"], chunk_id=chunk["chunk_id"]
            ),
        )
        if (
            ScopedProjectDocuments(files).resolve(chunk["chunk_id"])["text"]
            != chunk["text"]
        ):
            raise ValueError("Migrated citation failed verification")
    try:
        files.content(file["id"])
    except ProjectError as exc:
        if exc.status != 404:
            raise
    else:
        raise ValueError("Legacy original was fabricated")


def import_documents(
    files: ProjectFiles, rows: dict[str, list[dict[str, Any]]]
) -> None:
    folder = files.folder(
        dict(name="Legacy documents", locality="server", model_use="allow")
    )
    selected = {
        table: [old for old in rows[table] if old["project"] == files.project]
        for table in TABLES[1:]
    }
    conversations = {old["id"] for old in selected["conversations"]}
    for table, values in selected.items():
        for old in values:
            row_put(
                files.db,
                files.org,
                files.project,
                table,
                str(old.get("id", old["ordinal"])),
                old["ordinal"],
                old,
            )
            if table == "conversations":
                conversation(files, old["id"], old["name"], old["id"], old["ordinal"])
            elif table == "turns":
                if old["conversation"] not in conversations:
                    raise ValueError("Turn has unknown conversation")
                turn(files, old, old["conversation"], "legacy")
            elif table == "facts":
                if old["conversation_id"] not in conversations:
                    raise ValueError("Fact has unknown conversation")
                value = dict(
                    files.new(old["id"]),
                    text=old["text"],
                    kind="legacy",
                    source_conversation_id=old["conversation_id"],
                    source_turn_id=None,
                    source_file_version_ids=[],
                    status="active",
                    confirmed_by=None,
                    memory_revision=files.scope.project_data["memory_revision"],
                )
                files.put(
                    "memory",
                    value,
                    private=dict(provenance="legacy", legacy_id=old["id"]),
                )
            elif table == "project_documents":
                document(files, old, folder)


def import_code(files: ProjectFiles, item: dict[str, Any], snapshot: Path) -> None:
    session = item["legacy_id"]
    key = UUID(session).hex
    metadata = json.loads((snapshot / "state" / (key + ".json")).read_bytes())
    parent = files.root / "orgs" / files.org / "projects" / files.project
    work = parent / "work" / session
    if work.exists():
        if inventory(work) != inventory(snapshot / "code" / key):
            raise ValueError("Pending workspace differs; never overwrite P0 writes")
    else:
        work.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        copy_tree(snapshot / "code" / key, work)
    archive = parent / "legacy" / session
    archive.mkdir(parents=True, exist_ok=True, mode=0o700)
    for suffix in (".json", ".jsonl"):
        source = snapshot / "state" / (key + suffix)
        target = archive / ("state" + suffix)
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise ValueError("Pending audit differs")
        if not target.exists():
            shutil.copy2(source, target)
    conv = conversation(
        files,
        identifier(session, "conversation"),
        "Imported Code conversation",
        session,
    )
    value = dict(
        files.new(session),
        conversation_id=conv,
        initiator_member_id=files.scope.identity.member,
        workspace_id=session,
        base_file_versions=[],
        repository_id=None,
        state="ready",
        plan=metadata.get("plan", []),
        effective_limits=files.scope.policy["quotas"]["per_turn"],
        usage=USAGE,
        lease=None,
        last_event_seq=0,
        legacy_session_id=session,
        pending_approvals=[],
        environment=dict(
            location="server",
            workspace_id=session,
            network=False,
            device_available=False,
        ),
    )
    row_put(files.db, files.org, files.project, "work_session", session, 0, value)
    row_put(
        files.db,
        files.org,
        files.project,
        "code_provenance",
        session,
        0,
        dict(
            legacy_session_id=session,
            baseline=metadata["baseline"],
            metadata=metadata,
            workspace=f"work/{session}",
            audit=f"legacy/{session}/state.jsonl",
            forge_capability=False,
            browser_history="unavailable",
        ),
    )
    browser = json.loads((snapshot / "browser.json").read_bytes())
    for state in browser:
        if str(UUID(state["id"])) != session:
            continue
        row_put(
            files.db,
            files.org,
            files.project,
            "client_import",
            session,
            0,
            dict(
                provenance="client_import",
                imported_by_member_id=files.scope.identity.member,
                sha256=digest(encoded(state)),
                state=state,
            ),
        )
        for ordinal, message in enumerate(state["messages"], 1):
            turn(
                files,
                dict(
                    ordinal=ordinal,
                    question=message["user"],
                    answer=message.get("answer", ""),
                ),
                conv,
                "client_import",
            )
    row_put(
        files.db,
        files.org,
        files.project,
        "session_alias",
        session,
        0,
        dict(session_id=session, project_id=files.project),
    )
    for ordinal, (url, relative) in enumerate(item["downloads"].items()):
        folder = files.get("folder", files.scope.project_data["deliverables_folder_id"])
        content = (snapshot / "exports" / relative).read_bytes()
        file = files.create_file(
            folder["id"], relative, content, folder["revision"], origin="work"
        )
        if files.content(file["id"])[1] != content:
            raise ValueError("Migrated export download differs")
        row_put(
            files.db,
            files.org,
            files.project,
            "download_alias",
            url,
            ordinal,
            dict(
                file_id=file["id"],
                version_id=file["version_id"],
                sha256=file["sha256"],
                requires_scope=True,
            ),
        )


def project_rows(db: DB, org: str, project: str) -> dict[str, Any]:
    result = {}
    for table in (
        "projects",
        "grants",
        "resources",
        "resource_sources",
        "legacy_rows",
        "reservations",
        "ledger_events",
        "outbox",
        "audit",
    ):
        field = "id" if table == "projects" else "project_id"
        query = sql.SQL(
            "SELECT * FROM atlas_projects.{} WHERE org_id=%s AND {}=%s"
        ).format(sql.Identifier(table), sql.Identifier(field))
        values = [dict(row) for row in db.execute(query, (org, project))]
        if table == "legacy_rows":
            values = [
                row
                for row in values
                if row["kind"] not in ("migration_receipt", "migration_aliases")
            ]
        result[table] = sorted(values, key=encoded)
    return result


def stage_project(recovery: Path, data_root: Path, org: str, project: str) -> Path:
    """Recover a crash between filesystem publication and transaction commit."""
    target = data_root / "orgs" / org / "projects" / project
    pending = recovery / (project + ".json")
    if target.exists():
        if not pending.exists() or inventory(target) != json.loads(
            pending.read_bytes()
        ):
            raise ValueError(
                "Uncommitted destination changed; preserve for operator recovery"
            )
        # No committed receipt or P0 resources exist. Preserve the abandoned copy.
        abandoned = recovery / "abandoned"
        abandoned.mkdir(exist_ok=True, mode=0o700)
        target.rename(abandoned / (project + "-" + str(len(list(abandoned.iterdir())))))
    staging = recovery / "staging" / project
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, mode=0o700)
    return staging


def publish_project(
    recovery: Path, files: ProjectFiles, data_root: Path
) -> dict[str, Any]:
    relative = Path("orgs") / files.org / "projects" / files.project
    source, target = files.root / relative, data_root / relative
    expected = inventory(source)
    write_json(recovery / (files.project + ".json"), expected)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Copy into the destination filesystem, then publish atomically within it.
    pending = target.with_name("." + target.name + ".pending")
    if pending.exists():
        shutil.rmtree(pending)
    copy_tree(source, pending)
    os.rename(pending, target)
    descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return expected


@contextmanager
def recovery_lock(recovery: Path) -> Iterator[None]:
    with (recovery / "lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def migrate(
    dsn: str, recovery: Path, data_root: Path, *, quiesced: bool = False
) -> dict[str, Any]:
    if not quiesced:
        raise ValueError("Quiesce legacy and P0 writers before migration")
    with recovery_lock(recovery):
        manifest = saved(recovery, data_root)
        snapshot = recovery / "snapshot"
        rows = legacy_rows(snapshot / "projects.sqlite")
        for item in manifest["projects"]:
            project = item["project_id"]
            with operator(dsn) as db:
                db.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (manifest["org_id"],),
                )
                scope = scope_for(db, manifest, item)
                receipt = db.execute(
                    "SELECT data FROM atlas_projects.legacy_rows WHERE org_id=%s AND project_id=%s AND kind='migration_receipt' AND id=%s",
                    (manifest["org_id"], project, manifest["legacy_sha256"]),
                ).fetchone()
                if receipt:
                    # A committed receipt wins over an interrupted manifest write.
                    # Never re-import or reset a project with subsequent P0 writes.
                    if receipt["data"]["snapshot_sha256"] != digest(
                        encoded(manifest["snapshot_inventory"])
                    ):
                        raise ValueError("Receipt belongs to another snapshot")
                else:
                    old = next(
                        (old for old in rows["projects"] if old["id"] == project), {}
                    )
                    files = create_project(
                        scope, item, old.get("instructions", ""), old.get("revision", 0)
                    )
                    files.root = stage_project(recovery, data_root, files.org, project)
                    try:
                        files.initialize(old.get("instructions", ""))
                        size = len(old.get("instructions", "").encode())
                        amounts: dict[str, float] = {
                            "stored_bytes": size,
                            "versioned_bytes": size,
                        }
                        scope.reserve(project, amounts)
                        for resource, amount in amounts.items():
                            scope.reconcile(project, resource, amount)
                        row_put(
                            db,
                            files.org,
                            project,
                            "project_provenance",
                            item["legacy_id"],
                            0,
                            dict(
                                kind=item["kind"],
                                legacy_id=item["legacy_id"],
                                original=old,
                            ),
                        )
                        if item["kind"] == "document":
                            import_documents(files, rows)
                        else:
                            import_code(files, item, snapshot)
                        if (
                            files.content(scope.project_data["instructions_file_id"])[1]
                            != old.get("instructions", "").encode()
                        ):
                            raise ValueError("Instructions download differs")
                        verification = dict(
                            snapshot_sha256=digest(
                                encoded(manifest["snapshot_inventory"])
                            ),
                            rows_sha256=digest(
                                encoded(project_rows(db, files.org, project))
                            ),
                            files=publish_project(recovery, files, data_root),
                            counts=manifest["counts"],
                        )
                        row_put(
                            db,
                            files.org,
                            project,
                            "migration_receipt",
                            manifest["legacy_sha256"],
                            0,
                            verification,
                        )
                    except BaseException:
                        for path in scope.rollback_files:
                            path.unlink(missing_ok=True)
                        raise
            if project not in manifest["completed"]:
                manifest["completed"].append(project)
            manifest["status"] = "copying"
            write_json(recovery / "manifest.json", manifest)
            staging = recovery / "staging" / project
            if staging.exists():
                shutil.rmtree(staging)
        # Publish mappings in one transaction only after all projects and rehearsal.
        if not manifest["quarantined"]:
            with operator(dsn) as db:
                for item in manifest["projects"]:
                    row_put(
                        db,
                        manifest["org_id"],
                        item["project_id"],
                        "migration_aliases",
                        item["legacy_id"],
                        0,
                        dict(
                            kind=item["kind"],
                            legacy_id=item["legacy_id"],
                            project_id=item["project_id"],
                            requires_scope=True,
                        ),
                    )
            manifest["status"] = "verified"
        else:
            manifest["status"] = "quarantined"
        write_json(recovery / "manifest.json", manifest)
        return manifest


def restore(
    dsn: str,
    recovery: Path,
    data_root: Path,
    destination: Path,
    *,
    quiesced: bool = False,
) -> dict[str, Any]:
    if not quiesced:
        raise ValueError("Quiesce legacy and P0 writers before recovery")
    with recovery_lock(recovery):
        manifest = saved(recovery, data_root)
        for source in (recovery, data_root):
            if (
                destination.resolve() == source.resolve()
                or source.resolve() in destination.resolve().parents
                or destination.resolve() in source.resolve().parents
            ):
                raise ValueError(
                    "Restore destination must be separate from existing stores"
                )
        destination.mkdir(mode=0o700)
        copy_tree(recovery / "snapshot", destination / "legacy")
        with operator(dsn, snapshot=True) as db:
            tables = db.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='atlas_projects' ORDER BY tablename"
            ).fetchall()
            exported = {
                row["tablename"]: list(
                    db.execute(
                        sql.SQL("SELECT * FROM atlas_projects.{}").format(
                            sql.Identifier(row["tablename"])
                        )
                    )
                )
                for row in tables
            }
            write_json(destination / "p0-database.json", exported)
            if data_root.exists():
                copy_tree(data_root, destination / "p0-data")
        write_json(destination / "manifest.json", manifest)
        schema_root = Path(__file__).resolve().parents[1] / "services/retrieval"
        schema = (schema_root / "project_schema.sql").read_text()
        if "work_leases" in exported:
            schema += "\n" + (schema_root / "project_work_schema.sql").read_text()
        (destination / "schema.sql").write_text(schema)
        evidence = dict(
            status="restored_offline",
            preserves_p0_writes=True,
            files=inventory(destination),
        )
        write_json(destination / "recovery.json", evidence)
        return evidence


def restore_database(dsn: str, bundle: Path) -> dict[str, Any]:
    """Rehydrate an offline bundle only into a database without project state."""
    evidence = json.loads((bundle / "recovery.json").read_bytes())
    actual = inventory(bundle)
    actual.pop("recovery.json")
    if actual != evidence["files"]:
        raise ValueError("Recovery bundle failed integrity verification")
    exported = json.loads((bundle / "p0-database.json").read_bytes())
    order: tuple[str, ...] = (
        "installation",
        "organizations",
        "members",
        "projects",
        "grants",
        "legacy_rows",
        "reservations",
        "requests",
        "audit",
        "ledger_events",
        "outbox",
        "resources",
        "resource_sources",
        "file_approvals",
    )
    if {"work_leases", "work_progress"} <= set(exported):
        order += ("work_leases", "work_progress")
    if "forge_operations" in exported:
        order += ("forge_operations",)
    if set(exported) != set(order):
        raise ValueError("Recovery schema differs from the supported project store")
    with operator(dsn) as db:
        if db.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname='atlas_projects'"
        ).fetchone():
            raise ValueError("Restore requires an empty project database")
        db.execute((bundle / "schema.sql").read_text())
        for table in order:
            rows = list(exported[table])
            if table == "resources":
                ordered: list[dict[str, Any]] = []
                seen: set[tuple[str, str, str]] = set()
                while rows:
                    eligible = [
                        row
                        for row in rows
                        if row["parent_id"] is None
                        or (row["org_id"], row["project_id"], row["parent_id"]) in seen
                    ]
                    if not eligible:
                        raise ValueError(
                            "Recovery resource parents are missing or cyclic"
                        )
                    for row in eligible:
                        rows.remove(row)
                        ordered.append(row)
                        seen.add((row["org_id"], row["project_id"], row["id"]))
                rows = ordered
            for row in rows:
                columns = list(row)
                statement = sql.SQL(
                    "INSERT INTO atlas_projects.{} ({}) OVERRIDING SYSTEM VALUE VALUES ({})"
                ).format(
                    sql.Identifier(table),
                    sql.SQL(",").join(map(sql.Identifier, columns)),
                    sql.SQL(",").join(sql.Placeholder() for _ in columns),
                )
                db.execute(
                    statement,
                    [
                        Jsonb(row[key])
                        if isinstance(row[key], (dict, list))
                        else row[key]
                        for key in columns
                    ],
                )
            restored = list(
                db.execute(
                    sql.SQL("SELECT * FROM atlas_projects.{}").format(
                        sql.Identifier(table)
                    )
                )
            )
            if encoded(sorted(restored, key=encoded)) != encoded(
                sorted(exported[table], key=encoded)
            ):
                raise ValueError("Restored rows differ from the recovery bundle")
        sequence_rows = "SELECT seq FROM atlas_projects.ledger_events"
        if "work_progress" in exported:
            sequence_rows += " UNION ALL SELECT seq FROM atlas_projects.work_progress"
        db.execute(
            "SELECT setval(pg_get_serial_sequence('atlas_projects.ledger_events','seq'),coalesce(max(seq),1),count(*)>0) FROM ("
            + sequence_rows
            + ") events"
        )
    return dict(
        status="database_restored",
        counts={key: len(value) for key, value in exported.items()},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("dry-run", "migrate", "resume", "restore", "restore-database"),
    )
    for name in (
        "legacy-db",
        "code-root",
        "code-state",
        "browser-import",
        "exports-root",
        "owners",
        "restore-to",
    ):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--recovery", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--writers-quiesced", action="store_true")
    args = parser.parse_args()
    dsn = os.environ["ATLAS_RETRIEVAL_DSN"]
    if args.command in ("dry-run", "migrate"):
        if not all((args.legacy_db, args.code_root, args.code_state)):
            parser.error("legacy-db, code-root and code-state are required")
        owners = json.loads(args.owners.read_bytes()) if args.owners else None
        if args.command == "dry-run":
            result = plan(
                dsn,
                args.legacy_db,
                args.code_root,
                args.code_state,
                args.browser_import,
                owners,
                args.exports_root,
            )
        else:
            prepare(
                dsn,
                args.legacy_db,
                args.code_root,
                args.code_state,
                args.recovery,
                args.data_root,
                browser=args.browser_import,
                owners=owners,
                exports=args.exports_root,
                quiesced=args.writers_quiesced,
            )
            result = migrate(
                dsn, args.recovery, args.data_root, quiesced=args.writers_quiesced
            )
    elif args.command == "resume":
        result = migrate(
            dsn, args.recovery, args.data_root, quiesced=args.writers_quiesced
        )
    elif args.command == "restore-database":
        if args.restore_to is None:
            parser.error("restore-to must name the verified offline bundle")
        result = restore_database(dsn, args.restore_to)
    else:
        if args.restore_to is None:
            parser.error("restore-to is required")
        result = restore(
            dsn,
            args.recovery,
            args.data_root,
            args.restore_to,
            quiesced=args.writers_quiesced,
        )
    # Never echo DSNs, source text, filenames or imported browser/audit content.
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("status", "counts", "completed")
                if key in result
            }
            | {"quarantined_records": len(result.get("quarantined", []))}
        )
    )


if __name__ == "__main__":
    main()
