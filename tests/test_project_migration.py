"""REQ-PROJ-03: lossless migration/recovery with synthetic stores and real PostgreSQL."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from scripts import migrate_projects as migration
from services.retrieval.project_documents import ScopedProjectDocuments
from services.retrieval.project_files import ProjectFiles
from services.retrieval.project_identity import Identity, ProjectError
from services.retrieval.project_install import install
from services.retrieval.project_store import ProjectPool, ScopedProjects
from services.retrieval.projects import Projects

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_project_isolation as isolation

ROOT = Path(__file__).resolve().parents[1]


class ProjectMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Reuse only database lifecycle, not the unrelated isolation test methods.
        getattr(isolation.ProjectIsolationTests.setUpClass, "__func__")(cls)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "BRAIN")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.legacy = self.root / "projects.sqlite"
        self.code = self.root / "workspaces"
        self.state = self.root / "state"
        self.recovery = self.root / "recovery"
        self.data = self.root / "p0"
        self.browser = self.root / "browser.json"
        self.code.mkdir()
        self.state.mkdir()
        self.policy = json.loads(
            (ROOT / "contracts/examples/project-mode/Policy.valid.json").read_text()
        )
        for key in ("stored_bytes", "versioned_bytes", "derived_bytes", "upload_bytes"):
            self.policy["quotas"][key] = 16777216
        self.policy["quotas"]["active_projects"] = 10
        with psycopg.connect(self.admin_dsn) as db:
            db.execute("DROP SCHEMA IF EXISTS atlas_projects CASCADE")
        self.org, self.admin = install(
            self.admin_dsn, "Installed organization", "installer", self.policy
        )
        old = Projects(self.legacy)
        self.instructions = "Exact instructions.\n\nPreserve spaces:  two.\n"
        self.project = old.create("Existing documents", self.instructions)
        self.conversation = old.conversation(self.project, "Ordered conversation")
        old.turn(
            self.project,
            self.conversation,
            "First question",
            "First answer",
            ["Remember this fact"],
            0,
        )
        old.turn(
            self.project, self.conversation, "Second question", "Second answer", [], 0
        )
        old.erase(self.project, None)
        old.turn(
            self.project, self.conversation, "Stale question", "Stale answer", [], 0
        )
        old.turn(
            self.project,
            self.conversation,
            "Current question",
            "Current answer",
            ["Current fact"],
            1,
        )
        self.document = str(uuid4())
        self.chunks = [
            dict(
                chunk_id="b" * 64,
                doc_id=self.document,
                source="old.pdf",
                langue="en",
                source_sha256="a" * 64,
                embedding_revision="legacy-cpu",
                position=0,
                text="Retained  extraction.\nExact text.",
                embedding=[0.25, -0.5],
            )
        ]
        self.chunk_json = json.dumps(self.chunks, indent=2)
        with old.connection() as db:
            db.execute(
                "CREATE TABLE project_documents(id TEXT PRIMARY KEY,project TEXT,name TEXT,chunks TEXT)"
            )
            db.execute(
                "INSERT INTO project_documents VALUES(?,?,?,?)",
                (self.document, self.project, "old.pdf", self.chunk_json),
            )
        self.session = str(uuid4())
        workspace = self.code / UUID(self.session).hex
        workspace.mkdir()
        (workspace / "source.txt").write_bytes(b"Original code\x00\xff")
        self.git(workspace, "init", "-q")
        self.git(workspace, "add", "source.txt")
        self.git(
            workspace,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "Imported baseline",
        )
        self.baseline = self.git(workspace, "rev-parse", "HEAD").strip()
        (workspace / "exports").mkdir()
        (workspace / "exports" / "result.zip").write_bytes(b"existing export")
        (workspace / "source.txt").write_bytes(b"Uncommitted code\x00\xfe")
        metadata = dict(
            baseline=self.baseline, plan=[dict(text="Preserved plan", status="pending")]
        )
        (self.state / (UUID(self.session).hex + ".json")).write_text(
            json.dumps(metadata)
        )
        (self.state / (UUID(self.session).hex + ".jsonl")).write_text(
            json.dumps(dict(seq=1, type="command", output="Preserved audit")) + "\n"
        )
        self.browser_state = [
            dict(
                id=self.session,
                messages=[
                    dict(
                        user="Browser question",
                        answer="Browser answer",
                        events=[dict(type="plan")],
                    )
                ],
                queue=["Queued request"],
            )
        ]
        self.browser.write_text(json.dumps(self.browser_state))

    admin_dsn: str
    app_dsn: str
    validator: Any
    contract: dict[str, Any]

    @staticmethod
    def git(directory: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(directory), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def prepare(self, **kwargs: Any) -> dict[str, Any]:
        return migration.prepare(
            self.admin_dsn,
            self.legacy,
            self.code,
            self.state,
            self.recovery,
            self.data,
            browser=self.browser,
            quiesced=True,
            **kwargs,
        )

    def migrate(self) -> dict[str, Any]:
        return migration.migrate(
            self.admin_dsn, self.recovery, self.data, quiesced=True
        )

    def rows(self, kind: str, project: str | None = None) -> list[dict[str, Any]]:
        with psycopg.connect(self.admin_dsn, row_factory=dict_row) as db:
            return [
                row["data"]
                for row in db.execute(
                    "SELECT data FROM atlas_projects.legacy_rows WHERE kind=%s AND (%s::uuid IS NULL OR project_id=%s) ORDER BY ordinal",
                    (kind, project, project),
                )
            ]

    def test_counts_bytes_chunks_baseline_browser_and_scoped_citations(self) -> None:
        original = migration.inventory(self.root)
        preview = migration.plan(
            self.admin_dsn, self.legacy, self.code, self.state, self.browser
        )
        self.assertEqual(migration.inventory(self.root), original)
        self.assertFalse(self.recovery.exists())
        self.assertFalse(self.data.exists())
        self.assertEqual(
            preview["counts"],
            dict(
                projects=1,
                conversations=1,
                turns=4,
                facts=1,
                project_documents=1,
                chunks=1,
                code_sessions=1,
                browser_chats=1,
            ),
        )
        self.prepare()
        result = self.migrate()
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["restore_rehearsed"])
        self.assertEqual(result["org_id"], self.org)
        self.assertEqual(self.rows("project_documents")[0]["chunks"], self.chunk_json)
        self.assertEqual(
            [r["content_refs"][0]["text"] for r in self.rows("turn", self.project)],
            ["First question", "Second question", "Stale question", "Current question"],
        )
        self.assertEqual(
            [r["context_revision"] for r in self.rows("turn", self.project)],
            [0, 0, -1, 1],
        )
        imported = self.rows("client_import")[0]
        self.assertEqual(imported["state"], self.browser_state[0])
        self.assertEqual(imported["provenance"], "client_import")
        for kind, schema in (
            ("conversation", "Conversation"),
            ("turn", "Turn"),
            ("work_session", "WorkSession"),
        ):
            for value in self.rows(kind):
                self.validator.validate(
                    value, self.contract["components"]["schemas"][schema]
                )
        code_project = next(
            item["project_id"] for item in result["projects"] if item["kind"] == "code"
        )
        workspace = (
            self.data
            / "orgs"
            / self.org
            / "projects"
            / code_project
            / "work"
            / self.session
        )
        self.assertEqual(
            migration.inventory(workspace),
            migration.inventory(self.code / UUID(self.session).hex),
        )
        self.assertEqual(
            self.git(workspace, "rev-parse", "HEAD").strip(), self.baseline
        )
        self.assertEqual(self.rows("code_provenance")[0]["baseline"], self.baseline)
        self.assertFalse(self.rows("code_provenance")[0]["forge_capability"])
        pool = ProjectPool(self.app_dsn)
        self.addCleanup(pool.close)
        identity = Identity(
            subject="installer",
            org=self.org,
            member=self.admin,
            project=self.project,
            membership_revision=0,
            policy_revision=0,
            expires=0,
        )
        with patch.dict(os.environ, {"ATLAS_DATA_ROOT": str(self.data)}):
            with ScopedProjects(pool).scope(identity, self.org, self.project) as scope:
                files = ProjectFiles(scope)
                chunk = ScopedProjectDocuments(files).resolve("b" * 64)
                self.assertEqual(chunk["text"], self.chunks[0]["text"])
                self.assertEqual(chunk["file_id"], self.document)
                with self.assertRaises(ProjectError) as unavailable:
                    files.content(self.document)
                self.assertEqual(unavailable.exception.status, 404)
                file = files.get("file", self.document)
                self.assertIsNone(file["sha256"])
                self.assertEqual(file["availability"], "not_retained")
                self.assertFalse(
                    files.blob_path(self.document, file["version_id"]).exists()
                )
                self.assertEqual(
                    files.content(scope.project_data["instructions_file_id"])[1],
                    self.instructions.encode(),
                )
                memory = files.listing("memory")
                self.assertEqual(memory[0]["kind"], "legacy")
                self.assertEqual(memory[0]["text"], "Current fact")
                for value in memory:
                    self.validator.validate(
                        value, self.contract["components"]["schemas"]["MemoryEntry"]
                    )
                record = files.db.execute(
                    "SELECT private FROM atlas_projects.resources WHERE kind='derived' AND project_id=%s",
                    (self.project,),
                ).fetchone()
                self.assertIsNotNone(record)
                assert record is not None
                self.assertEqual(
                    record["private"]["legacy_chunks_json"], self.chunk_json
                )
                self.assertEqual(record["private"]["legacy_chunks"], self.chunks)
        for path, evidence in original.items():
            self.assertEqual(migration.inventory(self.root)[path], evidence)
        self.assertEqual(len(self.rows("migration_aliases")), 2)
        receipt_count = len(self.rows("migration_receipt"))
        self.migrate()
        self.assertEqual(len(self.rows("migration_receipt")), receipt_count)
        self.assertEqual(len(self.rows("client_import")), 1)

    def test_interrupted_commit_resumes_and_restore_keeps_p0_writes(self) -> None:
        self.prepare()
        real_write = migration.write_json

        def interrupted(path: Path, value: Any) -> None:
            if path.name == "manifest.json" and value["completed"]:
                raise OSError("Injected interruption after committed project")
            real_write(path, value)

        with patch.object(migration, "write_json", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "Injected interruption"):
                self.migrate()
        self.assertEqual(len(self.rows("migration_receipt")), 1)
        result = self.migrate()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(len(self.rows("migration_receipt")), 2)
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.projects SET revision=revision+1,instructions=%s WHERE id=%s",
                ("Later P0 write", self.project),
            )
        (self.data / "later-write.txt").write_bytes(b"Keep subsequent content")
        self.migrate()
        restored = self.root / "restored"
        evidence = migration.restore(
            self.admin_dsn, self.recovery, self.data, restored, quiesced=True
        )
        self.assertTrue(evidence["preserves_p0_writes"])
        self.assertEqual(
            migration.legacy_rows(restored / "legacy" / "projects.sqlite"),
            migration.legacy_rows(self.legacy),
        )
        self.assertEqual(
            migration.inventory(restored / "p0-data"), migration.inventory(self.data)
        )
        database = json.loads((restored / "p0-database.json").read_bytes())
        project = next(row for row in database["projects"] if row["id"] == self.project)
        self.assertEqual(project["instructions"], "Later P0 write")
        self.assertEqual(project["revision"], 1)
        self.assertEqual(len(database["ledger_events"]), len(database["outbox"]))
        with self.assertRaisesRegex(ValueError, "empty project database"):
            migration.restore_database(self.admin_dsn, restored)
        # Simulate total database loss only in this isolated fixture.
        with psycopg.connect(self.admin_dsn) as db:
            db.execute("DROP SCHEMA atlas_projects CASCADE")
        recovered = migration.restore_database(self.admin_dsn, restored)
        self.assertEqual(recovered["status"], "database_restored")
        with psycopg.connect(self.admin_dsn) as db:
            value = db.execute(
                "SELECT instructions,revision FROM atlas_projects.projects WHERE id=%s",
                (self.project,),
            ).fetchone()
        self.assertEqual(value, ("Later P0 write", 1))
        with self.assertRaises(FileExistsError):
            migration.restore(
                self.admin_dsn, self.recovery, self.data, restored, quiesced=True
            )

    def test_transaction_failure_and_quarantine_preserve_sources(self) -> None:
        self.prepare()
        with patch.object(
            migration,
            "import_documents",
            side_effect=OSError("Injected transaction failure"),
        ):
            with self.assertRaises(OSError):
                self.migrate()
        self.assertEqual(self.rows("migration_receipt"), [])
        self.assertEqual(self.migrate()["status"], "verified")
        shutil.rmtree(self.recovery)
        (self.state / (UUID(self.session).hex + ".json")).unlink()
        preview = migration.plan(
            self.admin_dsn,
            self.legacy,
            self.code,
            self.state,
            owners={self.project: str(uuid4())},
        )
        self.assertEqual(
            {item["reason"] for item in preview["quarantined"]},
            {"missing_code_state", "unknown_ownership"},
        )
        self.assertEqual(preview["browser_history"], "unavailable")
        self.assertEqual(preview["projects"], [])

    def test_snapshot_tampering_and_missing_quiescence_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Quiesce"):
            migration.prepare(
                self.admin_dsn,
                self.legacy,
                self.code,
                self.state,
                self.recovery,
                self.data,
            )
        self.prepare()
        (
            self.recovery / "snapshot" / "state" / (UUID(self.session).hex + ".jsonl")
        ).write_text("tampered")
        with self.assertRaisesRegex(ValueError, "snapshot"):
            self.migrate()
        self.assertEqual(self.rows("migration_receipt"), [])

    def test_installation_import_is_completed_without_duplicate_projects(self) -> None:
        with psycopg.connect(self.admin_dsn) as db:
            db.execute("DROP SCHEMA atlas_projects CASCADE")
        self.org, self.admin = install(
            self.admin_dsn, "Operator selected", "installer", self.policy, self.legacy
        )
        self.prepare()
        self.assertEqual(self.migrate()["status"], "verified")
        self.assertEqual(len(self.rows("migration_receipt")), 2)
        with psycopg.connect(self.admin_dsn) as db:
            count = db.execute(
                "SELECT count(*) FROM atlas_projects.projects"
            ).fetchone()
        self.assertEqual(count, (2,))

    def test_export_aliases_and_unmapped_export_preservation(self) -> None:
        exports = self.root / "terminal-exports"
        request = "chatcmpl-" + "c" * 32
        (exports / request).mkdir(parents=True)
        content = b"Existing terminal export\x00\xff"
        (exports / request / "workspace.tar.gz").write_bytes(content)
        (exports / request / "unmapped.bin").write_bytes(b"Restricted unmapped export")
        url = migration.DOWNLOAD_PREFIX + request + "/workspace.tar.gz"
        audit = self.state / (UUID(self.session).hex + ".jsonl")
        with audit.open("a") as handle:
            handle.write(
                json.dumps(
                    dict(type="export", turn_id=request, output=dict(files=[url]))
                )
                + "\n"
            )
        self.assertEqual(
            migration.plan(self.admin_dsn, self.legacy, self.code, self.state)[
                "quarantined"
            ][0]["reason"],
            "missing_export",
        )
        self.prepare(exports=exports)
        manifest = self.migrate()
        self.assertEqual(manifest["unmapped_exports"], [request + "/unmapped.bin"])
        alias = self.rows("download_alias")[0]
        self.assertEqual(alias["sha256"], migration.digest(content))
        self.assertTrue(alias["requires_scope"])
        self.assertEqual(
            next(self.data.rglob(alias["version_id"])).read_bytes(), content
        )
        self.assertEqual(
            migration.inventory(self.recovery / "snapshot" / "exports"),
            migration.inventory(exports),
        )

    def test_interrupted_filesystem_publication_preserves_abandoned_copy(self) -> None:
        self.prepare()
        real_publish = migration.publish_project

        def interrupted(*args: Any) -> Any:
            real_publish(*args)
            raise OSError("Injected crash after filesystem publication")

        with patch.object(migration, "publish_project", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "Injected crash"):
                self.migrate()
        self.assertEqual(self.rows("migration_receipt"), [])
        self.assertEqual(self.migrate()["status"], "verified")
        self.assertTrue(list((self.recovery / "abandoned").iterdir()))

    def test_symlink_escape_quarantined_without_reading_target(self) -> None:
        workspace = self.code / UUID(self.session).hex
        (workspace / "outside").symlink_to("../../absent-sensitive-file")
        manifest = self.prepare()
        self.assertEqual(manifest["quarantined"][0]["reason"], "invalid_code_state")
        self.assertEqual(self.migrate()["status"], "quarantined")
        self.assertEqual(self.rows("migration_aliases"), [])
        self.assertTrue(
            (
                self.recovery / "snapshot" / "code" / UUID(self.session).hex / "outside"
            ).is_symlink()
        )
