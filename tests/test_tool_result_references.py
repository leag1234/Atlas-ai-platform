"""P1a retention policy, bounded references, scoped storage and recorded-loop replay."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from packages import tool_results
from packages.context_usage import measure
from packages.profiles import PROFILES
from services.orchestrator import code_worker
from services.orchestrator.loop import (
    Call,
    Limits,
    Message,
    Query,
    Reservation,
    Turn,
    run,
)
from services.orchestrator.result_references import ResultReferences
from services.orchestrator.work_tool_selection import GROUPS, WorkToolSelection

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_project_conversations as conversations


class ClearingPolicyTests(unittest.TestCase):
    def test_continuous_last_k_and_protected_results(self) -> None:
        records: list[dict[str, Any]] = [
            dict(id=str(i), tool="read_file", content="x" * 4000) for i in range(8)
        ]
        records.insert(
            2, dict(id="approval", tool="request_approval", content="approved")
        )
        records.insert(4, dict(id="plan", tool="update_plan", content="Keep this plan"))
        changes = tool_results.clear(records, 4)
        self.assertEqual([c["id"] for c in changes], ["0", "1", "2", "3"])
        self.assertTrue(all(len(c["summary"].splitlines()) == 2 for c in changes))
        self.assertTrue(all(c["tokens_removed"] > 0 for c in changes))
        for record in records:
            record["cleared"] = record["id"] in {c["id"] for c in changes}
        self.assertEqual(tool_results.clear(records, 4), [])
        records.append(dict(id="8", tool="web_fetch", content="page"))
        self.assertEqual([c["id"] for c in tool_results.clear(records, 4)], ["4"])
        self.assertEqual(len(tool_results.clear(records, 0)), 5)
        self.assertEqual(tool_results.clear(records, 100), [])
        with self.assertRaises(ValueError):
            tool_results.clear(records, -1)

    def test_reference_validation_unicode_ranges_and_config(self) -> None:
        identifier = str(uuid4()) + ":" + str(uuid4())
        self.assertEqual(
            tool_results.arguments(json.dumps(dict(id=identifier))), (identifier, None)
        )
        text = "🦉" * 20000
        result = tool_results.reopen(text, None)
        self.assertEqual(len(result["content"].encode()), 32768)
        self.assertTrue(result["has_more"])
        self.assertEqual(
            tool_results.reopen(text, dict(start=8192, end=8195))["content"], "🦉" * 3
        )
        for selected in (
            dict(start=-1, end=1),
            dict(start=0, end=8193),
            dict(start=2, end=1),
            dict(start=True, end=2),
        ):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                tool_results.reopen(text, selected)
        for raw in (
            "{}",
            "[]",
            '{"id":"../../outside"}',
            json.dumps(dict(id=identifier, range=dict(start=0, end=1), member="owner")),
        ):
            with self.assertRaises(ValueError):
                tool_results.arguments(raw)
        with patch.dict(os.environ, {"ATLAS_CONTEXT_KEEP_RESULTS": "2"}):
            self.assertEqual(tool_results.keep_results(), 2)
        with patch.dict(os.environ, {"ATLAS_CONTEXT_KEEP_RESULTS": "101"}):
            with self.assertRaises(ValueError):
                tool_results.keep_results()

    def test_all_group_tools_reachable_with_reopen_and_eight_slots(self) -> None:
        from agent_provider import AgentRequest

        for group, names in GROUPS.items():
            selection = WorkToolSelection(group)
            selection.reopen_enabled = True
            for name in names:
                selection.switch(json.dumps(dict(group=group, focus=name)))
                declarations = selection.declarations()
                self.assertLessEqual(len(declarations), 8)
                self.assertIn(name, selection.names)
                self.assertIn("reopen_result", selection.names)
                self.assertIn("update_plan", selection.names)
                self.assertIn("request_approval", selection.names)
                AgentRequest.model_validate(
                    dict(
                        messages=[dict(role="user", content="Continue")],
                        tools=declarations,
                        timeout=10.0,
                    )
                )


class ProjectResultTests(conversations.ProjectConversationTests):
    def storage_capacity(self) -> None:
        import psycopg

        # This fixture's default 100-byte quota is exercised separately below.
        # Large-result cases need a project with room for their actual bytes.
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=jsonb_set(ceilings,'{quotas,stored_bytes}','2000000')"
            )
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=jsonb_set(policy,'{quotas,stored_bytes}','2000000') WHERE org_id=%s",
                (self.org,),
            )

    def result_body(
        self, *, content: str = "Evidence " * 1000, tool: str = "read_file"
    ) -> dict[str, Any]:
        return dict(
            conversation_id=self.conversation,
            request_id=self.turn_id,
            generation=None,
            id=self.turn_id + ":" + str(uuid4()),
            tool=tool,
            content=content,
        )

    def open_body(
        self, identifier: str, selected: dict[str, int] | None = None
    ) -> dict[str, Any]:
        return dict(
            conversation_id=self.conversation,
            request_id=self.turn_id,
            generation=None,
            id=identifier,
            range=selected,
        )

    def test_storage_clearing_ledger_and_idempotent_reopen(self) -> None:
        self.storage_capacity()
        self.assertEqual(self.begin().status_code, 201)
        bodies = [self.result_body() for _ in range(5)]
        with patch.dict(os.environ, {"ATLAS_CONTEXT_KEEP_RESULTS": "4"}):
            for body in bodies:
                reply = self.broker("result-put", body, key=body["id"])
                self.assertEqual(reply.status_code, 200, reply.text)
        receipt = reply.json()
        self.assertEqual([c["id"] for c in receipt["cleared"]], [bodies[0]["id"]])
        event = receipt["events"][0]
        self.assertGreater(event["context_clearing"]["tokens_removed"], 0)
        self.assertNotIn(bodies[0]["content"], json.dumps(event))
        self.assertEqual(
            self.broker("result-put", bodies[-1], key=bodies[-1]["id"]).json(), receipt
        )
        opened = self.broker(
            "result-open", self.open_body(bodies[0]["id"], dict(start=3, end=100))
        )
        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertEqual(opened.json()["content"], bodies[0]["content"][3:100])
        self.assertTrue(opened.json()["has_more"])
        self.assertEqual(opened.json()["trust"], "untrusted")

    def test_reference_authority_ranges_and_content_size(self) -> None:
        self.storage_capacity()
        self.assertEqual(self.begin().status_code, 201)
        body = self.result_body(content="🦉" * 20000)
        self.assertEqual(self.broker("result-put", body).status_code, 200)
        self.assertEqual(
            self.broker("result-open", self.open_body(body["id"])).status_code, 200
        )
        for role in ("other", "ungranted", "revoked"):
            self.assertEqual(
                self.broker(
                    "result-open", self.open_body(body["id"]), role=role
                ).status_code,
                404,
            )
        self.assertEqual(
            self.broker(
                "result-open", self.open_body(body["id"], dict(start=0, end=8193))
            ).status_code,
            422,
        )
        bad = self.open_body(str(uuid4()) + ":" + str(uuid4()))
        self.assertEqual(self.broker("result-open", bad).status_code, 404)
        huge = self.result_body(content="🦉" * 262145)
        self.assertEqual(self.broker("result-put", huge).status_code, 413)
        mismatched = self.open_body(body["id"])
        mismatched["conversation_id"] = str(uuid4())
        self.assertEqual(self.broker("result-open", mismatched).status_code, 404)

    def test_protected_results_and_reload_after_turn_finish(self) -> None:
        self.assertEqual(self.begin().status_code, 201)
        protected = self.result_body(
            tool="request_approval", content='{"decision":"denied"}'
        )
        with patch.dict(os.environ, {"ATLAS_CONTEXT_KEEP_RESULTS": "0"}):
            reply = self.broker("result-put", protected)
        self.assertEqual(reply.json()["cleared"], [])
        self.assertEqual(self.broker("finish", self.finish_body()).status_code, 200)
        self.turn_id = str(uuid4())
        self.assertEqual(self.begin().status_code, 201)
        reply = self.broker("result-open", self.open_body(protected["id"]))
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["content"], protected["content"])

    def test_quota_refusal_does_not_store_or_clear(self) -> None:
        self.assertEqual(self.begin().status_code, 201)
        body = self.result_body(content="x" * 101)
        refusal = self.broker("result-put", body)
        self.assertEqual(refusal.status_code, 429)
        self.assertEqual(refusal.json()["measurements"][0]["limit"], 100)
        self.assertEqual(
            self.broker("result-open", self.open_body(body["id"])).status_code, 404
        )

    def test_reopen_rechecks_original_source_even_with_same_idempotency_key(
        self,
    ) -> None:
        from psycopg.types.json import Jsonb
        from services.retrieval.project_scoped_api import store
        from services.retrieval.project_files import ProjectFiles

        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            files = ProjectFiles(scope)
            folder = files.folder(
                dict(name="Sources", locality="server", model_use="allow")
            )
            file = files.create_file(
                folder["id"], "input.txt", b"Evidence", folder["revision"]
            )
        payload = self.payload()
        payload["file_version_ids"] = [file["version_id"]]
        self.assertEqual(
            self.broker(
                "begin", dict(request_id=self.turn_id, request=payload)
            ).status_code,
            201,
        )
        result = self.result_body(content="Evidence")
        self.assertEqual(self.broker("result-put", result).status_code, 200)
        self.assertEqual(
            self.broker(
                "result-open", self.open_body(result["id"]), key="open-once"
            ).status_code,
            200,
        )
        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            folder["model_use"] = "deny"
            scope.db.execute(
                "UPDATE atlas_projects.resources SET data=%s WHERE org_id=%s AND id=%s",
                (Jsonb(folder), self.org, folder["id"]),
            )
        self.assertEqual(
            self.broker(
                "result-open", self.open_body(result["id"]), key="open-once"
            ).status_code,
            403,
        )

    def test_work_results_require_current_lease_and_same_session(self) -> None:
        from services.retrieval.project_scoped_api import store
        from services.retrieval.project_work import Work

        # The creator must explicitly share before another member starts Work.
        shared = self.request(
            "POST",
            self.project_path() + f"/conversations/{self.conversation}/share",
            "viewer",
            {"confirm": True},
            match=0,
        )
        self.assertEqual(shared.status_code, 200, shared.text)
        sessions = []
        original_id = ""
        for index in range(2):
            reply = self.request(
                "POST",
                self.project_path() + "/work-sessions",
                "editor",
                dict(
                    conversation_id=self.conversation, folder_ids=[], base_versions=[]
                ),
            )
            self.assertEqual(reply.status_code, 201, reply.text)
            session = reply.json()
            sessions.append(session)
            self.turn_id = str(uuid4())
            payload = self.payload(project_action="work", work_session_id=session["id"])
            begun = self.broker(
                "begin", dict(request_id=self.turn_id, request=payload), role="editor"
            )
            self.assertEqual(begun.status_code, 201)
            with store(self.app_dsn).scope(
                self.identity("editor"), self.org, self.project
            ) as scope:
                Work(scope).acquire(
                    dict(
                        session_id=session["id"],
                        request_id=self.turn_id,
                        expected_revision=session["revision"],
                        storage_bytes=0,
                    )
                )
            body = self.result_body(content="Work evidence")
            body["generation"] = 2
            self.assertEqual(
                self.broker("result-put", body, role="editor").status_code, 409
            )
            body["generation"] = 1
            self.assertEqual(
                self.broker("result-put", body, role="editor").status_code, 200
            )
            import asyncio
            from unittest.mock import AsyncMock
            from services.orchestrator.project_transcript import (
                DispatchEvents,
                TranscriptBroker,
            )

            recorded_events = []

            async def publish(operation: str, data: dict[str, Any]) -> dict[str, Any]:
                reply = self.broker(operation, data, role="editor")
                self.assertEqual(reply.status_code, 200, reply.text)
                recorded_events.extend(reply.json()["events"])
                return dict(reply.json())

            broker = TranscriptBroker("editor", self.org, self.project)
            events = DispatchEvents(broker, begun.json())
            events.session_id, events.generation = session["id"], 1
            with patch.object(broker, "call", AsyncMock(side_effect=publish)):
                asyncio.run(
                    events.publish(
                        "intent",
                        dict(
                            messages=[
                                dict(role="system", content="Pinned instruction")
                            ],
                            tools=[],
                            profile=PROFILES[0],
                        ),
                    )
                )
                asyncio.run(
                    events.publish(
                        "acknowledged",
                        dict(usage=dict(prompt_tokens=3, completion_tokens=1)),
                    )
                )
            self.assertEqual(
                recorded_events[-1]["model_dispatch"]["context"][
                    "provider_input_tokens"
                ],
                3,
            )
            self.assertEqual(recorded_events[-1]["session_id"], session["id"])
            if index == 0:
                original_id = body["id"]
            else:
                open_body = dict(self.open_body(original_id), generation=1)
                self.assertEqual(
                    self.broker("result-open", open_body, role="editor").status_code,
                    404,
                )
            with store(self.app_dsn).scope(
                self.identity("editor"), self.org, self.project
            ) as scope:
                Work(scope).turn(
                    dict(
                        session_id=session["id"],
                        request_id=self.turn_id,
                        generation=1,
                        operation="release",
                        usage=None,
                        terminal_state="completed",
                        cleanup_confirmed=True,
                    )
                )
            # Releasing a worker lease is separate from committing its transcript.
            # Complete this turn before exercising the next session's isolation.
            finished = self.broker("finish", self.finish_body(), role="editor")
            self.assertEqual(finished.status_code, 200, finished.text)


class ReplayBroker:
    """Test-only storage replay. Real RLS and ledger boundaries are tested above."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def call(
        self, operation: str, data: dict[str, Any], key: str | None = None
    ) -> dict[str, Any]:
        if operation == "result-open":
            record = next(r for r in self.records if r["id"] == data["id"])
            return dict(
                id=record["id"],
                tool=record["tool"],
                **tool_results.reopen(record["content"], data["range"]),
            )
        self.records.append(dict(data, cleared=False))
        changes = tool_results.clear(self.records, 4)
        for record in self.records:
            if record["id"] in {c["id"] for c in changes}:
                record["cleared"] = True
        return dict(id=data["id"], cleared=changes, events=[])


class WorkReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_sixty_step_work_replay_preserves_plan_and_flattens_curve(
        self,
    ) -> None:
        # Replay real local worker read results; model choices are scripted test
        # controls, explicitly not a provider-quality or recall measurement.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text(
                "Evidence line with numeric value 37.25.\n" * 150
            )
            recorded = code_worker.perform(
                root, "read_file", dict(path="source.txt", start_line=1, end_line=150)
            )

        class Replay:
            def __init__(self) -> None:
                self.step = 0
                self.curve: list[int] = []
                self.captures: list[list[Message]] = []

            def estimate(self, messages: list[Message] | Call) -> Reservation:
                return Reservation(100000, Decimal(0))

            async def complete(self, messages: list[Message], timeout: float) -> Turn:
                self.captures.append(copy.deepcopy(messages))
                self.curve.append(measure(messages, [], None)["total_tokens"])
                self.step += 1
                if self.step > 60:
                    return Turn(
                        "Completed the retained plan.", usage=Reservation(0, Decimal(0))
                    )
                return Turn(
                    "Retain the plan.",
                    (
                        Call(
                            str(self.step),
                            "read_file",
                            json.dumps(
                                dict(
                                    path="source.txt",
                                    start_line=self.step,
                                    end_line=150,
                                )
                            ),
                        ),
                    ),
                    Reservation(0, Decimal(0)),
                )

            async def execute(self, call: Call, timeout: float) -> Message:
                return dict(recorded)

        compact, baseline = Replay(), Replay()
        broker = ReplayBroker()
        references = ResultReferences(broker, str(uuid4()), str(uuid4()))
        original: list[Message] = [
            dict(
                role="user", content="Pinned plan: inspect, calculate, write, verify."
            ),
            dict(
                role="assistant",
                content="Decision: preserve source values and the plan.",
            ),
        ]
        with patch.dict(os.environ, {"ATLAS_PROJECT_MODE": "1", "ATLAS_STABLE": "0"}):
            limits = Limits(
                tokens=None,
                profile=PROFILES[0],
                code_mode=True,
                tool_calls=60,
                model_calls=61,
                cost=Decimal(0),
            )
            for model, storage in ((baseline, None), (compact, references)):
                result = await run(
                    Query(question="Continue the plan", lang="en"),
                    model,
                    model,
                    "Never overwrite originals.",
                    limits,
                    history=copy.deepcopy(original),
                    references=storage,
                )
                self.assertEqual(result.state, "done", result.text)
                self.assertEqual(result.tool_calls, 60)
        self.assertLess(max(compact.curve), 16000)
        self.assertLess(compact.curve[-1], baseline.curve[-1] // 3)
        self.assertLess(
            compact.curve[-1] - compact.curve[-11],
            (baseline.curve[-1] - baseline.curve[-11]) // 4,
        )
        for capture in compact.captures:
            self.assertEqual(capture[0]["content"], "Never overwrite originals.")
            self.assertEqual(capture[1:3], original)
        self.assertEqual(sum(not r["cleared"] for r in broker.records), 4)
        reopened = await references.reopen(
            Call(
                "again",
                "reopen_result",
                json.dumps(
                    dict(id=broker.records[0]["id"], range=dict(start=0, end=100))
                ),
            )
        )
        self.assertEqual(
            reopened["content"], json.dumps(recorded, ensure_ascii=False)[:100]
        )
        report = os.environ.get("ATLAS_CONTEXT_REPORT")
        if report:
            Path(report).write_text(
                json.dumps(
                    dict(
                        mode="recorded-tool-replay",
                        model="scripted-test-control",
                        steps=60,
                        budget_per_step=16000,
                        baseline=baseline.curve,
                        cleared=compact.curve,
                    ),
                    indent=2,
                )
                + "\n"
            )
