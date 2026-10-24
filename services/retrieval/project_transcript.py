"""Canonical conversations and the approved private Chat transcript transaction."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from psycopg.types.json import Jsonb

from services.retrieval.project_documents import ScopedProjectDocuments
from services.retrieval.project_files import ProjectFiles
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_ledger import BYTE_KEYS, MODEL_KINDS, append
from services.retrieval.project_store import Scope
from services.retrieval.project_work import Work, empty_usage
from services.retrieval.project_work_contract import validate


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class Transcript:
    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self.files = ProjectFiles(scope)
        self.db, self.org, self.project = scope.db, scope.identity.org, scope.project_id

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        validate("ConversationCreate", data)
        value = dict(
            self.files.new(),
            **data,
            owner_member_id=self.scope.identity.member,
            last_action="chat",
            legacy_chat_id=None,
        )
        self.files.put("conversation", value)
        self.scope.event("conversation_created", self.project)
        return value

    def authorize(self, request: dict[str, Any]) -> None:
        if request["org_id"] != self.org or request["project_id"] != self.project:
            raise ProjectError(404, "not_found")
        self.files.get("conversation", request["conversation_id"])
        action = request.get("project_action", "chat")
        self.scope.authorize(
            "POST", ["conversations"] if action == "chat" else ["work-sessions"]
        )
        self.check_sources(request.get("file_version_ids", []))
        if action == "work":
            session = Work(self.scope).get(request["work_session_id"])
            if session["conversation_id"] != request["conversation_id"]:
                raise ProjectError(404, "not_found")
            if not set(request.get("file_version_ids", [])) <= {
                v["version_id"] for v in session["base_file_versions"]
            }:
                raise ProjectError(409, "selector_conflict")

    def check_sources(self, versions: list[str]) -> None:
        self.files.check_sources(versions)
        for identifier in versions:
            version = self.files.get("version", identifier)
            file = self.files.get("file", version["file_id"])
            Work(self.scope).folder(file["folder_id"])

    def sources(self, turn: dict[str, Any]) -> None:
        private = self.files.row("turn", turn["id"])["private"]
        self.check_sources(private["sources"])

    def turns(self, conversation: str) -> list[dict[str, Any]]:
        self.files.get("conversation", conversation)
        result = []
        for turn in self.files.listing("turn", conversation):
            try:
                self.sources(turn)
                result.append(turn)
            except ProjectError as exc:
                if exc.status not in {403, 404, 409}:
                    raise
        return result

    def context(
        self, conversation: str, query: str, selected: list[str]
    ) -> dict[str, Any]:
        self.files.get("conversation", conversation)
        self.check_sources(selected)
        instructions_file = self.files.get(
            "file", self.scope.project_data["instructions_file_id"]
        )
        self.check_sources([instructions_file["version_id"]])
        base = self.files.context()
        permitted_facts = []
        for fact in base["memory"]:
            try:
                self.check_sources(fact["source_file_version_ids"])
                permitted_facts.append(fact)
            except ProjectError as exc:
                if exc.status not in {403, 404, 409}:
                    raise
        base["memory"] = permitted_facts
        passages = ScopedProjectDocuments(self.files).sources()
        instructions = base["instructions"]
        passages = [
            p
            for p in passages
            if p["file_id"] != instructions["file_id"]
            and (not selected or p["version_id"] in selected)
        ]
        permitted_passages = []
        for passage in passages:
            try:
                self.check_sources([passage["version_id"]])
                permitted_passages.append(passage)
            except ProjectError as exc:
                if exc.status not in {403, 404, 409}:
                    raise
        passages = permitted_passages
        words = set(query.casefold().split())
        passages.sort(
            key=lambda p: (
                -len(words & set(p["text"].casefold().split())),
                p["chunk_id"],
            )
        )
        count = len(passages)
        passages = passages[:8]
        sources = [
            {k: p[k] for k in ("file_id", "version_id", "locator")} for p in passages
        ]
        history = []
        for turn in self.turns(conversation):
            private = self.files.row("turn", turn["id"])["private"]
            if (
                turn["state"] == "completed"
                and turn["context_revision"] == base["memory_revision"]
                and private["instructions_version"] == instructions["version"]
                and private["policy_revision"] == base["policy_revision"]
            ):
                history.append(turn)
        return dict(
            instructions_version=instructions["version"],
            instructions_text=instructions["text"],
            memory_revision=base["memory_revision"],
            facts=base["memory"][:8],
            passages=passages,
            history=history[-8:],
            context_manifest=dict(
                sources=sources,
                policy_revision=base["policy_revision"],
                excluded_versions=[
                    v for v in selected if v not in {p["version_id"] for p in passages}
                ],
                coverage=dict(
                    complete=count <= 8,
                    selected_units=len(passages),
                    total_units=count,
                    unit="paragraphs",
                    warnings=[],
                ),
            ),
        )

    def state(
        self, turn: dict[str, Any], context: dict[str, Any], replay: bool
    ) -> dict[str, Any]:
        rows = self.db.execute(
            "SELECT data FROM (SELECT seq,data FROM atlas_projects.transcript_progress WHERE org_id=%s AND project_id=%s AND turn_id=%s UNION ALL SELECT seq,data FROM atlas_projects.ledger_events WHERE org_id=%s AND project_id=%s AND data->>'request_id'=%s UNION ALL SELECT seq,data FROM atlas_projects.work_progress WHERE org_id=%s AND project_id=%s AND data->>'request_id'=%s) e ORDER BY seq LIMIT 100",
            (
                self.org,
                self.project,
                turn["id"],
                self.org,
                self.project,
                turn["request_id"],
                self.org,
                self.project,
                turn["request_id"],
            ),
        ).fetchall()
        events = [r["data"] for r in rows]
        return dict(
            turn=turn,
            context=context,
            replay=replay,
            events=events,
            last_event_seq=max((e["seq"] for e in events), default=0),
        )

    def begin(self, data: dict[str, Any], key: str) -> tuple[int, Any]:
        request = dict(data["request"])
        request.setdefault("project_action", "chat")
        request.setdefault("file_version_ids", [])
        self.authorize(request)
        body = canonical(request)
        path = f"/internal/atlas/transcript/begin/{self.project}"
        prior = self.scope.replay("POST", path, key, body)
        if prior:
            turn = self.files.get("turn", prior[1]["id"])
            self.sources(turn)
            context = self.context(
                request["conversation_id"],
                request["messages"][0]["content"],
                request["file_version_ids"],
            )
            return 200, self.state(turn, context, True)
        context = self.context(
            request["conversation_id"],
            request["messages"][0]["content"],
            request["file_version_ids"],
        )
        turn = dict(
            self.files.new(data["request_id"]),
            conversation_id=request["conversation_id"],
            action=request["project_action"],
            request_id=data["request_id"],
            content_refs=[
                dict(
                    kind="question",
                    text=request["messages"][0]["content"],
                    file_version_id=None,
                )
            ],
            file_version_ids=request["file_version_ids"],
            context_revision=context["memory_revision"],
            state="running",
            usage=empty_usage(),
            persistability="content",
        )
        sources = (
            set(request["file_version_ids"])
            | {p["version_id"] for p in context["passages"]}
            | {context["instructions_version"]}
        )
        if turn["action"] == "work":
            session = Work(self.scope).get(request["work_session_id"])
            sources.update(v["version_id"] for v in session["base_file_versions"])
        self.check_sources(sorted(sources))
        for fact in context["facts"]:
            sources.update(fact["source_file_version_ids"])
        for historical in context["history"]:
            sources.update(
                self.files.row("turn", historical["id"])["private"]["sources"]
            )
        reservation = min(0.10, self.scope.policy["quotas"]["per_turn"]["cost_eur"])
        private = dict(
            request=request,
            sources=sorted(sources),
            instructions_version=context["instructions_version"],
            policy_revision=self.scope.org["revision"],
            reservation=reservation,
            finish_digest=None,
        )
        self.files.put("turn", turn, request["conversation_id"], private=private)
        if turn["action"] == "chat":
            self.scope.reserve(
                turn["id"],
                dict(daily_model_eur=reservation, monthly_model_eur=reservation),
            )
            turn["usage"]["reserved_eur"] = reservation
            self.files.save(turn)
        conversation = self.files.get("conversation", request["conversation_id"])
        conversation["last_action"] = turn["action"]
        self.files.save(conversation)
        self.audit(
            turn,
            "context_selected",
            "succeeded",
            context["context_manifest"]["sources"],
        )
        self.scope.remember("POST", path, key, body, (201, {"id": turn["id"]}))
        return 201, self.state(turn, context, False)

    def bound(self, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        turn = self.files.get("turn", data["request_id"])
        if (
            turn["conversation_id"] != data["conversation_id"]
            or turn["created_by_member_id"] != self.scope.identity.member
        ):
            raise ProjectError(404, "not_found")
        private = self.files.row("turn", turn["id"])["private"]
        self.authorize(private["request"])
        self.sources(turn)
        return turn, private

    def audit(
        self, turn: dict[str, Any], kind: str, outcome: str, refs: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return append(
            self.scope,
            kind=kind,
            request_id=turn["request_id"],
            actor=dict(
                kind="member", member_id=self.scope.identity.member, service=None
            ),
            resource_refs=refs,
            source=dict(kind="server", ref=None),
            destination=dict(kind="server", ref=None),
            byte_counts={k: 0 for k in BYTE_KEYS},
            outcome=outcome,
            conversation_id=turn["conversation_id"],
            transcript=True,
        )

    def progress(self, turn: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT nextval(pg_get_serial_sequence('atlas_projects.ledger_events','seq')) AS seq"
        ).fetchone()
        assert row is not None
        event = dict(
            event,
            seq=row["seq"],
            org_id=self.org,
            project_id=self.project,
            request_id=turn["request_id"],
            turn_id=turn["id"],
        )
        self.db.execute(
            "INSERT INTO atlas_projects.transcript_progress VALUES(%s,%s,%s,%s,%s)",
            (self.org, self.project, turn["id"], event["seq"], Jsonb(event)),
        )
        return event

    def finish(self, data: dict[str, Any]) -> dict[str, Any]:
        turn, private = self.bound(data)
        digest = hashlib.sha256(canonical(data)).hexdigest()
        if private["finish_digest"]:
            if private["finish_digest"] != digest:
                raise ProjectError(409, "idempotency_conflict")
            return turn
        if turn["state"] != "running":
            raise ProjectError(409, "idempotency_conflict")
        for ref in data["content_refs"]:
            if ref["kind"] == "question":
                raise ProjectError(422, "invalid_request")
            if ref["file_version_id"]:
                self.check_sources([ref["file_version_id"]])
                private["sources"].append(ref["file_version_id"])
        usage = dict(data["usage"])
        if turn["action"] == "chat":
            unknown = (
                data["state"] != "completed"
                or usage["unknown_eur"] > 0
                or usage["reserved_eur"] > 0
            )
            for resource in ("daily_model_eur", "monthly_model_eur"):
                self.scope.reconcile(
                    turn["id"], resource, None if unknown else usage["cost_eur"]
                )
            usage["reserved_eur"] = 0
            usage["unknown_eur"] = private["reservation"] if unknown else 0
        else:
            work = Work(self.scope)
            session_id = private["request"]["work_session_id"]
            lease = work.lease(session_id)
            bound = lease is not None and str(lease["request_id"]) == turn["request_id"]
            if data["state"] == "completed" and (
                not bound or not lease or not lease["data"].get("released")
            ):
                raise ProjectError(409, "session_busy")
            if bound:
                usage = dict(work.get(session_id)["usage"])
        stale = (
            self.scope.project_data["memory_revision"] != turn["context_revision"]
            or self.files.instructions()["version"] != private["instructions_version"]
            or self.scope.org["revision"] != private["policy_revision"]
        )
        turn.update(
            state="stale" if stale else data["state"],
            usage=usage,
            content_refs=turn["content_refs"] + data["content_refs"],
        )
        self.files.save(turn)
        private["finish_digest"] = digest
        self.db.execute(
            "UPDATE atlas_projects.resources SET private=%s WHERE org_id=%s AND project_id=%s AND id=%s",
            (Jsonb(private), self.org, self.project, turn["id"]),
        )
        self.progress(
            turn,
            dict(
                version=1,
                session_id=private["request"].get("work_session_id"),
                type="recap" if data["state"] == "completed" else "error",
                status="completed" if data["state"] == "completed" else "failed",
                label="Turn " + data["state"],
                tool=None,
                plan=[],
                approval_id=None,
                job_id=None,
                text=None,
                error=data["error"],
                output={},
            ),
        )
        self.scope.event("transcript_" + data["state"], self.project)
        self.audit(
            turn,
            "context_selected",
            "succeeded" if data["state"] == "completed" else "failed",
            [],
        )
        return turn

    def events(self, data: dict[str, Any]) -> dict[str, Any]:
        turn, private = self.bound(data)
        if turn["state"] != "running" or turn["action"] != "chat":
            raise ProjectError(409, "session_busy")
        events = []
        for event in data["events"]:
            if (
                event["org_id"] != self.org
                or event["project_id"] != self.project
                or event["request_id"] != turn["request_id"]
                or event["session_id"] is not None
                or event["approval_id"] is not None
            ):
                raise ProjectError(404, "not_found")
            if "kind" in event:
                if (
                    event["kind"] not in MODEL_KINDS
                    or event["conversation_id"] != turn["conversation_id"]
                    or event["audit_record"] is not None
                ):
                    raise ProjectError(422, "invalid_request")
                if event["actor"] != dict(
                    kind="member", member_id=self.scope.identity.member, service=None
                ):
                    raise ProjectError(404, "not_found")
                model = event["model_dispatch"]
                if (
                    model is None
                    or model["instructions_version"] != private["instructions_version"]
                    or model["memory_revision"] != turn["context_revision"]
                ):
                    raise ProjectError(422, "invalid_request")
                for source in event["resource_refs"] + model["sources"]:
                    version = self.files.get("version", source["version_id"])
                    if (
                        source["version_id"] not in private["sources"]
                        or version["file_id"] != source["file_id"]
                    ):
                        raise ProjectError(404, "not_found")
                fields = {
                    k: event[k]
                    for k in (
                        "kind",
                        "request_id",
                        "actor",
                        "resource_refs",
                        "source",
                        "destination",
                        "byte_counts",
                        "outcome",
                        "conversation_id",
                        "model_dispatch",
                        "event_id",
                    )
                }
                events.append(append(self.scope, **fields, transcript=True))
            else:
                if (
                    event["turn_id"] not in (None, turn["id"])
                    or event["type"] not in {"error", "recap", "limit", "honesty"}
                    or event["tool"] is not None
                    or event["job_id"] is not None
                    or event["plan"]
                ):
                    raise ProjectError(422, "invalid_request")
                events.append(self.progress(turn, event))
        return dict(events=events)
