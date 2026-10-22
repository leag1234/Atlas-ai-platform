"""Task C acceptance over HTTP and real PostgreSQL, without inference calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from starlette.requests import Request

from services.retrieval.project_documents import ScopedProjectDocuments
from services.retrieval.project_files import (
    ProjectFiles,
    action_digest,
    after,
    timestamp,
)
from services.retrieval.project_identity import ProjectError, sign
from services.retrieval.project_scoped_api import projects, store
from services.retrieval.project_store import Scope

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_project_isolation import ProjectIsolationTests, ROOT


class ProjectFilesTests(ProjectIsolationTests):
    def test_legacy_extract_without_original_retains_scoped_citations(self) -> None:
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            folder = files.folder(
                dict(name="Legacy", locality="server", model_use="allow")
            )
            file, version = files.new(), files.new()
            metadata = dict(
                mime="application/pdf",
                size_bytes=0,
                sha256=None,
                origin="legacy_extracted",
                availability="not_retained",
                source_locator=None,
                index_status="ready",
            )
            file.update(
                metadata,
                folder_id=folder["id"],
                relative_path="old.pdf",
                version_id=version["id"],
                classification="internal",
                locality="server",
            )
            version.update(metadata, file_id=file["id"], parent_version_id=None)
            files.put("file", file, folder["id"])
            files.put("version", version, file["id"])
            chunk = dict(
                file_id=file["id"],
                version_id=version["id"],
                chunk_id="legacy-citation",
                text="Preserved exact extract.",
                locator=dict(kind="paragraph", paragraph=0),
            )
            derived = dict(files.new(), chunks=[chunk])
            files.put("derived", derived, version["id"], retention="E")
            documents = ScopedProjectDocuments(files)
            self.assertEqual(documents.sources(), [chunk])
            self.assertEqual(documents.resolve("legacy-citation"), chunk)
            with self.assertRaises(ProjectError) as denied:
                files.content(file["id"])
            self.assertEqual(denied.exception.status, 404)
            self.assertFalse(files.blob_path(file["id"], version["id"]).exists())
            folder["model_use"] = "deny"
            files.save(folder)
            self.assertEqual(documents.sources(), [])
            folder.update(model_use="allow", locality="local_only")
            files.save(folder)
            self.assertEqual(documents.sources(), [])
            folder.update(locality="server", deleted_at=timestamp())
            files.save(folder)
            self.assertEqual(documents.sources(), [])
        with store(self.app_dsn).scope(
            self.identity("admin"), self.org, self.second_project
        ) as scope:
            self.assertEqual(ScopedProjectDocuments(ProjectFiles(scope)).sources(), [])

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=ROOT / "BRAIN")
        self.addCleanup(directory.cleanup)
        self.blobs = Path(directory.name)
        environment = patch.dict(os.environ, {"ATLAS_DATA_ROOT": directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        super().setUp()
        bootstrap = self.request(
            "GET", self.project_path() + "/ledger", "viewer"
        ).json()["items"]
        self.ledger_start = max(event["seq"] for event in bootstrap)
        for key in ("stored_bytes", "versioned_bytes", "derived_bytes", "upload_bytes"):
            self.policy["quotas"][key] = 16777216
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s",
                (Jsonb(self.policy),),
            )
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s",
                (Jsonb(self.policy),),
            )

    def ledger_path(self) -> str:
        return self.project_path() + "/ledger?after=" + str(self.ledger_start)

    def headers(
        self, role: str = "editor", revision: int | None = None, key: str | None = None
    ) -> dict[str, str]:
        headers = {
            "X-Atlas-Project-Identity": sign(self.identity(role), self.key.encode()),
            "Idempotency-Key": key or str(uuid4()),
        }
        if revision is not None:
            headers["If-Match"] = f'"{revision}"'
        return headers

    def folder(
        self, locality: str = "server", name: str = "Inputs", model_use: str = "allow"
    ) -> dict[str, Any]:
        response = self.request(
            "POST",
            self.project_path() + "/folders",
            "editor",
            dict(name=name, locality=locality, model_use=model_use),
        )
        self.assertEqual(response.status_code, 201, response.text)
        result: dict[str, Any] = response.json()
        self.validator.validate(
            result, self.contract["components"]["schemas"]["Folder"]
        )
        return result

    def upload(
        self,
        folder: dict[str, Any],
        content: bytes = b"Original bytes\x00\xff",
        name: str = "notes.bin",
        revision: int = 0,
        role: str = "editor",
    ) -> Any:
        # Deliberately send file bytes before all metadata parts.
        return self.client.post(
            self.project_path() + "/files?folder_id=" + folder["id"],
            headers=self.headers(role),
            files=[
                ("file", (name, content, "text/html")),
                ("path", (None, name)),
                ("expected_parent_revision", (None, str(revision))),
            ],
        )

    def uploaded(
        self,
        folder: dict[str, Any],
        content: bytes = b"Original bytes\x00\xff",
        name: str = "notes.bin",
    ) -> dict[str, Any]:
        response = self.upload(folder, content, name)
        self.assertEqual(response.status_code, 201, response.text)
        self.validator.validate(
            response.json(), self.contract["components"]["schemas"]["FileUploaded"]
        )
        result: dict[str, Any] = response.json()["file"]
        return result

    def approved(
        self,
        kind: str,
        effects: dict[str, Any],
        *,
        expires: str | None = None,
        role: str = "editor",
    ) -> str:
        with store(self.app_dsn).scope(
            self.identity(role), self.org, self.project
        ) as scope:
            digest = action_digest(scope, kind, effects)
        data = json.loads(
            (ROOT / "contracts/examples/project-mode/Approval.valid.json").read_text()
        )
        identifier = str(uuid4())
        data.update(
            id=identifier,
            org_id=self.org,
            project_id=self.project,
            approver_member_id=self.members[role],
            requested_by_member_id=self.members[role],
            decision="allowed",
            action_kind=kind,
            action_digest=digest,
            expires_at=expires or after(timestamp(), 120 / 86400),
            maximum_bytes=effects.get("size_bytes", 0),
            paths=[effects["path"]] if "path" in effects else [],
            version_refs=[
                dict(file_id=effects["file_id"], version_id=effects["base_version"])
            ]
            if "base_version" in effects
            else [],
        )
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "INSERT INTO atlas_projects.file_approvals(org_id,project_id,id,data) VALUES(%s,%s,%s,%s)",
                (self.org, self.project, identifier, Jsonb(data)),
            )
        return identifier

    def test_bytes_version_collisions_and_approval_replay(self) -> None:
        folder = self.folder()
        original = bytes(range(256)) * 1000
        file = self.uploaded(folder, original, "measurements.bin")
        path = self.project_path() + "/files/" + file["id"]
        response = self.request("GET", path + "/content", "viewer")
        self.assertEqual(response.content, original)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertNotEqual(response.headers["content-type"], "text/html")
        self.assertEqual(file["sha256"], hashlib.sha256(original).hexdigest())
        self.error(
            self.upload(folder, name="measurements.bin"), 409, "revision_conflict"
        )
        self.error(
            self.upload(folder, name="measurements.bin", revision=1),
            409,
            "path_conflict",
        )
        replacement = b"Revised bytes\x00\xfe"
        approval = self.approved(
            "overwrite", ProjectFiles.overwrite_effects(file, replacement)
        )
        fields = {"base_version": file["version_id"], "approval_id": approval}
        updated = self.client.post(
            path + "/versions",
            headers=self.headers(revision=0),
            data=fields,
            files={"file": ("m.bin", replacement)},
        )
        self.assertEqual(updated.status_code, 201, updated.text)
        self.validator.validate(
            updated.json(), self.contract["components"]["schemas"]["FileVersion"]
        )
        self.assertEqual(
            self.request("GET", path + "/content", "viewer").content, replacement
        )
        self.assertEqual(
            self.request(
                "GET", path + "/content?version=" + file["version_id"], "viewer"
            ).content,
            original,
        )
        self.assertEqual(
            len(self.request("GET", path + "/versions", "viewer").json()["items"]), 2
        )
        self.error(
            self.client.post(
                path + "/versions",
                headers=self.headers(revision=0),
                data=fields,
                files={"file": ("m.bin", replacement)},
            ),
            409,
            "revision_conflict",
        )
        fields["base_version"] = updated.json()["id"]
        self.error(
            self.client.post(
                path + "/versions",
                headers=self.headers(revision=1),
                data=fields,
                files={"file": ("m.bin", replacement)},
            ),
            410,
            "approval_expired_or_replayed",
        )
        for path_on_disk in self.blobs.rglob(file["version_id"]):
            self.assertEqual(path_on_disk.read_bytes(), original)
            self.assertIn(self.org, path_on_disk.parts)
            self.assertIn(self.project, path_on_disk.parts)

    def test_project_creation_records_initial_folders_and_instructions(self) -> None:
        response = self.request("GET", self.project_path() + "/ledger", "viewer")
        self.assertEqual(response.status_code, 200, response.text)
        events = response.json()["items"]
        self.assertEqual(
            [event["kind"] for event in events],
            ["folder_registered", "folder_registered", "file_uploaded"],
        )
        project = self.request("GET", self.project_path(), "viewer").json()
        self.assertEqual(
            events[-1]["resource_refs"][0]["file_id"], project["instructions_file_id"]
        )
        self.assertEqual(events[-1]["byte_counts"]["source_bytes"], 0)
        self.assertIsNone(events[0]["previous_digest"])
        for previous, event in zip(events, events[1:]):
            self.assertEqual(event["previous_digest"], previous["digest"])

    def test_file_lifecycle_ledger_is_exact_paginated_and_scoped(self) -> None:
        folder = self.folder(name="Ledger")
        original, replacement = b"alpha", b"bravo!"
        file = self.uploaded(folder, original, "ledger.bin")
        path = self.project_path() + "/files/" + file["id"]
        self.assertEqual(
            self.request("GET", path + "/content", "viewer").content, original
        )
        approval = self.approved(
            "overwrite", ProjectFiles.overwrite_effects(file, replacement)
        )
        updated = self.client.post(
            path + "/versions",
            headers=self.headers(revision=0, key="ledger-version"),
            data={"base_version": file["version_id"], "approval_id": approval},
            files={"file": ("ledger.bin", replacement)},
        )
        self.assertEqual(updated.status_code, 201, updated.text)
        current = {**file, "version_id": updated.json()["id"], "revision": 1}
        replay = self.client.send(updated.request)
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json(), updated.json())
        totals = self.request("GET", self.project_path() + "/ledger/summary", "viewer")
        self.assertEqual(totals.status_code, 200, totals.text)
        self.assertEqual(
            totals.json()["server"],
            {"files": 3, "bytes": len(original) + len(replacement)},
        )
        deletion = self.approved(
            "delete",
            dict(
                file_id=file["id"],
                base_version=current["version_id"],
                revision=current["revision"],
            ),
        )
        deleted = self.client.delete(
            path + "?approval_id=" + deletion,
            headers=self.headers(revision=1, key="ledger-delete"),
        )
        self.assertEqual(deleted.status_code, 202, deleted.text)
        replay = self.client.send(deleted.request)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json(), deleted.json())
        ledger = self.request("GET", self.ledger_path() + "&limit=3", "viewer")
        self.assertEqual(ledger.status_code, 200, ledger.text)
        page = ledger.json()
        self.assertIsNotNone(page["next_cursor"])
        rest = self.request(
            "GET",
            self.ledger_path() + "&limit=3&cursor=" + page["next_cursor"],
            "viewer",
        ).json()
        final = self.request(
            "GET",
            self.ledger_path() + "&limit=3&cursor=" + rest["next_cursor"],
            "viewer",
        ).json()
        events = page["items"] + rest["items"] + final["items"]
        self.assertIsNone(final["next_cursor"])
        for event in events:
            self.validator.validate(
                event, self.contract["components"]["schemas"]["LedgerEvent"]
            )
        self.assertEqual(len({event["event_id"] for event in events}), 7)
        self.assertEqual(events[3]["actor"]["member_id"], self.members["viewer"])
        self.assertEqual(
            events[3]["resource_refs"][0]["version_id"], file["version_id"]
        )
        self.assertEqual(
            [event["kind"] for event in events],
            [
                "folder_registered",
                "file_uploaded",
                "extraction_completed",
                "file_read",
                "file_versioned",
                "extraction_completed",
                "file_deleted",
            ],
        )
        self.assertEqual(
            [event["byte_counts"]["source_bytes"] for event in events],
            [0, len(original), 0, len(original), len(replacement), 0, 0],
        )
        summary = self.request("GET", self.project_path() + "/ledger/summary", "viewer")
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertEqual(summary.json()["server"], {"files": 1, "bytes": 0})
        self.assertEqual(summary.json()["as_of_seq"], events[-1]["seq"])
        self.validator.validate(
            summary.json(), self.contract["components"]["schemas"]["LedgerSummary"]
        )
        denied = self.request(
            "GET",
            self.prefix(self.other_org) + "/" + self.foreign_project + "/ledger",
            "viewer",
        )
        self.error(denied, 404, "not_found")
        self.error(
            self.request(
                "GET",
                self.prefix(self.other_org)
                + "/"
                + self.foreign_project
                + "/ledger/summary",
                "viewer",
            ),
            404,
            "not_found",
        )
        after = self.request(
            "GET",
            self.project_path() + "/ledger?after=" + str(events[-2]["seq"]),
            "viewer",
        )
        self.assertEqual(after.json()["items"], events[-1:])
        self.error(
            self.request("GET", self.project_path() + "/ledger?after=-1", "viewer"),
            422,
            "invalid_request",
        )
        for suffix in ("/ledger", "/ledger/summary"):
            self.error(
                self.request("GET", self.project_path() + suffix, "ungranted"),
                404,
                "not_found",
            )
        empty = self.request(
            "GET",
            self.project_path() + "/ledger/summary?session_id=" + str(uuid4()),
            "viewer",
        ).json()
        self.assertEqual(empty["server"], summary.json()["server"])
        self.assertEqual(empty["as_of_seq"], summary.json()["as_of_seq"])

    def test_folder_registration_update_and_denied_promotion_ledger(self) -> None:
        folder = self.folder("local_only")
        path = self.project_path() + "/folders/" + folder["id"]
        renamed = self.request("PATCH", path, "editor", dict(name="Archive"), match=0)
        self.assertEqual(renamed.status_code, 200, renamed.text)
        denied = self.request(
            "PATCH",
            path,
            "editor",
            dict(locality="server", approval_id=str(uuid4())),
            match=1,
        )
        self.error(denied, 409, "locality_transition_requires_plan")
        events = self.request("GET", self.ledger_path(), "viewer").json()["items"]
        self.assertEqual(
            [event["kind"] for event in events],
            ["folder_registered", "folder_registered"],
        )
        self.assertEqual(
            [event["byte_counts"]["source_bytes"] for event in events], [0, 0]
        )

    def test_deliverable_boundary_download_range_and_summary(self) -> None:
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            folder = files.get("folder", scope.project_data["deliverables_folder_id"])
            file = files.create_file(folder["id"], "result.txt", b"published", 0)
            deliverable = json.loads(
                (
                    ROOT / "contracts/examples/project-mode/Deliverable.valid.json"
                ).read_text()
            )
            deliverable.update(
                files.new(),
                file_version_id=file["version_id"],
                mime=file["mime"],
                size_bytes=file["size_bytes"],
                sha256=file["sha256"],
            )
            deliverable["validation"]["artifact_sha256"] = file["sha256"]
            path = (
                self.project_path() + "/deliverables/" + deliverable["id"] + "/download"
            )
            deliverable["download_route"] = path
            files.put("deliverable", deliverable)
            duplicate = {**deliverable, **files.new()}
            files.put("deliverable", duplicate)
        response = self.client.get(
            path, headers={**self.headers("viewer"), "Range": "bytes=1-3"}
        )
        self.assertEqual(response.status_code, 206, response.text)
        self.assertEqual(response.content, b"ubl")
        events = self.request("GET", self.ledger_path(), "viewer").json()["items"]
        self.assertEqual(
            [event["kind"] for event in events],
            [
                "file_uploaded",
                "deliverable_created",
                "deliverable_created",
                "deliverable_downloaded",
            ],
        )
        self.assertEqual(
            [event["byte_counts"]["source_bytes"] for event in events], [9, 9, 9, 3]
        )
        self.assertEqual(
            events[-1]["resource_refs"],
            [dict(file_id=file["id"], version_id=file["version_id"], locator=None)],
        )
        summary = self.request(
            "GET", self.project_path() + "/ledger/summary", "viewer"
        ).json()
        self.assertEqual(summary["server"], {"files": 2, "bytes": 9})
        self.assertEqual(summary["produced"], {"files": 1, "bytes": 9})
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.resources SET expires_at=now()-interval '1 second' WHERE id=%s",
                (file["version_id"],),
            )
        summary = self.request("GET", self.project_path() + "/ledger/summary", "viewer")
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertEqual(summary.json()["produced"], {"files": 0, "bytes": 0})
        self.error(self.request("GET", path, "viewer"), 404, "not_found")

    def test_memory_and_instructions_boundaries_have_utf8_counts(self) -> None:
        instructions = self.request(
            "GET", self.project_path() + "/instructions", "viewer"
        ).json()
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            file = ProjectFiles(scope).get("file", instructions["file_id"])
        text = "Use precise units: µm."
        approval = self.approved(
            "overwrite", ProjectFiles.overwrite_effects(file, text.encode())
        )
        response = self.request(
            "PUT",
            self.project_path() + "/instructions",
            "editor",
            dict(text=text, base_version=instructions["version"], approval_id=approval),
            match=0,
        )
        self.assertEqual(response.status_code, 200, response.text)
        memory = self.request(
            "POST",
            self.project_path() + "/memory",
            "editor",
            dict(text="Résumé", source_refs=[]),
        ).json()
        path = self.project_path() + "/memory/" + memory["id"]
        active = self.request("PATCH", path, "editor", dict(status="active"), match=0)
        self.assertEqual(active.status_code, 200, active.text)
        approval = self.approved(
            "delete", dict(memory_ids=[memory["id"]], memory_revision=2)
        )
        deleted = self.request(
            "DELETE", path + "?approval_id=" + approval, "editor", match=1
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        events = self.request("GET", self.ledger_path(), "viewer").json()["items"]
        self.assertEqual(
            [event["kind"] for event in events],
            [
                "file_read",
                "file_versioned",
                "file_read",
                "context_selected",
                "context_selected",
                "context_selected",
            ],
        )
        self.assertEqual(
            [event["byte_counts"]["source_bytes"] for event in events],
            [0, len(text.encode()), len(text.encode()), 8, 8, 8],
        )
        self.assertNotIn("Résumé", json.dumps(events, ensure_ascii=False))

    def test_local_only_and_inaccessible_destination_never_read_body(self) -> None:
        folder = self.folder("local_only")
        self.assertEqual(folder["status"], "device_unavailable")
        before = list(self.blobs.rglob("*"))
        cases = [
            (self.project_path() + "/files", "folder_id=" + folder["id"], 409),
            (self.project_path() + "/files", "", 422),
            (
                self.project_path(self.second_project) + "/files",
                "folder_id=" + folder["id"],
                404,
            ),
        ]
        for path, query, expected in cases:

            async def receive() -> dict[str, Any]:
                self.fail("Upload body was consumed before destination rejection")

            request = Request(
                dict(
                    type="http",
                    method="POST",
                    path=path,
                    query_string=query.encode(),
                    headers=[
                        (k.lower().encode(), v.encode())
                        for k, v in self.headers().items()
                    ],
                    server=("testserver", 80),
                    scheme="http",
                ),
                receive=receive,
            )
            response = asyncio.run(projects(request, path.removeprefix("/api/atlas/")))
            self.assertEqual(response.status_code, expected)
        self.assertEqual(list(self.blobs.rglob("*")), before)
        self.error(self.upload(folder), 409, "local_only_upload_forbidden")
        self.error(
            self.request(
                "PATCH",
                self.project_path() + "/folders/" + folder["id"],
                "editor",
                {"locality": "server", "approval_id": str(uuid4())},
                match=0,
            ),
            409,
            "locality_transition_requires_plan",
        )

    def test_folder_move_cycles_collisions_and_scope(self) -> None:
        folder = self.folder()
        file = self.uploaded(folder)
        target = self.folder(name="Archive")
        path = self.project_path() + "/files/" + file["id"]
        changed = self.request(
            "PATCH",
            path,
            "editor",
            dict(
                folder_id=target["id"],
                path="saved.bin",
                base_version=file["version_id"],
            ),
            match=0,
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["relative_path"], "saved.bin")
        local = self.folder("local_only", "Local")
        self.error(
            self.request(
                "PATCH",
                path,
                "editor",
                dict(folder_id=local["id"], base_version=file["version_id"]),
                match=1,
            ),
            409,
            "locality_transition_requires_plan",
        )
        self.error(
            self.request(
                "PATCH",
                self.project_path() + "/folders/" + folder["id"],
                "editor",
                dict(parent_id=folder["id"]),
                match=2,
            ),
            422,
            "invalid_request",
        )
        self.error(
            self.request(
                "GET",
                self.project_path(self.second_project)
                + "/files/"
                + file["id"]
                + "/content",
                "admin",
            ),
            404,
            "not_found",
        )
        self.error(self.upload(folder, role="viewer"), 403, "permission_denied")
        self.error(
            self.upload(folder, name="../escape", revision=1), 422, "invalid_request"
        )

    def test_instructions_versions_memory_activation_and_context(self) -> None:
        path = self.project_path() + "/instructions"
        initial = self.request("GET", path, "viewer").json()
        self.validator.validate(
            initial, self.contract["components"]["schemas"]["Instructions"]
        )
        file = self.request(
            "GET", self.project_path() + "/files/" + initial["file_id"], "editor"
        ).json()
        text = "Use concise answers.\nPreserve UTF-8: résumé 日本語."
        approval = self.approved(
            "overwrite", ProjectFiles.overwrite_effects(file, text.encode())
        )
        updated = self.request(
            "PUT",
            path,
            "editor",
            dict(text=text, base_version=initial["version"], approval_id=approval),
            match=0,
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["text"], text)
        self.assertEqual(updated.json()["effective_revision"], 1)
        self.assertEqual(
            self.request(
                "GET",
                self.project_path()
                + "/files/"
                + file["id"]
                + "/content?version="
                + initial["version"],
                "viewer",
            ).content,
            b"",
        )
        response = self.request(
            "POST",
            self.project_path() + "/memory",
            "editor",
            dict(text="Preferred format is plain text", source_refs=[]),
        )
        self.assertEqual(response.status_code, 201, response.text)
        memory = response.json()
        self.assertEqual(memory["status"], "proposed")
        with store(self.app_dsn).scope(
            self.identity("viewer"), self.org, self.project
        ) as scope:
            self.assertEqual(ProjectFiles(scope).context()["memory"], [])
        active = self.request(
            "PATCH",
            self.project_path() + "/memory/" + memory["id"],
            "editor",
            dict(status="active"),
            match=0,
        )
        self.assertEqual(active.status_code, 200, active.text)
        self.validator.validate(
            active.json(), self.contract["components"]["schemas"]["MemoryEntry"]
        )
        with store(self.app_dsn).scope(
            self.identity("viewer"), self.org, self.project
        ) as scope:
            context = ProjectFiles(scope).context()
            self.assertEqual(context["instructions"]["text"], text)
            self.assertEqual(context["memory"][0]["text"], memory["text"])
            self.assertEqual(context["memory_revision"], 2)
        self.error(
            self.request(
                "PATCH",
                self.project_path() + "/memory/" + memory["id"],
                "editor",
                dict(text="Correction"),
                match=0,
            ),
            409,
            "revision_conflict",
        )

    def test_source_retention_revokes_extract_memory_and_content(self) -> None:
        folder = self.folder()
        file = self.uploaded(folder, b"The measured result is forty two.", "result.txt")
        sources: list[dict[str, Any]]
        with store(self.app_dsn).scope(
            self.identity("viewer"), self.org, self.project
        ) as scope:
            sources = ScopedProjectDocuments(ProjectFiles(scope)).sources()
        self.assertEqual(sources[0]["text"], "The measured result is forty two.")
        source_path = self.project_path() + "/sources/" + sources[0]["chunk_id"]
        resolved = self.request("GET", source_path, "viewer")
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.validator.validate(
            resolved.json(), self.contract["components"]["schemas"]["Source"]
        )
        memory = self.request(
            "POST",
            self.project_path() + "/memory",
            "editor",
            dict(
                text="Result: 42",
                source_refs=[
                    dict(
                        file_id=file["id"], version_id=file["version_id"], locator=None
                    )
                ],
            ),
        ).json()
        self.assertEqual(
            self.request(
                "PATCH",
                self.project_path() + "/memory/" + memory["id"],
                "editor",
                dict(status="active"),
                match=0,
            ).status_code,
            200,
        )
        approval = self.approved(
            "delete",
            dict(file_id=file["id"], base_version=file["version_id"], revision=0),
        )
        deleted = self.request(
            "DELETE",
            self.project_path() + "/files/" + file["id"] + "?approval_id=" + approval,
            "editor",
            match=0,
        )
        self.assertEqual(deleted.status_code, 202, deleted.text)
        self.assertIsNotNone(deleted.json()["purge_at"])
        for suffix in ("", "/content", "/versions"):
            self.error(
                self.request(
                    "GET",
                    self.project_path() + "/files/" + file["id"] + suffix,
                    "viewer",
                ),
                404,
                "not_found",
            )
        self.error(self.request("GET", source_path, "viewer"), 404, "not_found")
        self.assertEqual(
            self.request("GET", self.project_path() + "/memory", "viewer").json()[
                "items"
            ],
            [],
        )
        with psycopg.connect(self.admin_dsn) as db:
            rows = db.execute(
                "SELECT kind,retention_class,expires_at,data FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s",
                (self.org, self.project),
            ).fetchall()
        classes = {r[0]: r[1] for r in rows}
        self.assertEqual(classes["file"], "C")
        self.assertEqual(classes["derived"], "E")
        self.assertEqual(classes["outbox"], "A")
        self.assertEqual(classes["job"], "T")
        self.assertTrue(any(r[0] == "job" and r[2] is not None for r in rows))
        self.assertTrue(
            any(
                r[0] == "version"
                and r[3]["id"] == file["version_id"]
                and r[3]["deleted_at"]
                for r in rows
            )
        )

    def test_approval_forgery_range_and_blob_rollback(self) -> None:
        folder = self.folder()
        file = self.uploaded(folder, b"0123456789", "ten.txt")
        path = self.project_path() + "/files/" + file["id"]
        response = self.client.get(
            path + "/content", headers={**self.headers("viewer"), "Range": "bytes=2-4"}
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"234")
        self.assertEqual(response.headers["content-range"], "bytes 2-4/10")
        self.error(
            self.client.get(
                path + "/content",
                headers={**self.headers("viewer"), "Range": "bytes=99-100"},
            ),
            416,
            "range_not_satisfiable",
        )
        self.error(
            self.client.post(
                path + "/versions",
                headers=self.headers(revision=0),
                data={"base_version": file["version_id"], "approval_id": str(uuid4())},
                files={"file": ("ten.txt", b"changed")},
            ),
            403,
            "approval_required",
        )
        before = {p for p in self.blobs.rglob("*") if p.is_file()}
        with patch.object(
            Scope, "remember", side_effect=RuntimeError("transaction failure")
        ):
            with self.assertRaisesRegex(RuntimeError, "transaction failure"):
                self.upload(folder, b"rollback", name="rollback.txt", revision=1)
        self.assertEqual({p for p in self.blobs.rglob("*") if p.is_file()}, before)
        events = self.request("GET", self.ledger_path(), "viewer").json()["items"]
        self.assertEqual(
            [event["kind"] for event in events],
            ["folder_registered", "file_uploaded", "extraction_completed", "file_read"],
        )
        self.assertEqual(events[-1]["byte_counts"]["source_bytes"], 3)
        listing = self.request(
            "GET", self.project_path() + "/files?folder_id=" + folder["id"], "viewer"
        ).json()
        self.assertEqual([f["id"] for f in listing["items"]], [file["id"]])

    def test_resource_rls_references_expiry_and_source_policy(self) -> None:
        folder = self.folder()
        file = self.uploaded(folder, b"evidence", "evidence.txt")
        with store(self.app_dsn).scope(
            self.identity("admin"), self.org, self.second_project
        ) as scope:
            self.assertEqual(
                scope.db.execute(
                    "SELECT id FROM atlas_projects.resources WHERE project_id=%s",
                    (self.project,),
                ).fetchall(),
                [],
            )
            with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                ProjectFiles(scope).put(
                    "memory", ProjectFiles(scope).new(), parent=file["id"]
                )
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.resources SET data=jsonb_set(data,'{model_use}','\"deny\"') WHERE id=%s",
                (folder["id"],),
            )
        response = self.request(
            "POST",
            self.project_path() + "/memory",
            "editor",
            dict(
                text="restricted",
                source_refs=[
                    dict(
                        file_id=file["id"], version_id=file["version_id"], locator=None
                    )
                ],
            ),
        )
        self.error(response, 403, "policy_denied")
        with store(self.app_dsn).scope(
            self.identity("viewer"), self.org, self.project
        ) as scope:
            self.assertEqual(ScopedProjectDocuments(ProjectFiles(scope)).sources(), [])
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.resources SET expires_at=now()-interval '1 second' WHERE id=%s",
                (file["version_id"],),
            )
        self.error(
            self.request(
                "GET",
                self.project_path() + "/files/" + file["id"] + "/content",
                "viewer",
            ),
            404,
            "not_found",
        )

    def test_query_body_selector_injection_and_reserved_folders(self) -> None:
        folder = self.folder()
        response = self.client.post(
            self.project_path() + "/files?folder_id=" + folder["id"],
            headers=self.headers(),
            data=dict(
                folder_id=folder["id"], path="bad.txt", expected_parent_revision="0"
            ),
            files={"file": ("bad.txt", b"hello")},
        )
        self.error(response, 422, "invalid_request")
        response = self.client.post(
            self.project_path()
            + "/files?folder_id="
            + folder["id"]
            + "&folder_id="
            + folder["id"],
            headers=self.headers(),
            content=b"unused",
        )
        self.error(response, 422, "invalid_request")
        project = self.request("GET", self.project_path(), "viewer").json()
        folders = self.request(
            "GET", self.project_path() + "/folders", "viewer"
        ).json()["items"]
        deliverables = next(
            f for f in folders if f["id"] == project["deliverables_folder_id"]
        )
        self.assertEqual(deliverables["name"], "deliverables")
        self.assertEqual(deliverables["locality"], "server")
        self.error(
            self.request(
                "PATCH",
                self.project_path() + "/folders/" + deliverables["id"],
                "editor",
                dict(name="renamed"),
                match=0,
            ),
            403,
            "policy_denied",
        )

    def test_concurrent_upload_and_idempotent_replay(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        import httpx

        folder = self.folder()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: self.upload(folder, b"same content", "same.txt"), range(2)
                )
            )
        self.assertEqual(sorted(r.status_code for r in results), [201, 409])
        second = self.folder(name="Replay")
        request = httpx.Request(
            "POST",
            "http://testserver"
            + self.project_path()
            + "/files?folder_id="
            + second["id"],
            files=[
                ("file", ("replay.txt", b"persist once")),
                ("path", (None, "replay.txt")),
                ("expected_parent_revision", (None, "0")),
            ],
        )
        body = request.read()
        headers = {
            **self.headers(key="same-upload"),
            "Content-Type": request.headers["content-type"],
        }
        first = self.client.post(str(request.url), content=body, headers=headers)
        replay = self.client.post(str(request.url), content=body, headers=headers)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json(), replay.json())
        self.error(
            self.client.post(
                str(request.url), content=body + b"changed", headers=headers
            ),
            409,
            "idempotency_conflict",
        )
        listing = self.request(
            "GET", self.project_path() + "/files?folder_id=" + second["id"], "viewer"
        )
        self.assertEqual(len(listing.json()["items"]), 1)
        events = self.request("GET", self.ledger_path(), "viewer").json()["items"]
        uploaded = [event for event in events if event["kind"] == "file_uploaded"]
        self.assertEqual(len(uploaded), 2)
        self.assertEqual(
            sum(event["byte_counts"]["source_bytes"] for event in uploaded),
            len(b"same content") + len(b"persist once"),
        )

    def test_memory_deletion_approval_and_revision(self) -> None:
        memory = self.request(
            "POST",
            self.project_path() + "/memory",
            "editor",
            dict(text="Remember this", source_refs=[]),
            key="remember-once",
        ).json()
        path = self.project_path() + "/memory/" + memory["id"]
        self.error(
            self.request("DELETE", path, "editor", match=0), 403, "approval_required"
        )
        forged = self.request(
            "PATCH",
            path,
            "editor",
            dict(status="deleted", approval_id=str(uuid4())),
            match=0,
        )
        self.error(forged, 403, "approval_required")
        approval = self.approved(
            "delete", dict(memory_ids=[memory["id"]], memory_revision=1)
        )
        deleted = self.request(
            "DELETE", path + "?approval_id=" + approval, "editor", match=0
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["memory_revision"], 2)
        self.assertEqual(
            self.request("GET", self.project_path() + "/memory", "viewer").json()[
                "items"
            ],
            [],
        )
        self.error(
            self.request(
                "POST",
                self.project_path() + "/memory",
                "editor",
                dict(text="Remember this", source_refs=[]),
                key="remember-once",
            ),
            404,
            "not_found",
        )
        with store(self.app_dsn).scope(
            self.identity("viewer"), self.org, self.project
        ) as scope:
            self.assertEqual(ProjectFiles(scope).context()["memory_revision"], 2)

    def test_superseded_retention_immutable_metadata_and_expired_approval(self) -> None:
        folder = self.folder()
        file = self.uploaded(folder, b"before", "notes.txt")
        with psycopg.connect(self.admin_dsn) as db:
            retention = dict(self.policy["retention"], superseded_versions_days=1)
            db.execute(
                "UPDATE atlas_projects.resources SET data=jsonb_set(data,'{retention_override}',%s) WHERE id=%s",
                (Jsonb(retention), folder["id"]),
            )
        path = self.project_path() + "/files/" + file["id"] + "/versions"
        content = b"after"
        effects = ProjectFiles.overwrite_effects(file, content)
        # A whole-second RFC3339 expiry is already past during this second;
        # lexical comparison against a fractional timestamp would allow it.
        expiry = timestamp()[:19] + "Z"
        expired = self.approved("overwrite", effects, expires=expiry)
        self.error(
            self.client.post(
                path,
                headers=self.headers(revision=0),
                data=dict(base_version=file["version_id"], approval_id=expired),
                files={"file": ("notes.txt", content)},
            ),
            410,
            "approval_expired_or_replayed",
        )
        approval = self.approved("overwrite", effects)
        response = self.client.post(
            path,
            headers=self.headers(revision=0),
            data=dict(base_version=file["version_id"], approval_id=approval),
            files={"file": ("notes.txt", content)},
        )
        self.assertEqual(response.status_code, 201, response.text)
        with store(self.app_dsn).scope(
            self.identity("viewer"), self.org, self.project
        ) as scope:
            sources = ScopedProjectDocuments(ProjectFiles(scope)).sources()
            self.assertEqual([source["text"] for source in sources], ["after"])
            row = ProjectFiles(scope).row("version", file["version_id"])
            self.assertIsNotNone(row["expires_at"])
        with self.assertRaises(psycopg.errors.CheckViolation):
            with store(self.app_dsn).scope(
                self.identity("editor"), self.org, self.project
            ) as scope:
                scope.db.execute(
                    "UPDATE atlas_projects.resources SET data=jsonb_set(data,'{sha256}',to_jsonb(repeat('0',64))) WHERE id=%s",
                    (file["version_id"],),
                )

    def test_synced_empty_upload_limits_and_cursor_filters(self) -> None:
        folder = self.folder("synced")
        file = self.uploaded(folder, b"", "empty.bin")
        self.assertEqual(file["size_bytes"], 0)
        self.assertEqual(file["locality"], "synced")
        self.assertEqual(
            self.request(
                "GET",
                self.project_path() + "/files/" + file["id"] + "/content",
                "viewer",
            ).content,
            b"",
        )
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=jsonb_set(policy,'{quotas,upload_bytes}','4') WHERE org_id=%s",
                (self.org,),
            )
        refused = self.upload(folder, b"12345", "too-large.bin", revision=1)
        self.error(refused, 413, "size_limit")
        self.assertEqual(
            refused.json()["measurements"], [dict(measured=5, limit=4, unit="bytes")]
        )
        listing = self.request(
            "GET",
            self.project_path() + "/files?folder_id=" + folder["id"] + "&limit=1",
            "viewer",
        )
        self.assertEqual([v["id"] for v in listing.json()["items"]], [file["id"]])


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    # Borrow the real database fixture, not task B's tests with altered quotas.
    # Those run unchanged in tests.test_project_isolation and full discovery.
    return unittest.TestSuite(
        ProjectFilesTests(name)
        for name in ProjectFilesTests.__dict__
        if name.startswith("test_")
    )
