"""REQ-PROJ-06 append-only project ledger and transactional outbox."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Mapping
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from services.retrieval.project_identity import ProjectError

if TYPE_CHECKING:
    from services.retrieval.project_store import Scope

Json = dict[str, Any]

BYTE_KEYS = frozenset(
    {
        "source_bytes",
        "selected_utf8_bytes",
        "image_bytes",
        "serialized_payload_bytes",
        "response_bytes",
    }
)
MODEL_KINDS = frozenset(
    {
        "model_dispatch_intent",
        "model_sent",
        "model_acknowledged",
        "model_failed",
        "model_unknown",
    }
)
KINDS = frozenset(
    {
        "folder_registered",
        "locality_changed",
        "file_uploaded",
        "file_versioned",
        "file_read",
        "file_moved",
        "file_deleted",
        "file_purged",
        "extraction_completed",
        "context_selected",
        "memory_changed",
        "location_used",
        *MODEL_KINDS,
        "approval_requested",
        "approval_decided",
        "tool_started",
        "tool_completed",
        "tool_failed",
        "deliverable_created",
        "deliverable_downloaded",
        "sync_conflict",
        "retention_applied",
        "administrative_access",
        "repository_action",
        "runner_code_egress_approved",
        "runner_code_egress_revoked",
        "runner_session_opened",
        "runner_session_closed",
        "runner_command_requested",
        "runner_command_approved",
        "runner_command_denied",
        "runner_command_started",
        "runner_command_completed",
        "runner_command_failed",
        "runner_command_cancelled",
        "runner_command_unknown",
        "runner_file_fetched",
    }
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Json) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError):
        raise ProjectError(422, "invalid_request") from None


def _bytes(value: Mapping[str, Any]) -> Json:
    if set(value) != BYTE_KEYS or any(
        type(value[key]) is not int or value[key] < 0 for key in BYTE_KEYS
    ):
        raise ProjectError(422, "invalid_request")
    return dict(value)


def _endpoint(value: Mapping[str, Any]) -> Json:
    if set(value) != {"kind", "ref"} or value["kind"] not in {
        "server",
        "device",
        "model",
        "connector",
        "none",
    }:
        raise ProjectError(422, "invalid_request")
    if value["ref"] is not None and (
        not isinstance(value["ref"], str) or not value["ref"]
    ):
        raise ProjectError(422, "invalid_request")
    return dict(value)


def _refs(values: Iterable[Mapping[str, Any]]) -> list[Json]:
    result: list[Json] = []
    for value in values:
        if set(value) != {"file_id", "version_id", "locator"}:
            raise ProjectError(422, "invalid_request")
        result.append(
            {
                "file_id": _uuid(str(value["file_id"])),
                "version_id": _uuid(str(value["version_id"])),
                "locator": value["locator"],
            }
        )
    return result


def _model(value: Mapping[str, Any] | None, kind: str, counts: Json) -> Json | None:
    if kind in MODEL_KINDS and value is None:
        raise ProjectError(422, "invalid_request")
    if value is None:
        return None
    required = {
        "call_id",
        "attempt",
        "provider_ref",
        "purpose",
        "sources",
        "instructions_version",
        "memory_revision",
        "byte_counts",
        "input_tokens",
        "output_tokens",
        "tokens_estimated",
        "transmission",
    }
    if (
        not required <= set(value)
        or set(value) - required - {"context"}
        or value["purpose"]
        not in {"generation", "embedding", "rerank", "ocr", "vision", "session_state"}
        or value["transmission"]
        not in {"not_sent", "sent", "acknowledged", "possibly_sent"}
    ):
        raise ProjectError(422, "invalid_request")
    if (
        type(value["attempt"]) is not int
        or value["attempt"] < 1
        or type(value["memory_revision"]) is not int
        or value["memory_revision"] < 0
    ):
        raise ProjectError(422, "invalid_request")
    if (
        any(
            type(value[key]) is not int or value[key] < 0
            for key in ("input_tokens", "output_tokens")
        )
        or type(value["tokens_estimated"]) is not bool
    ):
        raise ProjectError(422, "invalid_request")
    if not isinstance(value["provider_ref"], str) or not value["provider_ref"]:
        raise ProjectError(422, "invalid_request")
    if "context" in value:
        from services.retrieval.project_work_contract import validate

        validate("ContextUsage", value["context"])
    model_counts = _bytes(value["byte_counts"])
    if model_counts != counts:
        raise ProjectError(422, "invalid_request")
    return {
        **value,
        "call_id": _uuid(str(value["call_id"])),
        "instructions_version": _uuid(str(value["instructions_version"])),
        "sources": _refs(value["sources"]),
        "byte_counts": model_counts,
    }


def append(
    scope: Scope,
    *,
    kind: str,
    request_id: str,
    actor: Mapping[str, Any],
    resource_refs: Iterable[Mapping[str, Any]],
    source: Mapping[str, Any],
    destination: Mapping[str, Any],
    byte_counts: Mapping[str, Any],
    outcome: str,
    conversation_id: str | None = None,
    session_id: str | None = None,
    approval_id: str | None = None,
    model_dispatch: Mapping[str, Any] | None = None,
    context_clearing: Mapping[str, Any] | None = None,
    context_compaction: Mapping[str, Any] | None = None,
    runner: Mapping[str, Any] | None = None,
    memory_change: Mapping[str, Any] | None = None,
    audit_record: Mapping[str, Any] | None = None,
    event_id: str | None = None,
    timestamp: str | None = None,
    cleanup: bool = False,
    transcript: bool = False,
    connector: bool = False,
) -> Json:
    """Append once and queue the exact payload in the caller's authorized transaction."""
    if scope.project is None:
        raise ProjectError(404, "not_found")
    if kind not in KINDS or outcome not in {
        "intent",
        "succeeded",
        "failed",
        "denied",
        "unknown",
    }:
        raise ProjectError(422, "invalid_request")
    if connector:
        if (
            kind not in {"approval_requested", "approval_decided", "tool_completed"}
            or not conversation_id
            or source.get("kind") != "connector"
        ):
            raise ProjectError(403, "permission_denied")
        scope.authorize("POST", ["conversations"])
    elif transcript:
        if (
            kind not in MODEL_KINDS | {"context_selected", "location_used"}
            or not conversation_id
        ):
            raise ProjectError(403, "permission_denied")
        row = scope.db.execute(
            "SELECT data FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND kind='turn' AND data->>'request_id'=%s",
            (scope.identity.org, scope.project_id, request_id),
        ).fetchone()
        if (
            row is None
            or row["data"]["created_by_member_id"] != scope.identity.member
            or row["data"]["conversation_id"] != conversation_id
        ):
            raise ProjectError(404, "not_found")
        scope.authorize(
            "POST",
            ["conversations"] if row["data"]["action"] == "chat" else ["work-sessions"],
        )
    elif cleanup:
        # Internal cleanup records survive archival; no public write permission
        # changes. Only the broker's terminal approval audit uses this path.
        if (
            scope.role not in {"owner", "editor"}
            or kind != "approval_decided"
            or audit_record is None
            or audit_record.get("phase") != "outcome"
        ):
            raise ProjectError(403, "permission_denied")
    else:
        scope.authorize(
            "GET" if kind in {"file_read", "deliverable_downloaded"} else "POST",
            ["ledger"],
        )
    if set(actor) != {"kind", "member_id", "service"} or actor["kind"] not in {
        "member",
        "service",
    }:
        raise ProjectError(422, "invalid_request")
    member_id = None if actor["member_id"] is None else _uuid(str(actor["member_id"]))
    service = actor["service"]
    if (actor["kind"] == "member") != (member_id is not None) or (
        actor["kind"] == "service"
    ) != (isinstance(service, str) and bool(service)):
        raise ProjectError(422, "invalid_request")
    counts = _bytes(byte_counts)
    event = {
        "org_id": scope.identity.org,
        "project_id": str(scope.project["id"]),
        "seq": 0,
        "event_id": _uuid(event_id) if event_id else str(uuid4()),
        "timestamp": timestamp or _timestamp(),
        "actor": {"kind": actor["kind"], "member_id": member_id, "service": service},
        "conversation_id": _uuid(conversation_id) if conversation_id else None,
        "session_id": _uuid(session_id) if session_id else None,
        "request_id": _uuid(request_id),
        "kind": kind,
        "resource_refs": _refs(resource_refs),
        "source": _endpoint(source),
        "destination": _endpoint(destination),
        "byte_counts": counts,
        "outcome": outcome,
        "approval_id": _uuid(approval_id) if approval_id else None,
        "policy_revision": int(scope.org["revision"]),
        "previous_digest": None,
        "digest": "",
        "model_dispatch": _model(model_dispatch, kind, counts),
        "audit_record": dict(audit_record) if audit_record is not None else None,
    }
    if context_clearing is not None:
        from services.retrieval.project_work_contract import validate

        validate("ContextClearing", context_clearing)
        if kind != "context_selected":
            raise ProjectError(422, "invalid_request")
        event["context_clearing"] = dict(context_clearing)
    if runner is not None:
        from services.retrieval.project_work_contract import validate

        validate("RunnerAudit", runner)
        if not kind.startswith("runner_"):
            raise ProjectError(422, "invalid_request")
        event["runner"] = dict(runner)
    if memory_change is not None:
        if kind != "memory_changed" or set(memory_change) != {
            "id",
            "revision",
            "status",
            "supersedes",
        }:
            raise ProjectError(422, "invalid_request")
        event["memory_change"] = dict(memory_change)
    if context_compaction is not None:
        from services.retrieval.project_work_contract import validate

        validate("ContextCompaction", context_compaction)
        if kind != "context_selected":
            raise ProjectError(422, "invalid_request")
        event["context_compaction"] = dict(context_compaction)
    existing = scope.db.execute(
        "SELECT data FROM atlas_projects.ledger_events WHERE org_id=%s AND project_id=%s AND event_id=%s",
        (scope.identity.org, event["project_id"], event["event_id"]),
    ).fetchone()
    if existing is not None:
        return dict(existing["data"])
    previous = scope.db.execute(
        "SELECT data->>'digest' AS digest FROM atlas_projects.ledger_events WHERE org_id=%s AND project_id=%s ORDER BY seq DESC LIMIT 1",
        (scope.identity.org, event["project_id"]),
    ).fetchone()
    event["previous_digest"] = previous["digest"] if previous else None
    row = scope.db.execute(
        "SELECT nextval(pg_get_serial_sequence('atlas_projects.ledger_events','seq')) AS seq"
    ).fetchone()
    assert row is not None
    event["seq"] = row["seq"]
    digest_input = dict(event)
    digest_input.pop("digest")
    event["digest"] = hashlib.sha256(_canonical(digest_input)).hexdigest()
    scope.db.execute(
        "INSERT INTO atlas_projects.ledger_events(org_id,project_id,seq,event_id,data) OVERRIDING SYSTEM VALUE VALUES(%s,%s,%s,%s,%s)",
        (
            scope.identity.org,
            event["project_id"],
            event["seq"],
            event["event_id"],
            Jsonb(event),
        ),
    )
    scope.db.execute(
        "INSERT INTO atlas_projects.outbox(org_id,project_id,event_id,state) VALUES(%s,%s,%s,'pending')",
        (scope.identity.org, event["project_id"], event["event_id"]),
    )
    return event


def claim_outbox(scope: Scope, limit: int = 50) -> list[Json]:
    if scope.project is None or limit < 1 or limit > 100:
        raise ProjectError(422, "invalid_request")
    scope.authorize("POST", ["ledger"])
    rows = scope.db.execute(
        """WITH claimed AS (SELECT event_id FROM atlas_projects.outbox WHERE org_id=%s AND project_id=%s AND state='pending' AND available_at<=now() ORDER BY available_at,event_id FOR UPDATE SKIP LOCKED LIMIT %s) UPDATE atlas_projects.outbox o SET state='publishing',attempts=o.attempts+1 FROM claimed WHERE o.org_id=%s AND o.project_id=%s AND o.event_id=claimed.event_id RETURNING o.event_id""",
        (
            scope.identity.org,
            str(scope.project["id"]),
            limit,
            scope.identity.org,
            str(scope.project["id"]),
        ),
    ).fetchall()
    events: list[Json] = []
    for row in rows:
        event = scope.db.execute(
            "SELECT data FROM atlas_projects.ledger_events WHERE org_id=%s AND project_id=%s AND event_id=%s",
            (scope.identity.org, str(scope.project["id"]), row["event_id"]),
        ).fetchone()
        assert event is not None
        events.append(dict(event["data"]))
    return events


def settle_outbox(scope: Scope, event_id: str, published: bool) -> None:
    if scope.project is None:
        raise ProjectError(404, "not_found")
    scope.authorize("POST", ["ledger"])
    status = scope.db.execute(
        "UPDATE atlas_projects.outbox SET state=%s,published_at=CASE WHEN %s THEN now() ELSE NULL END,available_at=CASE WHEN %s THEN available_at ELSE now() END WHERE org_id=%s AND project_id=%s AND event_id=%s AND state='publishing'",
        (
            "published" if published else "pending",
            published,
            published,
            scope.identity.org,
            str(scope.project["id"]),
            _uuid(event_id),
        ),
    )
    if status.rowcount != 1:
        raise ProjectError(404, "not_found")


def summary(
    events: Iterable[Mapping[str, Any]],
    server_versions: Mapping[str, int],
    produced_versions: Mapping[str, int],
) -> Json:
    """Compute a snapshot without double-counting dispatch lifecycle events or versions."""
    model: dict[tuple[str, int], Json] = {}
    as_of = 0
    for event in events:
        as_of = max(as_of, int(event["seq"]))
        dispatch = event.get("model_dispatch")
        if dispatch is not None:
            model.setdefault(
                (str(dispatch["call_id"]), int(dispatch["attempt"])), dict(dispatch)
            )
    payload = sum(
        item["byte_counts"]["serialized_payload_bytes"] for item in model.values()
    )
    unique: dict[str, int] = {}
    for item in model.values():
        sources = item["sources"]
        if sources:
            each = item["byte_counts"]["source_bytes"] // len(sources)
            for source in sources:
                unique.setdefault(_canonical(source).decode(), each)
    return {
        "local": {"files": None, "bytes": None, "known": False, "as_of": None},
        "server": {
            "files": len(server_versions),
            "bytes": sum(server_versions.values()),
        },
        "model": {
            "payload_bytes": payload,
            "unique_source_bytes": sum(unique.values()),
            "calls": len(model),
            "possibly_sent_calls": sum(
                item["transmission"] == "possibly_sent" for item in model.values()
            ),
        },
        "produced": {
            "files": len(produced_versions),
            "bytes": sum(produced_versions.values()),
        },
        "as_of_seq": as_of,
    }
