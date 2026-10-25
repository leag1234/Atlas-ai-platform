"""P1a result bodies stay inside canonical scoped turns and their source policy."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb

from packages import tool_results
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_ledger import BYTE_KEYS, append
from services.retrieval.project_transcript import Transcript
from services.retrieval.project_work import Work


class ResultReferences:
    def __init__(self, transcript: Transcript) -> None:
        self.transcript = transcript

    def bound(self, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        turn, private = self.transcript.bound(data)
        if turn["state"] != "running":
            raise ProjectError(409, "session_busy")
        session = private["request"].get("work_session_id")
        if session:
            Work(self.transcript.scope).fence(
                dict(
                    session_id=session,
                    request_id=turn["request_id"],
                    generation=data["generation"],
                )
            )
        elif data["generation"] is not None:
            raise ProjectError(422, "invalid_request")
        return turn, private

    def put(self, data: dict[str, Any]) -> dict[str, Any]:
        t = self.transcript
        turn, private = self.bound(data)
        if data["id"].split(":")[0] != turn["id"]:
            raise ProjectError(404, "not_found")
        measured = len(data["content"].encode())
        if measured > tool_results.MAX_CONTENT_BYTES:
            raise ProjectError(
                413,
                "size_limit",
                measurements=[
                    dict(
                        measured=measured,
                        limit=tool_results.MAX_CONTENT_BYTES,
                        unit="bytes",
                    )
                ],
            )
        records = private.setdefault("tool_results", [])
        if any(r["id"] == data["id"] for r in records):
            raise ProjectError(409, "idempotency_conflict")
        reservation_id = str(uuid5(UUID(turn["id"]), "tool-result-content"))
        if len(records) >= 1000:
            raise ProjectError(
                413,
                "size_limit",
                measurements=[
                    dict(measured=len(records) + 1, limit=1000, unit="results")
                ],
            )
        old_bytes = private.get("tool_result_bytes", 0)
        if records:
            t.scope.reconcile(reservation_id, "stored_bytes", old_bytes + measured)
        else:
            t.scope.reserve(reservation_id, {"stored_bytes": measured})
        private["tool_result_bytes"] = old_bytes + measured
        records.append(
            dict(
                id=data["id"], tool=data["tool"], content=data["content"], cleared=False
            )
        )
        changes = tool_results.clear(records, tool_results.keep_results())
        cleared = {c["id"] for c in changes}
        for record in records:
            if record["id"] in cleared:
                record["cleared"] = True
        t.db.execute(
            "UPDATE atlas_projects.resources SET private=%s WHERE org_id=%s AND project_id=%s AND id=%s",
            (Jsonb(private), t.org, t.project, turn["id"]),
        )
        events = []
        if changes:
            event = append(
                t.scope,
                kind="context_selected",
                request_id=turn["request_id"],
                actor=dict(
                    kind="member", member_id=t.scope.identity.member, service=None
                ),
                resource_refs=[],
                source=dict(kind="server", ref=None),
                destination=dict(kind="server", ref=None),
                byte_counts={k: 0 for k in BYTE_KEYS},
                outcome="succeeded",
                conversation_id=turn["conversation_id"],
                session_id=private["request"].get("work_session_id"),
                transcript=True,
                context_clearing=dict(
                    ids=[c["id"] for c in changes],
                    tokens_removed=sum(c["tokens_removed"] for c in changes),
                    tokens_added=sum(c["tokens_added"] for c in changes),
                    method="utf8_bytes_div_4_ceil_per_component",
                ),
            )
            events.append(event)
        return dict(id=data["id"], cleared=changes, events=events)

    def open(self, data: dict[str, Any]) -> dict[str, Any]:
        t = self.transcript
        turn, private = self.bound(data)
        origin = data["id"].split(":")[0]
        source = t.files.get("turn", origin)
        source_private = t.files.row("turn", origin)["private"]
        if source["conversation_id"] != turn["conversation_id"] or source_private[
            "request"
        ].get("work_session_id") != private["request"].get("work_session_id"):
            raise ProjectError(404, "not_found")
        t.sources(source)
        record = next(
            (
                r
                for r in source_private.get("tool_results", [])
                if r["id"] == data["id"]
            ),
            None,
        )
        if record is None:
            raise ProjectError(404, "not_found")
        try:
            result = tool_results.reopen(record["content"], data["range"])
        except ValueError:
            raise ProjectError(422, "invalid_request") from None
        return dict(id=record["id"], tool=record["tool"], **result)
