"""REQ-PROJ-05: real formats, private publication, durable bytes and derived previews."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from packages.doc_render import render, verify
from packages.doc_render.schema import Content, Section
from services.retrieval.project_files import ProjectFiles, reopen_artifact
from services.retrieval.project_identity import sign
from services.retrieval.project_scoped_api import store

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_project_work import ProjectWorkTests
import test_project_auth as auth


class ProjectDeliverableTests(ProjectWorkTests):
    retrieval_url: str

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from services.retrieval.api import app

        cls.retrieval_url = getattr(auth.ProjectAuthTests.serve, "__func__")(cls, app)

    def publication_setup(self) -> None:
        # Match ProjectFilesTests' real-document fixture. Quota-refusal tests
        # retain the isolation fixture's deliberately tiny upload ceiling.
        for name in (
            "upload_bytes",
            "stored_bytes",
            "versioned_bytes",
            "derived_bytes",
        ):
            self.policy["quotas"][name] = 16777216
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s",
                (Jsonb(self.policy),),
            )
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s WHERE org_id=%s",
                (Jsonb(self.policy), self.org),
            )

    def selected_sources(self) -> list[dict[str, str]]:
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            folder = files.folder(
                dict(name="Inputs", locality="server", model_use="allow")
            )
            sources = [
                files.create_file(
                    folder["id"], "first.txt", b"First source evidence", 0
                ),
                files.create_file(
                    folder["id"], "second.txt", b"Second source evidence", 1
                ),
            ]
        response = self.request(
            "POST",
            self.project_path() + "/work-sessions",
            "editor",
            dict(
                conversation_id=self.conversation,
                folder_ids=[folder["id"]],
                base_versions=[
                    dict(file_id=f["id"], version_id=f["version_id"]) for f in sources
                ],
            ),
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.session = response.json()
        return [dict(file_id=f["id"], version=f["version_id"]) for f in sources]

    def test_work_dispatch_publishes_all_formats_with_source_bound_receipts(
        self,
    ) -> None:
        from services.orchestrator.code_api import ExecutionBroker
        from services.orchestrator.code_mode import WorkExecution
        from services.orchestrator.code_tools import WorkTools, declarations
        from services.orchestrator.code_worker import perform
        from services.orchestrator.loop import Call

        self.publication_setup()
        sources = self.selected_sources()
        acquired = self.acquire()
        self.assertEqual(acquired.status_code, 200)
        execution = WorkExecution(
            ExecutionBroker("editor", self.org, self.project),
            acquired.json()["session"],
            self.turn_id,
        )
        args = dict(
            path="findings.docx",
            content=dict(
                title="Findings",
                sections=[
                    dict(
                        heading="Evidence",
                        paragraphs=["First source evidence", "Second source evidence"],
                    )
                ],
            ),
            sources=sources,
        )
        self.assertIn("publish_document", str(declarations(scoped=True)))
        self.assertNotIn("publish_document", str(declarations()))
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "ATLAS_RETRIEVAL_URL": self.retrieval_url,
                    "ATLAS_PROJECT_IDENTITY_SECRET": os.urandom(32).hex(),
                },
            ),
        ):
            workspace = MagicMock()
            workspace.invoke = AsyncMock(
                side_effect=lambda name, data, timeout: perform(
                    Path(directory), name, data
                )
            )
            tools = WorkTools(workspace, execution)

            async def run() -> None:
                for suffix in (".docx", ".xlsx", ".pptx", ".pdf"):
                    reply = await tools.execute(
                        Call(
                            str(uuid4()),
                            "publish_document",
                            json.dumps(dict(args, path="findings" + suffix)),
                        ),
                        60,
                    )
                    self.assertNotIn("error", reply, reply)
                    receipt = reply["deliverable"]
                    assert isinstance(receipt, dict)
                    assert isinstance(reply["text"], str)
                    self.assertEqual(
                        receipt["source_version_ids"], [s["version"] for s in sources]
                    )
                    self.assertEqual(receipt["turn_id"], self.turn_id)
                    self.assertEqual(receipt["session_id"], self.session["id"])
                    self.assertIn(receipt["download_route"], reply["text"])
                    self.assertIn("/preview", reply["text"])
                for bad in (
                    dict(
                        args,
                        path="missing.pdf",
                        content=dict(
                            title="Unrelated",
                            sections=[
                                dict(
                                    heading="No evidence", paragraphs=["Unrelated text"]
                                )
                            ],
                        ),
                    ),
                    dict(args, path="forged.pdf", validation=dict(status="passed")),
                    dict(
                        args,
                        path="foreign.pdf",
                        sources=[
                            dict(file_id=str(uuid4()), version=sources[0]["version"])
                        ],
                    ),
                    dict(args, path="../escape.pdf"),
                ):
                    refused = await tools.execute(
                        Call(str(uuid4()), "publish_document", json.dumps(bad)), 60
                    )
                    self.assertIn("error", refused)
                self.assertEqual(len(tools.deliverables), 4)

            asyncio.run(run())
        # Temporary reader workspace has expired; all canonical bytes remain readable.
        for receipt in tools.deliverables:
            raw = self.request("GET", receipt["download_route"], "viewer").content
            self.assertEqual(hashlib.sha256(raw).hexdigest(), receipt["sha256"])
            preview = self.request(
                "GET",
                receipt["download_route"].removesuffix("download") + "preview",
                "viewer",
            )
            self.assertEqual(preview.status_code, 200)
            self.assertIn(b"First source evidence", preview.content)
            self.assertIn(b"Second source evidence", preview.content)
        listing = self.request(
            "GET", self.project_path() + "/deliverables", "viewer"
        ).json()["items"]
        self.assertEqual(len(listing), 4)

    def test_work_lifecycle_keeps_verified_links_after_real_worker_cleanup(
        self,
    ) -> None:
        from decimal import Decimal
        from services.orchestrator.terminal_stack import IMAGE
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.code_mode import CodeModel, process_work
        from services.orchestrator.code_workspace import volume
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Call, CodeResult

        try:
            available = (
                subprocess.run(
                    ["docker", "image", "inspect", IMAGE],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            available = False
        if not available:
            self.skipTest(
                "real document worker image unavailable; no image pull in task J"
            )
        self.publication_setup()
        sources = self.selected_sources()
        self.policy["quotas"]["temporary_bytes"] = 16777216
        self.policy["quotas"]["per_turn"]["expanded_bytes"] = 1048576
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s",
                (Jsonb(self.policy),),
            )
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s WHERE org_id=%s",
                (Jsonb(self.policy), self.org),
            )
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            session = files.get("work_session", self.session["id"])
            session["effective_limits"] = self.policy["quotas"]["per_turn"]
            files.save(session)
        request = ChatRequest.model_validate(
            dict(
                messages=[
                    dict(role="user", content="Create a report from both sources")
                ],
                project_id=self.project,
                conversation_id=self.conversation,
            )
        )
        item = Interaction()
        model = MagicMock(spec=CodeModel)
        model.spent, model.input_tokens, model.output_tokens, model.work_unknown = (
            Decimal(0),
            0,
            0,
            False,
        )
        receipts: list[dict[str, Any]] = []

        async def turn(*args: Any) -> CodeResult:
            tools = args[2]
            reply = await tools.execute(
                Call(
                    str(uuid4()),
                    "publish_document",
                    json.dumps(
                        dict(
                            path="report.pdf",
                            content=dict(
                                title="Findings",
                                sections=[
                                    dict(
                                        heading="Evidence",
                                        paragraphs=[
                                            "First source evidence",
                                            "Second source evidence",
                                        ],
                                    )
                                ],
                            ),
                            sources=sources,
                        )
                    ),
                ),
                60,
            )
            self.assertNotIn("error", reply, reply)
            receipts.append(reply["deliverable"])
            # The recap must preserve receipts even when the model omits every link.
            return CodeResult(
                text="The report contains both sources.",
                state="done",
                tool_calls=1,
                model_calls=0,
            )

        self.addCleanup(
            subprocess.run, ["docker", "volume", "rm", volume()], capture_output=True
        )
        with (
            patch.dict(
                os.environ,
                {
                    "ATLAS_PROJECT_MODE": "1",
                    "ATLAS_RETRIEVAL_URL": self.retrieval_url,
                    "ATLAS_PROJECT_IDENTITY_SECRET": os.urandom(32).hex(),
                },
            ),
            patch.object(CodeModel, "connect", AsyncMock(return_value=model)),
            patch("services.orchestrator.code_mode.execute_turn", side_effect=turn),
        ):
            asyncio.run(
                process_work(
                    request,
                    item,
                    subject="editor",
                    org_id=self.org,
                    project_id=self.project,
                    session_id=self.session["id"],
                )
            )
        self.assertEqual(item.state, "done")
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertIn(receipt["download_route"], item.reponse)
        self.assertIn("/preview", item.reponse)
        session = self.request(
            "GET",
            self.project_path() + "/work-sessions/" + self.session["id"],
            "editor",
        ).json()
        self.assertIsNone(session["lease"])
        self.assertEqual(session["state"], "completed")
        self.assertEqual(session["usage"]["tool_calls"], 1)
        downloaded = self.request("GET", receipt["download_route"], "viewer")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(
            hashlib.sha256(downloaded.content).hexdigest(), receipt["sha256"]
        )
        self.assertIn(
            b"Second source evidence", reopen_artifact(downloaded.content, ".pdf")
        )

    def artifact(self, suffix: str) -> tuple[bytes, bytes]:
        content = Content(
            title="Project findings",
            sections=(
                Section(
                    heading="Evidence", paragraphs=("Expected project content 42",)
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ("report" + suffix)
            render(content, path, image_root=Path(directory))
            verified = verify(path, expected=content)
            self.assertIn("Expected project content 42", "\n".join(verified.text))
            raw = path.read_bytes()
        return raw, reopen_artifact(raw, suffix)

    def metadata(
        self, raw: bytes, suffix: str = ".docx", revision: int = 0
    ) -> dict[str, Any]:
        return dict(
            self.fence(),
            turn_id=self.turn_id,
            path="report" + suffix,
            expected_parent_revision=revision,
            source_version_ids=[],
            strategy="created",
            validation=dict(
                status="passed",
                checks=["package_readable", "requested_content", "source_coverage"],
                warnings=[],
                verifier_exit=0,
                artifact_sha256=hashlib.sha256(raw).hexdigest(),
            ),
        )

    def publish(
        self,
        metadata: dict[str, Any],
        raw: bytes,
        preview: bytes | None = None,
        *,
        key: str | None = None,
        role: str = "editor",
        extra: dict[str, Any] | None = None,
    ) -> Any:
        parts: dict[str, Any] = dict(
            metadata=(None, json.dumps(metadata), "application/json"),
            file=("artifact", raw, "application/octet-stream"),
        )
        if preview is not None:
            parts["preview"] = ("preview", preview, "text/plain")
        parts.update(extra or {})
        return self.client.post(
            "/internal/atlas/execution/publish",
            files=parts,
            headers={
                "X-Atlas-Execution-Secret": os.environ[
                    "ATLAS_PROJECT_EXECUTION_SECRET"
                ],
                "X-Atlas-Execution-Audience": "atlas-retrieval-execution",
                "X-Atlas-Project-Identity": sign(
                    self.identity(role, self.project), self.key.encode()
                ),
                "Idempotency-Key": key or str(uuid4()),
            },
        )

    def test_four_formats_durable_download_preview_hash_and_idempotency(self) -> None:
        self.publication_setup()
        self.assertEqual(self.acquire().status_code, 200)
        for revision, suffix in enumerate((".docx", ".xlsx", ".pptx", ".pdf")):
            with self.subTest(format=suffix):
                raw, preview = self.artifact(suffix)
                data, key = self.metadata(raw, suffix, revision), str(uuid4())
                reply = self.publish(data, raw, preview, key=key)
                self.assertEqual(reply.status_code, 201, reply.text)
                value = reply.json()
                self.validator.validate(
                    value, self.contract["components"]["schemas"]["Deliverable"]
                )
                self.assertEqual(value["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(
                    value["validation"]["artifact_sha256"], value["sha256"]
                )
                replay = self.publish(data, raw, preview, key=key)
                self.assertEqual(replay.status_code, 201, replay.text)
                self.assertEqual(replay.json(), value)
                self.error(
                    self.publish(
                        dict(data, path="different" + suffix), raw, preview, key=key
                    ),
                    409,
                    "idempotency_conflict",
                )
                # Builder scratch was destroyed before publication. No delivery cache exists.
                download = self.request("GET", value["download_route"], "viewer")
                self.assertEqual(
                    download.status_code,
                    200,
                    download.text[:100] if suffix == ".pdf" else "",
                )
                self.assertEqual(download.content, raw)
                derived = self.request(
                    "GET",
                    value["download_route"].removesuffix("download") + "preview",
                    "viewer",
                )
                self.assertEqual(derived.status_code, 200)
                self.assertEqual(derived.content, preview)
                self.assertIn(b"Expected project content 42", derived.content)
                self.assertEqual(derived.headers["x-content-type-options"], "nosniff")
        ledger = self.request("GET", self.project_path() + "/ledger", "viewer").json()[
            "items"
        ]
        self.assertEqual(sum(e["kind"] == "deliverable_created" for e in ledger), 4)
        for event in ledger:
            if event["kind"] == "deliverable_created":
                self.assertEqual(event["request_id"], self.turn_id)
                self.assertEqual(event["session_id"], self.session["id"])
                self.assertEqual(event["conversation_id"], self.conversation)

    def test_publication_refuses_missing_failed_or_forged_verification(self) -> None:
        self.publication_setup()
        self.assertEqual(self.acquire().status_code, 200)
        raw, preview = self.artifact(".docx")
        valid = self.metadata(raw)
        invalid = [
            dict(valid, validation=dict(valid["validation"], status="failed")),
            dict(valid, validation=dict(valid["validation"], verifier_exit=1)),
            dict(
                valid,
                validation=dict(valid["validation"], checks=["model_says_passed"]),
            ),
            dict(valid, validation=dict(valid["validation"], artifact_sha256="0" * 64)),
            dict(valid, turn_id=str(uuid4())),
            dict(valid, path="../escape.docx"),
            dict(valid, org_id=self.org),
        ]
        missing = copy.deepcopy(valid)
        del missing["validation"]
        invalid.append(missing)
        for data in invalid:
            with self.subTest(metadata=data):
                self.error(self.publish(data, raw, preview), 422, "invalid_request")
        malformed = b"not an Office package"
        self.error(
            self.publish(self.metadata(malformed), malformed), 422, "invalid_request"
        )
        self.error(
            self.publish(valid, raw, b"invented preview"), 422, "invalid_request"
        )
        self.error(
            self.publish(valid, raw, extra={"scope": (None, self.project)}),
            422,
            "invalid_request",
        )
        self.assertEqual(
            self.request("GET", self.project_path() + "/deliverables", "viewer").json()[
                "items"
            ],
            [],
        )

    def test_publication_fences_revisions_roles_and_atomic_rollback(self) -> None:
        self.publication_setup()
        self.assertEqual(self.acquire().status_code, 200)
        raw, preview = self.artifact(".pdf")
        data = self.metadata(raw, ".pdf")
        self.error(self.publish(data, raw, role="viewer"), 403, "permission_denied")
        self.error(self.publish(dict(data, generation=2), raw), 409, "session_busy")
        self.error(
            self.publish(dict(data, expected_parent_revision=1), raw),
            409,
            "revision_conflict",
        )
        self.error(
            self.publish(dict(data, source_version_ids=[str(uuid4())]), raw),
            404,
            "not_found",
        )
        original = ProjectFiles.put

        def fail_outbox(
            files: ProjectFiles, kind: str, *args: Any, **kwargs: Any
        ) -> None:
            if kind == "deliverable":
                raise OSError("Injected publication transaction failure")
            original(files, kind, *args, **kwargs)

        with patch.object(ProjectFiles, "put", fail_outbox):
            self.error(self.publish(data, raw, preview), 503, "dependency_unavailable")
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            self.assertEqual(files.listing("deliverable"), [])
            self.assertEqual(files.listing("preview"), [])
            self.assertEqual(files.listing("derived"), [])
            self.assertEqual(
                files.listing("file", scope.project_data["deliverables_folder_id"]), []
            )
            self.assertEqual(
                files.get("folder", scope.project_data["deliverables_folder_id"])[
                    "revision"
                ],
                0,
            )
        success = self.publish(data, raw, preview)
        self.assertEqual(success.status_code, 201, success.text)
        self.assertEqual(
            self.turn(
                "release", terminal_state="completed", cleanup_confirmed=True
            ).status_code,
            200,
        )
        self.error(self.publish(data, raw), 409, "session_busy")

    def test_upload_ceiling_refuses_before_parsing_and_leaves_no_publication(
        self,
    ) -> None:
        self.assertEqual(self.acquire().status_code, 200)
        raw, _ = self.artifact(".docx")
        response = self.publish(self.metadata(raw), raw)
        self.error(response, 413, "size_limit")
        self.assertEqual(
            response.json()["measurements"],
            [dict(measured=len(raw), limit=100, unit="bytes")],
        )
        self.assertEqual(
            self.request("GET", self.project_path() + "/deliverables", "viewer").json()[
                "items"
            ],
            [],
        )

    def test_publisher_uses_real_identity_and_execution_http_boundary(self) -> None:
        from services.orchestrator.code_api import ExecutionBroker
        from services.orchestrator.code_mode import WorkExecution
        from services.orchestrator.presentation_delivery import build_project_document
        from services.orchestrator.terminal_client import ProjectPublisher

        self.publication_setup()
        acquired = self.acquire()
        self.assertEqual(acquired.status_code, 200)
        content = Content(
            title="Verified report",
            sections=(
                Section(heading="Findings", paragraphs=("Canonical project evidence",)),
            ),
        )
        document = build_project_document(
            content, ".docx", [], max_bytes=16777216, timeout=20
        )
        execution = WorkExecution(
            ExecutionBroker("editor", self.org, self.project),
            acquired.json()["session"],
            self.turn_id,
        )
        with patch.dict(
            os.environ,
            {
                "ATLAS_RETRIEVAL_URL": self.retrieval_url,
                "ATLAS_PROJECT_IDENTITY_SECRET": os.urandom(32).hex(),
            },
        ):
            result = asyncio.run(
                ProjectPublisher(execution).publish(
                    document, "verified.docx", "created", [], 0, str(uuid4()), 20
                )
            )
        reply = self.request("GET", result["download_route"], "viewer")
        self.assertEqual(reply.content, document.content)

    def test_preview_and_canonical_aliases_recheck_source_restrictions(self) -> None:
        self.publication_setup()
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            source_folder = files.folder(
                dict(name="Sources", locality="server", model_use="allow")
            )
            source = files.create_file(
                source_folder["id"], "source.txt", b"Expected project content 42", 0
            )
        response = self.request(
            "POST",
            self.project_path() + "/work-sessions",
            "editor",
            dict(
                conversation_id=self.conversation,
                folder_ids=[source_folder["id"]],
                base_versions=[
                    dict(file_id=source["id"], version_id=source["version_id"])
                ],
            ),
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.session = response.json()
        self.assertEqual(self.acquire().status_code, 200)
        raw, preview = self.artifact(".pdf")
        data = dict(
            self.metadata(raw, ".pdf"), source_version_ids=[source["version_id"]]
        )
        reply = self.publish(data, raw, preview)
        self.assertEqual(reply.status_code, 201, reply.text)
        value = reply.json()
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            version = files.get("version", value["file_version_id"])
            source_folder["model_use"] = "deny"
            files.save(source_folder)
        for path in (
            value["download_route"],
            value["download_route"].removesuffix("download") + "preview",
            self.project_path() + "/files/" + version["file_id"] + "/content",
        ):
            with self.subTest(path=path):
                self.error(self.request("GET", path, "viewer"), 403, "policy_denied")
        self.assertEqual(
            self.request("GET", self.project_path() + "/deliverables", "viewer").json()[
                "items"
            ],
            [],
        )


class BoundedDeliverableTests(unittest.TestCase):
    def test_trusted_builder_reopens_all_formats_and_refuses_missing_source_content(
        self,
    ) -> None:
        from services.orchestrator.presentation_delivery import build_project_document

        content = Content(
            title="Expected title",
            sections=(
                Section(heading="Evidence", paragraphs=("Verified source quotation",)),
            ),
        )
        for suffix in (".docx", ".xlsx", ".pptx", ".pdf"):
            with self.subTest(suffix=suffix):
                document = build_project_document(
                    content,
                    suffix,
                    ["Verified source quotation"],
                    max_bytes=16777216,
                    timeout=20,
                )
                self.assertEqual(
                    hashlib.sha256(document.content).hexdigest(), document.sha256
                )
                self.assertIn(b"Expected title", document.preview)
                self.assertIn(b"Verified source quotation", document.preview)
                self.assertEqual(
                    reopen_artifact(document.content, suffix), document.preview
                )
        with self.assertRaisesRegex(ValueError, "verification failed"):
            build_project_document(
                content,
                ".pdf",
                ["Missing required source quotation"],
                max_bytes=16777216,
                timeout=20,
            )
        with self.assertRaisesRegex(ValueError, "verification failed"):
            build_project_document(content, ".pdf", [], max_bytes=1, timeout=20)

    def test_scoped_links_refuse_foreign_and_unverified_records(self) -> None:
        from services.orchestrator.file_results import project_deliverable_links

        value = json.loads(
            Path("contracts/examples/project-mode/Deliverable.valid.json").read_text()
        )
        value["validation"]["verifier_exit"] = 0
        value["validation"]["artifact_sha256"] = value["sha256"]
        prefix = f"/api/atlas/orgs/{value['org_id']}/projects/{value['project_id']}/deliverables/{value['id']}"
        value["download_route"] = prefix + "/download"
        links = project_deliverable_links(value, value["org_id"], value["project_id"])
        self.assertIn(prefix + "/preview", links)
        for bad in (
            dict(value, project_id=str(uuid4())),
            dict(value, download_route="/api/v1/terminals/anything"),
            dict(value, validation=dict(value["validation"], status="failed")),
        ):
            with self.assertRaises(ValueError):
                project_deliverable_links(bad, value["org_id"], value["project_id"])
