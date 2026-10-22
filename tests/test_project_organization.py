"""Approved task-I atomic organization: real PostgreSQL transactions and HTTP fence."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import psycopg

from services.retrieval.project_files import ProjectFiles
from services.retrieval.project_scoped_api import store
from services.retrieval.project_work import Work

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_project_work as work_tests


class ProjectOrganizationTests(work_tests.ProjectWorkTests):
    def prepare_batch(self) -> dict[str, Any]:
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            source = files.folder(
                dict(name="Input", locality="server", model_use="allow")
            )
            destination = files.folder(
                dict(name="Output", locality="server", model_use="allow")
            )
            first = files.create_file(source["id"], "first.txt", b"first original", 0)
            second = files.create_file(
                source["id"],
                "second.txt",
                b"second original",
                files.get("folder", source["id"])["revision"],
            )
            session = Work(scope).create(
                dict(
                    conversation_id=self.conversation,
                    folder_ids=[source["id"], destination["id"]],
                    base_versions=[
                        dict(file_id=file["id"], version_id=file["version_id"])
                        for file in (first, second)
                    ],
                )
            )
        self.session = session
        acquired = self.acquire()
        self.assertEqual(acquired.status_code, 200, acquired.text)
        return dict(
            self.fence(),
            mode="apply",
            operations=[
                dict(
                    file_id=file["id"],
                    base_version=file["version_id"],
                    expected_revision=file["revision"],
                    folder_id=destination["id"],
                    path=file["relative_path"],
                )
                for file in (first, second)
            ],
        )

    def snapshot(self) -> dict[str, Any]:
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            return dict(
                files=files.listing("file"),
                folders=files.listing("folder"),
                versions=files.listing("version"),
                outbox=files.listing("outbox"),
                project=scope.project_data,
                ledger=scope.db.execute(
                    "SELECT seq,data FROM atlas_projects.ledger_events WHERE org_id=%s AND project_id=%s ORDER BY seq",
                    (self.org, self.project),
                ).fetchall(),
            )

    def test_organization_preview_apply_replay_and_immutable_originals(self) -> None:
        data = self.prepare_batch()
        before = self.snapshot()
        preview = self.broker("organize", dict(data, mode="preview"))
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["files"], [])
        self.assertEqual(before, self.snapshot())
        key = str(uuid4())
        applied = self.broker("organize", data, key=key)
        self.assertEqual(applied.status_code, 200, applied.text)
        self.validator.validate(
            applied.json(),
            self.contract["components"]["schemas"]["ExecutionOrganizeResult"],
        )
        after = self.snapshot()
        self.assertEqual(before["versions"], after["versions"])
        self.assertEqual(
            after["project"]["memory_revision"],
            before["project"]["memory_revision"] + 1,
        )
        for item, file in zip(data["operations"], applied.json()["files"]):
            self.assertEqual(file["revision"], item["expected_revision"] + 1)
            self.assertEqual(file["folder_id"], item["folder_id"])
            response = self.request(
                "GET",
                self.project_path() + "/files/" + file["id"] + "/content",
                "editor",
            )
            self.assertIn(response.content, (b"first original", b"second original"))
        after_reads = self.snapshot()
        replay = self.broker("organize", data, key=key)
        self.assertEqual(replay.json(), applied.json())
        self.assertEqual(after_reads, self.snapshot())
        self.assertEqual(len(after["outbox"]) - len(before["outbox"]), 2)
        self.assertEqual(
            self.broker("organize", dict(data, mode="preview"), key=key).status_code,
            409,
        )

    def test_organization_stale_preview_collision_duplicates_and_limits(self) -> None:
        data = self.prepare_batch()
        initial = self.snapshot()
        for field in ("expected_revision", "base_version"):
            invalid = copy.deepcopy(data)
            invalid["operations"][1][field] = (
                999 if field == "expected_revision" else str(uuid4())
            )
            response = self.broker("organize", invalid)
            self.assertIn(response.status_code, (404, 409), response.text)
            self.assertEqual(initial, self.snapshot())
        duplicate = dict(data, operations=[data["operations"][0]] * 2)
        self.error(self.broker("organize", duplicate), 422, "invalid_request")
        collision = copy.deepcopy(data)
        collision["operations"][1]["path"] = collision["operations"][0]["path"]
        self.error(self.broker("organize", collision), 422, "invalid_request")
        excessive = self.broker(
            "organize", dict(data, operations=[data["operations"][0]] * 101)
        )
        self.error(excessive, 413, "size_limit")
        self.assertEqual(
            excessive.json()["measurements"],
            [dict(measured=101, limit=100, unit="entries")],
        )
        self.assertEqual(
            self.broker("organize", dict(data, mode="preview")).status_code, 200
        )
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            folder = files.get("folder", data["operations"][0]["folder_id"])
            files.create_file(
                folder["id"], "second.txt", b"occupied", folder["revision"]
            )
        occupied = self.snapshot()
        self.error(self.broker("organize", data), 409, "path_conflict")
        self.assertEqual(occupied, self.snapshot())

    def test_organization_failure_after_first_move_rolls_back_every_store(self) -> None:
        data = self.prepare_batch()
        initial = self.snapshot()
        original = ProjectFiles.emit
        calls = 0

        def fail_second(
            files: ProjectFiles, kind: str, refs: list[str], source_bytes: int = 0
        ) -> None:
            nonlocal calls
            original(files, kind, refs, source_bytes)
            if kind == "file_moved":
                calls += 1
                if calls == 2:
                    raise OSError("injected storage failure")

        with patch.object(ProjectFiles, "emit", fail_second):
            self.error(self.broker("organize", data), 503, "dependency_unavailable")
        self.assertEqual(calls, 2)
        self.assertEqual(initial, self.snapshot())
        retry = self.broker("organize", data)
        self.assertEqual(retry.status_code, 200, retry.text)

    def test_organization_fence_scope_policy_swaps_and_traversal(self) -> None:
        data = self.prepare_batch()
        before = self.snapshot()
        self.error(
            self.broker("organize", dict(data, generation=2)), 409, "session_busy"
        )
        self.assertIn(
            self.broker("organize", data, role="viewer").status_code, (403, 404)
        )
        self.assertEqual(
            self.broker("organize", data, role="ungranted").status_code, 404
        )
        traversal = copy.deepcopy(data)
        traversal["operations"][0]["path"] = "../escape"
        self.error(self.broker("organize", traversal), 422, "invalid_request")
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            first = files.get("file", data["operations"][0]["file_id"])
        swaps = copy.deepcopy(data)
        for item, path in zip(swaps["operations"], ("second.txt", "first.txt")):
            item.update(folder_id=first["folder_id"], path=path)
        self.error(self.broker("organize", swaps), 409, "path_conflict")
        self.assertEqual(before, self.snapshot())
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.work_leases SET expires_at=now()-interval '1 second'"
            )
        self.error(self.broker("organize", data), 409, "session_busy")

    def test_organization_concurrent_batches_and_replay_reauthorization(self) -> None:
        data = self.prepare_batch()
        with ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(
                executor.map(lambda _: self.broker("organize", data), range(2))
            )
        self.assertEqual(
            sorted(response.status_code for response in replies), [200, 409]
        )
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            for item in data["operations"]:
                item["expected_revision"] = files.get("file", item["file_id"])[
                    "revision"
                ]
        key = str(uuid4())
        self.assertEqual(
            self.broker("organize", dict(data, mode="preview"), key=key).status_code,
            200,
        )
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.grants SET revision=revision+1 WHERE org_id=%s AND member_id=%s",
                (self.org, self.members["editor"]),
            )
        self.error(
            self.broker("organize", dict(data, mode="preview"), key=key),
            403,
            "policy_denied",
        )
