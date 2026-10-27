"""Real retrieval transactions for source-aware session snapshots and pinned memory."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from pathlib import Path
import sys
import unittest
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from packages.session_state import SessionState
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_scoped_api import store
from services.retrieval.project_session_state import SessionStates
from services.retrieval.project_transcript import Transcript
from services.retrieval.project_work import empty_usage

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_project_work as work


class SessionStorageTests(unittest.TestCase):
    fixture: work.ProjectWorkHTTPTests

    @classmethod
    def setUpClass(cls) -> None:
        work.ProjectWorkHTTPTests.setUpClass()
        cls.addClassCleanup(work.ProjectWorkHTTPTests.doClassCleanups)

    def setUp(self) -> None:
        self.fixture = work.ProjectWorkHTTPTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_snapshot_merge_scope_failure_and_revision_invalidation(self) -> None:
        policy = deepcopy(self.fixture.policy)
        policy["quotas"]["stored_bytes"] = 1000000
        with psycopg.connect(self.fixture.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s", (Jsonb(policy),)
            )
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s WHERE org_id=%s",
                (Jsonb(policy), self.fixture.org),
            )
        with store(self.fixture.app_dsn).scope(
            self.fixture.identity("editor"), self.fixture.org, self.fixture.project
        ) as scope:
            transcript = Transcript(scope)
            conversation = transcript.create(dict(title="State test"))
            states = SessionStates(transcript)
            request: dict[str, Any] = dict(
                model="atlas",
                org_id=self.fixture.org,
                project_id=self.fixture.project,
                conversation_id=conversation["id"],
                project_action="chat",
                file_version_ids=[],
                messages=[dict(role="user", content="48 units cost EUR 37.25 each.")],
                stream=True,
            )
            identifier = str(uuid4())
            _, begun = transcript.begin(
                dict(request_id=identifier, request=request), identifier
            )
            binding = dict(request_id=identifier, conversation_id=conversation["id"])
            transcript.finish(
                dict(
                    binding,
                    state="completed",
                    content_refs=[
                        dict(
                            kind="answer",
                            text="Recorded 48 units at EUR 37.25.",
                            file_version_id=None,
                        )
                    ],
                    usage=empty_usage(),
                    error=None,
                )
            )
            old = states.read(binding)
            self.assertEqual(old["represented_turn_ids"], [])
            update = SessionState.empty().model_dump() | dict(
                resolved_questions=[],
                replace_plan=False,
                established_facts=[
                    dict(
                        id="allocation",
                        text="48 units; EUR 37.25/unit",
                        source="user",
                        turn_id=identifier,
                    )
                ],
            )
            payload = dict(
                binding,
                previous_turn_id=None,
                represented_turn_ids=[identifier],
                update=update,
                usage=empty_usage(),
            )
            written = states.write(payload)
            self.assertEqual(
                written["session_state"]["established_facts"][0]["text"],
                "48 units; EUR 37.25/unit",
            )
            context = transcript.context(conversation["id"], "Recall", [])
            self.assertEqual(context["represented_turn_ids"], [identifier])
            self.assertEqual(context["session_state"], written["session_state"])
            self.assertEqual(context["history"][0]["id"], begun["turn"]["id"])
            with self.assertRaises(ProjectError) as rejected:
                states.write(payload)
            self.assertEqual(
                rejected.exception.code
                if hasattr(rejected.exception, "code")
                else rejected.exception.status,
                409,
            )
            self.assertEqual(
                states.read(binding)["session_state"], written["session_state"]
            )
            scope.project_data["memory_revision"] += 1
            self.assertEqual(states.eligible(conversation["id"], None), [])

    def test_public_answer_then_background_state_transaction(self) -> None:
        import asyncio
        import json
        from unittest.mock import patch
        from services.orchestrator.model import GatewayModel
        from services.orchestrator.session_updates import tasks

        policy = deepcopy(self.fixture.policy)
        policy["quotas"]["stored_bytes"] = 1000000
        with psycopg.connect(self.fixture.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s", (Jsonb(policy),)
            )
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s WHERE org_id=%s",
                (Jsonb(policy), self.fixture.org),
            )
        conversation = self.fixture.request(
            "POST",
            self.fixture.project_path() + "/conversations",
            "editor",
            dict(title="Background boundary"),
        ).json()["id"]

        async def post(url: str, payload: Any, timeout: float) -> Any:
            if url.endswith("/agent/config"):
                return dict(
                    input_eur_per_mtok="0", output_eur_per_mtok="0", max_tokens=2048
                )
            if payload.get("response_format") == {"type": "json_object"}:
                source = json.loads(payload["messages"][-1]["content"])[
                    "completed_turns"
                ][0]["id"]
                update = SessionState.empty().model_dump() | dict(
                    resolved_questions=[],
                    replace_plan=False,
                    established_facts=[
                        dict(id="units", text="48 units", source="user", turn_id=source)
                    ],
                )
                return dict(
                    text="",
                    calls=[
                        dict(
                            id="update",
                            name="update_session_state",
                            arguments=json.dumps(update),
                        )
                    ],
                    usage=dict(prompt_tokens=30, completion_tokens=30),
                    cost_eur="0",
                )
            return dict(
                text=json.dumps(dict(answer="Recorded 48 units.", facts=[])),
                calls=[],
                usage=dict(prompt_tokens=30, completion_tokens=30),
                cost_eur="0",
            )

        with (
            self.fixture.browser,
            patch.object(GatewayModel, "post", staticmethod(post)),
        ):
            response = self.fixture.browser.post(
                "/v1/chat/completions",
                json=dict(
                    model="atlas-qwen",
                    org_id=self.fixture.org,
                    project_id=self.fixture.project,
                    conversation_id=conversation,
                    project_action="chat",
                    messages=[dict(role="user", content="48 units")],
                    stream=True,
                ),
                headers=self.fixture.front(),
            )
            self.assertEqual(response.status_code, 200)

            async def drain() -> None:
                if tasks:
                    await asyncio.gather(*list(tasks))

            assert self.fixture.browser.portal is not None
            self.fixture.browser.portal.call(drain)
        context = self.fixture.request(
            "POST",
            self.fixture.project_path() + "/context",
            "editor",
            dict(conversation_id=conversation, query="Recall", action="chat"),
        ).json()
        self.assertEqual(
            context["session_state"]["established_facts"][0]["text"], "48 units"
        )
        self.assertEqual(len(context["represented_turn_ids"]), 1)
