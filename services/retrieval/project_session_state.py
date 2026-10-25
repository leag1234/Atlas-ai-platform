"""Retrieval-owned session snapshots, source checks and compare-and-swap writes."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb

from packages.context_usage import encoded
from packages.session_state import SessionState, StateUpdate, merge
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_transcript import Transcript
from services.retrieval.project_work import Work


class SessionStates:
    def __init__(self, transcript: Transcript) -> None:
        self.t = transcript

    def eligible(self, conversation: str, session: str | None) -> list[dict[str, Any]]:
        t = self.t
        instructions = t.files.instructions()["version"]
        result = []
        for turn in t.turns(conversation):
            private = t.files.row("turn", turn["id"])["private"]
            if (
                turn["state"] == "completed"
                and turn["context_revision"] == t.scope.project_data["memory_revision"]
                and private["instructions_version"] == instructions
                and private["policy_revision"] == t.scope.org["revision"]
            ):
                result.append(turn)
        return result

    def snapshot(
        self, turns: list[dict[str, Any]], session: str | None
    ) -> dict[str, Any]:
        eligible = {t["id"] for t in turns}
        for turn in reversed(turns):
            private = self.t.files.row("turn", turn["id"])["private"]
            snapshot = private.get("session_states", {}).get(session or "conversation")
            if snapshot and set(snapshot["represented_turn_ids"]) <= eligible:
                SessionState.model_validate(snapshot["session_state"])
                return dict(snapshot, previous_turn_id=turn["id"])
        return dict(
            session_state=SessionState.empty().model_dump(),
            represented_turn_ids=[],
            previous_turn_id=None,
        )

    def read(self, data: dict[str, Any]) -> dict[str, Any]:
        t = self.t
        t.db.execute(
            "SELECT id FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND id=%s FOR UPDATE",
            (t.org, t.project, data["conversation_id"]),
        )
        turn, private = t.bound(data)
        if turn["state"] != "completed":
            raise ProjectError(409, "session_busy")
        session = private["request"].get("work_session_id")
        turns = self.eligible(turn["conversation_id"], session)
        # A late updater must never represent turns newer than its own terminal turn.
        position = next(
            (i for i, value in enumerate(turns) if value["id"] == turn["id"]), None
        )
        if position is None:
            raise ProjectError(
                409,
                "revision_conflict",
                "Project memory changed before the session update",
            )
        turns = turns[: position + 1]
        snapshot = self.snapshot(turns, session)
        if turn["id"] not in snapshot["represented_turn_ids"] and not private.get(
            "state_reservation"
        ):
            ceiling = t.scope.policy["quotas"]["per_turn"]["cost_eur"]
            if turn["action"] == "chat":
                ceiling = min(ceiling, private["reservation"])
            cap = min(
                0.01,
                max(
                    0,
                    ceiling - turn["usage"]["cost_eur"] - turn["usage"]["unknown_eur"],
                ),
            )
            reservation = str(uuid5(UUID(turn["id"]), "session-state-model"))
            t.scope.reserve(
                reservation, dict(daily_model_eur=cap, monthly_model_eur=cap)
            )
            private["state_reservation"] = dict(id=reservation, cap=cap)
            turn["usage"]["unknown_eur"] += cap
            t.files.save(turn)
            t.db.execute(
                "UPDATE atlas_projects.resources SET private=%s WHERE org_id=%s AND project_id=%s AND id=%s",
                (Jsonb(private), t.org, t.project, turn["id"]),
            )
        covered = set(snapshot["represented_turn_ids"])
        evidence = []
        for value in turns:
            if value["id"] in covered:
                continue
            source = t.files.row("turn", value["id"])["private"]
            evidence.append(
                dict(
                    turn_id=value["id"],
                    tool_results=[
                        dict(
                            id=r["id"],
                            tool=r["tool"],
                            content=r["content"][:4000],
                            truncated=len(r["content"]) > 4000,
                        )
                        for r in source.get("tool_results", [])[-16:]
                    ],
                )
            )

        return dict(
            snapshot,
            turns=[v for v in turns if v["id"] not in covered],
            session_id=session,
            budget_eur=private.get("state_reservation", {}).get("cap", 0),
            evidence=evidence,
            work_plan=Work(t.scope).get(session)["plan"] if session else [],
        )

    def write(self, data: dict[str, Any]) -> dict[str, Any]:
        t = self.t
        # Serialize state writers independently of their different turn rows.
        t.db.execute(
            "SELECT id FROM atlas_projects.resources WHERE org_id=%s AND project_id=%s AND id=%s FOR UPDATE",
            (t.org, t.project, data["conversation_id"]),
        )
        turn, private = t.bound(data)
        current = self.read(data)
        all_turns = self.eligible(turn["conversation_id"], current["session_id"])
        latest = self.snapshot(all_turns, current["session_id"])
        if latest["previous_turn_id"] != data["previous_turn_id"]:
            raise ProjectError(409, "revision_conflict")
        expected = current["represented_turn_ids"] + [v["id"] for v in current["turns"]]
        if expected != data["represented_turn_ids"]:
            raise ProjectError(409, "revision_conflict")
        try:
            state = merge(
                SessionState.model_validate(current["session_state"]),
                StateUpdate.model_validate(data["update"]),
                set(expected),
            )
        except ValueError:
            raise ProjectError(
                422, "invalid_request", "Invalid session state or provenance"
            ) from None
        snapshot = dict(session_state=state.model_dump(), represented_turn_ids=expected)
        private = t.files.row("turn", turn["id"])["private"]
        reservation = private["state_reservation"]
        usage = data["usage"]
        if usage["cost_eur"] > reservation["cap"]:
            raise ProjectError(429, "budget_exceeded")
        for resource in ("daily_model_eur", "monthly_model_eur"):
            t.scope.reconcile(reservation["id"], resource, usage["cost_eur"])
        turn = t.files.get("turn", turn["id"])
        turn["usage"]["unknown_eur"] = max(
            0, turn["usage"]["unknown_eur"] - reservation["cap"]
        )
        for name in ("cost_eur", "model_calls", "input_tokens", "output_tokens"):
            turn["usage"][name] += usage[name]
        t.files.save(turn)
        snapshots = private.setdefault("session_states", {})
        key = current["session_id"] or "conversation"
        if key in snapshots:
            raise ProjectError(409, "revision_conflict")
        snapshots[key] = snapshot
        snapshots["conversation"] = snapshot
        measured = len(encoded(snapshots).encode())
        reservation = str(uuid5(UUID(turn["id"]), "session-state"))
        t.scope.reserve(reservation, {"stored_bytes": measured})
        t.db.execute(
            "UPDATE atlas_projects.resources SET private=%s WHERE org_id=%s AND project_id=%s AND id=%s",
            (Jsonb(private), t.org, t.project, turn["id"]),
        )
        t.audit(turn, "context_selected", "succeeded", [])
        return snapshot

    def compact(self, data: dict[str, Any]) -> dict[str, Any]:
        from services.retrieval.project_ledger import BYTE_KEYS, append

        t = self.t
        turn, private = t.bound(data)
        if turn["state"] != "running":
            raise ProjectError(409, "session_busy")
        session = private["request"].get("work_session_id")
        snapshot = self.snapshot(
            self.eligible(turn["conversation_id"], session), session
        )
        if not set(data["compaction"]["turn_ids"]) <= set(
            snapshot["represented_turn_ids"]
        ):
            raise ProjectError(409, "revision_conflict")
        event = append(
            t.scope,
            kind="context_selected",
            request_id=turn["id"],
            actor=dict(kind="member", member_id=t.scope.identity.member, service=None),
            resource_refs=[],
            source=dict(kind="server", ref=None),
            destination=dict(kind="server", ref=None),
            byte_counts={k: 0 for k in BYTE_KEYS},
            outcome="succeeded",
            conversation_id=turn["conversation_id"],
            session_id=session,
            transcript=True,
            context_compaction=data["compaction"],
        )
        return dict(events=[event])
