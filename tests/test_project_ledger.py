"""REQ-PROJ-06 ledger events, transactional outbox, and scoped accounting."""

from __future__ import annotations

import importlib
import unittest
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import psycopg

from services.retrieval.project_identity import ProjectError
from services.retrieval.project_ledger import (
    append,
    claim_outbox,
    settle_outbox,
    summary,
)
from services.retrieval.project_scoped_api import store

if TYPE_CHECKING:

    class ProjectIsolationTests(unittest.TestCase):
        app_dsn: str
        admin_dsn: str
        org: str
        project: str
        other_org: str
        foreign_project: str
        members: dict[str, str]
        validator: Any
        contract: dict[str, Any]

        def identity(self, role: str) -> Any: ...
else:
    ProjectIsolationTests = getattr(
        importlib.import_module("tests.test_project_isolation"), "ProjectIsolationTests"
    )


class ProjectLedgerTests(ProjectIsolationTests):
    def setUp(self) -> None:
        super().setUp()
        # These append/outbox unit scenarios require an empty chain; project
        # bootstrap events are exercised through HTTP in test_project_files.
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "TRUNCATE atlas_projects.outbox, atlas_projects.ledger_events RESTART IDENTITY"
            )

    def payload(self, event_id: str | None = None) -> dict[str, Any]:
        file_id, version_id = str(uuid4()), str(uuid4())
        return {
            "kind": "model_dispatch_intent",
            "request_id": str(uuid4()),
            "actor": {
                "kind": "member",
                "member_id": self.members["owner"],
                "service": None,
            },
            "resource_refs": [
                {"file_id": file_id, "version_id": version_id, "locator": None}
            ],
            "source": {"kind": "server", "ref": "project"},
            "destination": {"kind": "model", "ref": "dispatch"},
            "byte_counts": {
                "source_bytes": 19,
                "selected_utf8_bytes": 11,
                "image_bytes": 0,
                "serialized_payload_bytes": 31,
                "response_bytes": 0,
            },
            "outcome": "intent",
            "event_id": event_id,
            "model_dispatch": {
                "call_id": str(uuid4()),
                "attempt": 1,
                "provider_ref": "configured-profile",
                "purpose": "generation",
                "sources": [
                    {"file_id": file_id, "version_id": version_id, "locator": None}
                ],
                "instructions_version": str(uuid4()),
                "memory_revision": 0,
                "byte_counts": {
                    "source_bytes": 19,
                    "selected_utf8_bytes": 11,
                    "image_bytes": 0,
                    "serialized_payload_bytes": 31,
                    "response_bytes": 0,
                },
                "input_tokens": 3,
                "output_tokens": 0,
                "tokens_estimated": False,
                "transmission": "not_sent",
            },
        }

    def test_intent_is_idempotent_hash_chained_and_contract_shaped(self) -> None:
        payload = self.payload(str(uuid4()))
        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            first = append(scope, **payload)
            repeated = append(scope, **payload)
            self.assertEqual(first, repeated)
            self.assertEqual(first["seq"], 1)
            self.assertIsNone(first["previous_digest"])
            self.validator.validate(
                first, self.contract["components"]["schemas"]["LedgerEvent"]
            )
        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            second = append(scope, **self.payload())
            self.assertEqual(second["previous_digest"], first["digest"])
            self.assertEqual(second["seq"], 2)
            rows = scope.db.execute(
                "SELECT count(*) AS count FROM atlas_projects.outbox"
            ).fetchone()
            assert rows is not None
            self.assertEqual(rows["count"], 2)

    def test_retry_claim_does_not_duplicate_event_or_summary(self) -> None:
        payload = self.payload()
        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            event = append(scope, **payload)
            claimed = claim_outbox(scope)
            self.assertEqual(
                [item["event_id"] for item in claimed], [event["event_id"]]
            )
            settle_outbox(scope, event["event_id"], False)
            retry = claim_outbox(scope)
            self.assertEqual(retry, claimed)
            settle_outbox(scope, event["event_id"], True)
            self.assertEqual(claim_outbox(scope), [])
            result = summary(retry, {event["resource_refs"][0]["version_id"]: 19}, {})
            self.assertEqual(
                result["model"],
                {
                    "payload_bytes": 31,
                    "unique_source_bytes": 19,
                    "calls": 1,
                    "possibly_sent_calls": 0,
                },
            )

    def test_crash_rolls_back_intent_and_outbox_together(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "interrupt"):
            with store(self.app_dsn).scope(
                self.identity("owner"), self.org, self.project
            ) as scope:
                append(scope, **self.payload())
                raise RuntimeError("interrupt")
        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            events = scope.db.execute(
                "SELECT count(*) AS count FROM atlas_projects.ledger_events"
            ).fetchone()
            outbox = scope.db.execute(
                "SELECT count(*) AS count FROM atlas_projects.outbox"
            ).fetchone()
            assert events is not None and outbox is not None
            self.assertEqual((events["count"], outbox["count"]), (0, 0))

    def test_authorization_and_cross_organization_visibility(self) -> None:
        payload = self.payload()
        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            append(scope, **payload)
        with store(self.app_dsn).scope(
            self.identity("viewer"), self.org, self.project
        ) as scope:
            rows = scope.db.execute(
                "SELECT count(*) AS count FROM atlas_projects.ledger_events"
            ).fetchone()
            assert rows is not None
            self.assertEqual(rows["count"], 1)
            with self.assertRaises(ProjectError) as denied:
                append(scope, **self.payload())
            self.assertEqual(denied.exception.status, 403)
        with store(self.app_dsn).scope(
            self.identity("other"), self.other_org, self.foreign_project
        ) as scope:
            self.assertEqual(
                scope.db.execute(
                    "SELECT * FROM atlas_projects.ledger_events"
                ).fetchall(),
                [],
            )
