"""Replay and verify the genuine bounded forty-turn scoped Chat proof, keylessly."""

from __future__ import annotations

import asyncio
from decimal import Decimal
import gzip
import json
from pathlib import Path
import sys
from typing import Any

from packages.context_usage import measure
from packages.session_context import transport_context
from packages.session_state import SessionState, merge
from services.orchestrator.loop import Limits, Message, Query, Reservation, Turn, run
from services.orchestrator.session_updates import parse_update
from services.orchestrator.session_context import assemble
from services.orchestrator.chat_schema import ChatMessage

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_recording import manifest_for_row, request_manifest


class RecordedAnswer:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.used = False

    def estimate(self, messages: list[Message]) -> Reservation:
        return Reservation(0, Decimal(0))

    async def complete(self, messages: list[Message], timeout: float) -> Turn:
        if self.used or messages != self.row["request"]["messages"]:
            raise AssertionError("recorded_answer_request_mismatch")
        self.used = True
        response = self.row["response"]
        if response["calls"]:
            raise AssertionError("unexpected_tools_in_chat_proof")
        return Turn(response["text"], usage=Reservation(0, Decimal(0)))


class NoTools:
    def estimate(self, call: Any) -> Reservation:
        raise AssertionError("unexpected_tool")

    async def execute(self, call: Any, timeout: float) -> Message:
        raise AssertionError("unexpected_tool")


async def verify(path: Path) -> dict[str, Any]:
    evidence = json.loads(gzip.decompress(path.read_bytes()))
    assert evidence["mode"] == "live"
    assert evidence["provider"] == "legacy-scaleway"
    assert [t["number"] for t in evidence["turns"]] == list(range(1, 41))
    assert len(evidence["state_rows"]) == 40
    assert Decimal(str(evidence["cost_eur"])) <= Decimal(evidence["budget_eur"]) <= 1
    ids = []
    for row in evidence["provider_rows"]:
        assert manifest_for_row(row) == request_manifest(row["kind"], row["request"])
        for event in row["response"]:
            if result := event["event"].get("result"):
                assert result["provider_id"]
                ids.append(result["provider_id"])
    assert len(ids) >= 80
    assert len(ids) == len(set(ids))
    answers = []
    counts = []
    compactions = []
    previous = SessionState.empty()
    constraint = "Ship only to France or Germany. Never ship outside these countries."
    for number in range(1, 41):
        rows = [r for r in evidence["model_rows"] if r["turn"] == number]
        answer_rows = [r for r in rows if not r["request"].get("response_format")]
        updates = [r for r in rows if r["request"].get("response_format")]
        assert len(answer_rows) == len(updates) == 1
        row = answer_rows[0]
        payload = row["request"]
        assert payload["profile"] == "atlas-qwen"
        assert constraint in json.dumps(payload["messages"])
        assert len(payload["messages"]) <= 22  # fixed layers plus eight verbatim pairs
        current = json.loads(payload["messages"][-1]["content"])
        if number > 1:
            context = evidence["state_rows"][number - 2]
            history, _ = assemble(
                context, ChatMessage(role="user", content=current["question"])
            )
            # Derive reductions from the canonical captured history, then prove
            # that the resulting payload is exactly what the live model received.
            assert [dict(role=m.role, content=m.text) for m in history[:-1]] == payload[
                "messages"
            ][1:-1]
            bounded = transport_context(context)
            if removal := bounded.get("history_compaction"):
                compactions.append(removal)
        result = await run(
            Query(question=current["question"], lang=current["lang"]),
            RecordedAnswer(row),
            NoTools(),
            str(payload["messages"][0]["content"]),
            # The production loop adds current-question scope for a public profile.
            limits=Limits(profile="atlas-qwen"),
            history=payload["messages"][1:-1],
        )
        assert result.state == "done"
        assert result.text == row["response"]["text"]
        public = evidence["turns"][number - 1]["events"]
        assert not any(e.get("error") for e in public)
        delivered = "".join(
            choice.get("delta", {}).get("content", "")
            for event in public
            for choice in event.get("choices", [])
        )
        assert delivered == result.text
        answers.append(result.text)
        counts.append(
            measure(payload["messages"], payload["tools"], None)["total_tokens"]
        )
        update = updates[0]
        assert manifest_for_row(update) == request_manifest(
            update["kind"], update["request"]
        )
        source = json.loads(update["request"]["messages"][-1]["content"])
        previous = merge(
            previous,
            parse_update(update["response"]),
            {t["id"] for t in source["completed_turns"]}
            | set(evidence["state_rows"][number - 1]["represented_turn_ids"]),
        )
        assert (
            previous.model_dump() == evidence["state_rows"][number - 1]["session_state"]
        )
        assert len(evidence["state_rows"][number - 1]["represented_turn_ids"]) == number
    final = answers[-1]
    normalized = final.replace(",", "").replace(" ", "")
    assert all(v in normalized for v in ("37.25", "48", "1788"))
    assert "Canada" in final and any(
        w in final.lower() for w in ("no", "cannot", "not permitted", "reject")
    )
    assert all(
        step.lower() in final.lower()
        for step in (
            "Inspect source",
            "Calculate allocation",
            "Write register",
            "Verify totals",
        )
    )
    assert len(compactions) >= 3
    assert all(c["turn_ids"] and c["tokens_removed"] > 0 for c in compactions)
    final_payload = [
        r
        for r in evidence["model_rows"]
        if r["turn"] == 40 and not r["request"].get("response_format")
    ][0]["request"]
    opening_fact = evidence["turns"][2]["request"]["messages"][0]["content"]
    assert not any(opening_fact == m.get("content") for m in final_payload["messages"])
    assert "37.25" in json.dumps(evidence["state_rows"][-1]["session_state"])
    return dict(
        mode="recorded",
        turns=40,
        provider_completion_ids=len(ids),
        compactions=len(compactions),
        prompt_tokens_min=min(counts),
        prompt_tokens_max=max(counts),
        cost_eur=evidence["cost_eur"],
        final_answer=final,
    )


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify(Path(sys.argv[1]))), indent=2))
