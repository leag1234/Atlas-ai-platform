"""REQ-PROJ-03/04/07: private transcript and scoped public history on real RLS."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from psycopg.types.json import Jsonb

from services.retrieval.project_identity import sign
from services.retrieval.project_scoped_api import store
from services.retrieval.project_work import empty_usage

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_project_isolation import ProjectIsolationTests


class ProjectConversationTests(ProjectIsolationTests):
    def setUp(self) -> None:
        super().setUp()
        env = patch.dict(
            os.environ, {"ATLAS_PROJECT_EXECUTION_SECRET": os.urandom(32).hex()}
        )
        env.start()
        self.addCleanup(env.stop)
        reply = self.request(
            "POST",
            self.project_path() + "/conversations",
            "viewer",
            {"title": "Compare reports"},
        )
        self.assertEqual(reply.status_code, 201, reply.text)
        self.conversation = reply.json()["id"]
        self.turn_id = str(uuid4())

    def payload(self, **changes: Any) -> dict[str, Any]:
        return dict(
            model="atlas",
            org_id=self.org,
            project_id=self.project,
            conversation_id=self.conversation,
            messages=[dict(role="user", content="What changed?")],
            stream=True,
            file_version_ids=[],
            **changes,
        )

    def test_imported_conversation_is_private_ordered_and_editor_only(self) -> None:
        body = {
            "title": "Fictional briefing",
            "messages": [
                {
                    "role": "user",
                    "content": "What is the fictional budget?",
                    "created_at": "2026-10-29T09:00:00Z",
                    "attachment_file_ids": [],
                },
                {
                    "role": "assistant",
                    "content": "The fictional budget is twelve units.",
                    "created_at": "2026-10-29T09:01:00Z",
                    "attachment_file_ids": [],
                },
            ],
        }
        path = self.project_path() + "/conversations/import"
        self.error(self.request("POST", path, "viewer", body), 403, "permission_denied")
        response = self.request("POST", path, "editor", body)
        self.assertEqual(response.status_code, 201, response.text)
        conversation = response.json()
        self.assertEqual(conversation["visibility"], "private")
        turns = self.request(
            "GET",
            path.rsplit("/import", 1)[0] + "/" + conversation["id"] + "/turns",
            "editor",
        ).json()["items"]
        self.assertEqual(
            [turn["content_refs"][0]["text"] for turn in turns],
            [item["content"] for item in body["messages"]],
        )

    def test_import_rejects_more_than_bounded_messages(self) -> None:
        response = self.request(
            "POST",
            self.project_path() + "/conversations/import",
            "editor",
            {
                "title": "Bounded",
                "messages": [
                    {
                        "role": "user",
                        "content": "x",
                        "created_at": "2026-10-29T09:00:00Z",
                        "attachment_file_ids": [],
                    }
                ]
                * 501,
            },
        )
        self.error(response, 422, "invalid_request")

    def test_import_links_uploaded_project_file(self) -> None:
        from services.retrieval.project_files import ProjectFiles

        with store(self.app_dsn).scope(
            self.identity("editor", self.project), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            folder = files.folder(
                {"name": "Imported", "locality": "server", "model_use": "allow"}
            )
            attached = files.create_file(
                folder["id"], "brief.txt", b"fictional attachment", folder["revision"]
            )
        response = self.request(
            "POST",
            self.project_path() + "/conversations/import",
            "editor",
            {
                "title": "With file",
                "messages": [
                    {
                        "role": "user",
                        "content": "Read the fictional brief.",
                        "created_at": "2026-10-29T09:00:00Z",
                        "attachment_file_ids": [attached["id"]],
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        conversation = response.json()
        with store(self.app_dsn).scope(
            self.identity("editor", self.project), self.org, self.project
        ) as scope:
            turn = ProjectFiles(scope).listing("turn", conversation["id"])[0]
            self.assertEqual(turn["file_version_ids"], [attached["version_id"]])

    def broker(
        self,
        operation: str,
        data: dict[str, Any],
        role: str = "viewer",
        key: str | None = None,
        **headers: str,
    ) -> Any:
        return self.client.post(
            "/internal/atlas/transcript/" + operation,
            json=data,
            headers={
                "X-Atlas-Execution-Secret": os.environ[
                    "ATLAS_PROJECT_EXECUTION_SECRET"
                ],
                "X-Atlas-Execution-Audience": "atlas-retrieval-execution",
                "X-Atlas-Project-Identity": sign(
                    self.identity(role, self.project), self.key.encode()
                ),
                "Idempotency-Key": key or str(uuid4()),
                **headers,
            },
        )

    def begin(self, **kwargs: Any) -> Any:
        return self.broker(
            "begin", dict(request_id=self.turn_id, request=self.payload()), **kwargs
        )

    def finish_body(self) -> dict[str, Any]:
        return dict(
            request_id=self.turn_id,
            conversation_id=self.conversation,
            state="completed",
            content_refs=[
                dict(
                    kind="answer",
                    text="Revenue rose by 12 percent.",
                    file_version_id=None,
                )
            ],
            usage=dict(empty_usage(), cost_eur=0.02),
            error=None,
        )

    def test_chat_model_boundary_and_persisted_sse_through_real_broker(self) -> None:
        import asyncio
        from decimal import Decimal
        from unittest.mock import AsyncMock
        from services.orchestrator.project_transcript import process_scoped_project
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import Configuration, GatewayModel
        from services.orchestrator.stream_client import sink_context
        from services.retrieval.project_identity import ProjectError

        model = GatewayModel(
            "http://gateway.invalid",
            Configuration(
                input_eur_per_mtok=Decimal("0.5"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )
        from packages.profiles import PROFILES

        request = ChatRequest.model_validate(dict(self.payload(), model=PROFILES[0]))
        request._project_subject, request._transcript_key = "viewer", "model-turn"
        captured: list[dict[str, Any]] = []

        async def call(
            operation: str, body: dict[str, Any], key: str | None = None
        ) -> dict[str, Any]:
            reply = self.broker(operation, body, key=key)
            if reply.status_code not in (200, 201):
                raise ProjectError(reply.status_code, reply.json()["error"])
            return dict(reply.json())

        async def sink(event: dict[str, Any]) -> None:
            captured.append(event)

        async def execute() -> Interaction:
            token = sink_context.set(sink)
            try:
                item = Interaction()
                await process_scoped_project(request, item)
                await process_scoped_project(request, Interaction())
                return item
            finally:
                sink_context.reset(token)

        wire = dict(
            text=json.dumps(
                dict(answer="Revenue increased by twelve percent.", facts=[])
            ),
            calls=[],
            usage=dict(prompt_tokens=100, completion_tokens=20),
            cost_eur="0.0001",
        )
        with (
            patch(
                "services.orchestrator.project_transcript.TranscriptBroker.call",
                side_effect=call,
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(model, "post", AsyncMock(return_value=wire)) as transport,
        ):
            item = asyncio.run(execute())
        self.assertEqual(item.state, "done")
        self.assertEqual(item.reponse, "Revenue increased by twelve percent.")
        transport.assert_awaited_once()
        events = [e["project"] for e in captured]
        for event in events:
            self.validator.validate(
                event, self.contract["components"]["schemas"]["ProjectEvent"]
            )
        self.assertIn("model_dispatch_intent", [e.get("kind") for e in events])
        self.assertIn("model_acknowledged", [e.get("kind") for e in events])
        unique = {e["seq"]: e for e in events}
        self.assertEqual(sum(e.get("type") == "recap" for e in unique.values()), 1)

    def test_viewer_canonical_transcript_replay_and_reload(self) -> None:
        first = self.begin(key="turn")
        self.assertEqual(first.status_code, 201, first.text)
        self.validator.validate(
            first.json(), self.contract["components"]["schemas"]["TranscriptState"]
        )
        data = dict(request_id=str(uuid4()), request=self.payload())
        replay = self.broker("begin", data, key="turn")
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["replay"])
        self.assertEqual(replay.json()["turn"]["id"], self.turn_id)
        finish = self.broker("finish", self.finish_body())
        self.assertEqual(finish.status_code, 200, finish.text)
        self.assertEqual(
            self.broker("finish", self.finish_body()).json(), finish.json()
        )
        turns = self.request(
            "GET",
            self.project_path() + f"/conversations/{self.conversation}/turns",
            "viewer",
        )
        self.assertEqual(
            turns.json()["items"][0]["content_refs"][1]["text"],
            "Revenue rose by 12 percent.",
        )
        self.validator.validate(
            turns.json(), self.contract["components"]["schemas"]["TurnPage"]
        )
        context = self.request(
            "POST",
            self.project_path() + "/context",
            "viewer",
            dict(conversation_id=self.conversation, query="Why?", action="chat"),
        )
        self.assertEqual(context.json()["history"][0]["id"], self.turn_id)
        events = self.request("GET", self.project_path() + "/events", "viewer")
        values = [
            json.loads(line[6:])
            for line in events.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(sum(e.get("type") == "recap" for e in values), 1)
        last = max(e["seq"] for e in values)
        self.assertEqual(
            self.request(
                "GET", self.project_path() + f"/events?after={last}", "viewer"
            ).text,
            "",
        )
        with store(self.app_dsn).scope(
            self.identity("viewer"), self.org, self.project
        ) as scope:
            self.assertAlmostEqual(scope.usage("daily_model_eur"), 0.02)

    def test_conflicts_and_other_member_cannot_finish(self) -> None:
        self.assertEqual(self.begin(key="turn").status_code, 201)
        body = dict(
            request_id=str(uuid4()),
            request=dict(
                self.payload(), messages=[dict(role="user", content="Different")]
            ),
        )
        self.error(self.broker("begin", body, key="turn"), 409, "idempotency_conflict")
        self.error(
            self.broker("finish", self.finish_body(), role="editor"), 404, "not_found"
        )
        invalid = self.finish_body()
        invalid["content_refs"][0]["kind"] = "question"
        self.error(self.broker("finish", invalid), 422, "invalid_request")
        self.assertEqual(self.broker("finish", self.finish_body()).status_code, 200)
        self.error(
            self.broker("finish", dict(self.finish_body(), state="failed")),
            409,
            "idempotency_conflict",
        )

    def test_two_organization_denial_matrix(self) -> None:
        for role in ("other", "ungranted", "revoked", "admin"):
            for suffix in (
                "/conversations",
                f"/conversations/{self.conversation}/turns",
                "/events",
            ):
                with self.subTest(role=role, suffix=suffix):
                    self.error(
                        self.request("GET", self.project_path() + suffix, role),
                        404,
                        "not_found",
                    )
            self.error(self.begin(role=role), 404, "not_found")
        body = self.payload()
        body["project_id"] = self.foreign_project
        self.error(
            self.broker("begin", dict(request_id=self.turn_id, request=body)),
            404,
            "not_found",
        )

    def test_viewer_work_denied_and_archive_chat_allowed(self) -> None:
        body = self.payload(project_action="work", work_session_id=str(uuid4()))
        self.error(
            self.broker("begin", dict(request_id=self.turn_id, request=body)),
            403,
            "permission_denied",
        )
        archived = self.request(
            "PATCH", self.project_path(), "owner", dict(state="archived"), match=0
        )
        self.assertEqual(archived.status_code, 200, archived.text)
        self.assertEqual(self.begin().status_code, 201)
        self.assertEqual(self.broker("finish", self.finish_body()).status_code, 200)

    def test_private_ingress_and_closed_schema(self) -> None:
        for headers in ({"Origin": "https://example.test"}, {"Sec-Fetch-Mode": "cors"}):
            self.error(self.begin(**headers), 403, "origin_rejected")
        self.error(
            self.broker(
                "begin",
                dict(
                    request_id=self.turn_id,
                    request=self.payload(),
                    member_id=self.members["owner"],
                ),
            ),
            422,
            "invalid_request",
        )
        self.error(
            self.client.post("/internal/atlas/transcript/begin", json={}),
            401,
            "unauthenticated",
        )

    def test_progress_is_authoritative_idempotent_and_not_a_work_backdoor(self) -> None:
        self.assertEqual(self.begin().status_code, 201)
        event = json.loads(
            Path("contracts/examples/project-mode/ProgressEvent.valid.json").read_text()
        )
        event.update(
            org_id=self.org,
            project_id=self.project,
            request_id=self.turn_id,
            turn_id=None,
            session_id=None,
            approval_id=None,
            job_id=None,
            type="recap",
            status="running",
            tool=None,
            plan=[],
            output={},
        )
        body = dict(
            request_id=self.turn_id, conversation_id=self.conversation, events=[event]
        )
        first = self.broker("events", body, key="progress")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["events"][0]["turn_id"], self.turn_id)
        self.assertEqual(
            self.broker("events", body, key="progress").json(), first.json()
        )
        event["type"] = "tool"
        self.error(self.broker("events", body), 422, "invalid_request")
        event["type"], event["project_id"] = "recap", self.foreign_project
        self.error(self.broker("events", body), 404, "not_found")
        self.assertEqual(
            self.request(
                "DELETE",
                self.project_path() + "/grants/" + self.members["viewer"],
                "owner",
                match=1,
            ).status_code,
            204,
        )
        self.error(self.broker("events", body, key="progress"), 404, "not_found")

    def test_chat_reservation_reports_measured_quota_refusal(self) -> None:
        import psycopg

        with psycopg.connect(self.admin_dsn) as db:
            policy = dict(self.policy)
            policy["quotas"] = dict(policy["quotas"], daily_model_eur=0.05)
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s WHERE org_id=%s",
                (Jsonb(policy), self.org),
            )
        refused = self.begin()
        self.error(refused, 429, "budget_exceeded")
        self.assertEqual(refused.json()["measurements"][0]["measured"], 0.1)
        self.assertEqual(refused.json()["measurements"][0]["limit"], 0.05)
        turns = self.request(
            "GET",
            self.project_path() + f"/conversations/{self.conversation}/turns",
            "viewer",
        )
        self.assertEqual(turns.json()["items"], [])

    def test_operator_upgrade_is_repeatable_with_existing_transcripts(self) -> None:
        from services.retrieval.project_install import upgrade_work

        self.assertEqual(self.begin().status_code, 201)
        store(self.app_dsn).pool.close()
        upgrade_work(self.admin_dsn)
        upgrade_work(self.admin_dsn)
        response = self.broker("finish", self.finish_body())
        self.assertEqual(response.status_code, 200, response.text)
        turns = self.request(
            "GET",
            self.project_path() + f"/conversations/{self.conversation}/turns",
            "viewer",
        )
        self.assertEqual(turns.json()["items"][0]["id"], self.turn_id)

    def test_unknown_cost_and_quota(self) -> None:
        self.assertEqual(self.begin().status_code, 201)
        finished = self.broker("finish", dict(self.finish_body(), state="failed"))
        self.assertEqual(finished.status_code, 200, finished.text)
        self.assertEqual(finished.json()["usage"]["unknown_eur"], 0.1)
        with store(self.app_dsn).scope(
            self.identity("viewer"), self.org, self.project
        ) as scope:
            self.assertEqual(scope.usage("daily_model_eur"), 0.1)

    def test_stale_history_is_inspectable_but_not_context(self) -> None:
        self.assertEqual(self.begin().status_code, 201)
        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            from services.retrieval.project_files import ProjectFiles

            ProjectFiles(scope).memory_revision()
        result = self.broker("finish", self.finish_body())
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["state"], "stale")
        context = self.request(
            "POST",
            self.project_path() + "/context",
            "viewer",
            dict(conversation_id=self.conversation, query="Again?", action="chat"),
        )
        self.assertEqual(context.json()["history"], [])

    def test_parent_source_policy_precedes_context_selection(self) -> None:
        from services.retrieval.project_files import ProjectFiles

        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            parent = files.folder(
                dict(name="Restricted", locality="server", model_use="deny")
            )
            child = files.folder(
                dict(
                    name="Reports",
                    parent_id=parent["id"],
                    locality="server",
                    model_use="allow",
                )
            )
            file = files.create_file(
                child["id"], "report.txt", b"Restricted report", child["revision"]
            )
        request = dict(self.payload(), file_version_ids=[file["version_id"]])
        response = self.broker("begin", dict(request_id=self.turn_id, request=request))
        self.error(response, 403, "policy_denied")
        turns = self.request(
            "GET",
            self.project_path() + f"/conversations/{self.conversation}/turns",
            "viewer",
        )
        self.assertEqual(turns.json()["items"], [])

    def test_selected_source_policy_rechecked_before_finish(self) -> None:
        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            from services.retrieval.project_files import ProjectFiles

            files = ProjectFiles(scope)
            folder = files.folder(
                dict(name="Reports", locality="server", model_use="allow")
            )
            result = files.create_file(
                folder["id"], "report.txt", b"Revenue rose.", folder["revision"]
            )
            version = result["version_id"]
            from services.retrieval.project_documents import ScopedProjectDocuments

            job = files.job("ingestion", [result["id"]])
            ScopedProjectDocuments(files).ingest(result, b"Revenue rose.", job)
        body = self.payload()
        body["file_version_ids"] = [version]
        first = self.broker(
            "begin", dict(request_id=self.turn_id, request=body), key="source"
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertIn("Revenue rose.", str(first.json()["context"]["passages"]))
        context_body = dict(
            conversation_id=self.conversation, query="Revenue?", action="chat"
        )
        context = self.request(
            "POST",
            self.project_path() + "/context",
            "viewer",
            context_body,
            key="source-context",
        )
        self.assertEqual(context.status_code, 200, context.text)
        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            folder["model_use"] = "deny"
            scope.db.execute(
                "UPDATE atlas_projects.resources SET data=%s WHERE org_id=%s AND id=%s",
                (Jsonb(folder), self.org, folder["id"]),
            )
        self.error(self.broker("finish", self.finish_body()), 403, "policy_denied")
        self.error(
            self.request(
                "POST",
                self.project_path() + "/context",
                "viewer",
                context_body,
                key="source-context",
            ),
            403,
            "policy_denied",
        )
        self.error(
            self.broker(
                "begin", dict(request_id=self.turn_id, request=body), key="source"
            ),
            403,
            "policy_denied",
        )
        turns = self.request(
            "GET",
            self.project_path() + f"/conversations/{self.conversation}/turns",
            "viewer",
        )
        self.assertEqual(turns.json()["items"], [])
