"""Opt-in 40-turn public HTTP capture using the existing genuine-stream recorder."""

from __future__ import annotations

import asyncio
import copy
import gzip
import json
import os
from contextlib import aclosing, ExitStack
from pathlib import Path
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))


def capture(destination: Path, count: int = 40, *, action: str = "chat") -> None:
    import psycopg
    from psycopg.types.json import Jsonb
    from fastapi import FastAPI
    from unittest.mock import patch
    from agent_provider import AgentProvider
    import provider_settings
    import stream_transport
    from provider_recording import capture_stream, request_manifest
    from test_project_work import ProjectWorkHTTPTests
    from test_project_auth import ProjectAuthTests
    from services.orchestrator.model import GatewayModel
    from services.orchestrator.session_updates import tasks
    from services.retrieval.project_scoped_api import store
    from services.retrieval.project_files import ProjectFiles

    preset = provider_settings.selected()
    if (
        os.environ.get("ATLAS_PROVIDER") != "hetzner"
        or preset is None
        or preset["price"] != dict(currency="EUR", input=0, output=0)
    ):
        raise ValueError("free_hetzner_preset_required")
    if not provider_settings.api_key():
        raise ValueError("provider_credential_required")
    destination.mkdir(parents=True, exist_ok=False)
    evidence: dict[str, Any] = dict(
        mode="live",
        provider="hetzner",
        turns=[],
        model_rows=[],
        provider_rows=[],
        state_rows=[],
        cost_eur=0,
    )
    secrets = [
        v
        for k, v in os.environ.items()
        if len(v) >= 8
        and any(w in k.upper() for w in ("KEY", "SECRET", "TOKEN", "PASSWORD"))
    ]

    def save() -> None:
        raw = json.dumps(evidence, ensure_ascii=False, default=str).encode()
        if any(v.encode() in raw for v in secrets):
            raise ValueError("secret_in_recording")
        (destination / "candidate.json.gz").write_bytes(gzip.compress(raw, mtime=0))

    original_stream = stream_transport.attempt
    original_post = GatewayModel.post
    last_dispatch = 0.0

    async def stream(request: Any, model: str, timeout: float) -> Any:
        nonlocal last_dispatch
        # Hetzner documents ten requests/minute; pace both answer and state calls.
        await asyncio.sleep(max(0, 6.2 - (time.monotonic() - last_dispatch)))
        last_dispatch = time.monotonic()
        request_value = dict(
            request.model_dump(exclude={"timeout"}), provider_model=model
        )

        def record(events: Any) -> None:
            evidence["provider_rows"].append(
                dict(
                    kind="stream",
                    request=request_value,
                    response=events,
                    manifest=request_manifest("stream", request_value),
                )
            )
            save()

        async with aclosing(
            capture_stream(original_stream(request, model, timeout), record)
        ) as events:
            async for event in events:
                yield event

    async def post(url: str, payload: Any, timeout: float) -> Any:
        response = await original_post(url, payload, timeout)
        if url.endswith("/agent/complete"):
            evidence["model_rows"].append(
                dict(
                    kind="complete",
                    request=copy.deepcopy(payload),
                    response=response,
                    manifest=request_manifest("complete", payload),
                )
            )
            evidence["cost_eur"] += float(str(response.get("cost_eur", 0)))
            save()
            if evidence["cost_eur"] > 0.50:
                raise ValueError("live_budget_exceeded")
        return response

    gateway = FastAPI()

    @gateway.post("/agent/config")
    async def configuration(payload: dict[str, Any]) -> Any:
        return AgentProvider().configuration(payload.get("local_enabled", True))

    @gateway.post("/agent/complete")
    async def complete(payload: dict[str, Any]) -> Any:
        return await AgentProvider().complete(payload)

    with ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                os.environ,
                dict(
                    ATLAS_STABLE="0",
                    GPU_LOCAL="0",
                    ATLAS_IMAGE_ON_DEMAND="0",
                    ATLAS_INTERACTION_DIR=str(destination.resolve() / "runtime-logs"),
                ),
            )
        )
        fixture_type = ProjectWorkHTTPTests
        fixture_type.setUpClass()
        stack.callback(fixture_type.doClassCleanups)
        fixture = fixture_type(
            methodName="test_work_handoff_runs_tools_and_releases_after_cleanup"
        )
        stack.callback(fixture.doCleanups)
        fixture.setUp()
        stack.enter_context(fixture.browser)
        gateway_url = getattr(ProjectAuthTests.serve, "__func__")(fixture_type, gateway)
        stack.enter_context(patch.dict(os.environ, {"ATLAS_GATEWAY_URL": gateway_url}))
        policy = copy.deepcopy(fixture.policy)
        for name in ("stored_bytes", "versioned_bytes", "temporary_bytes"):
            policy["quotas"][name] = 300000000
        with psycopg.connect(fixture.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s", (Jsonb(policy),)
            )
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s WHERE org_id=%s",
                (Jsonb(policy), fixture.org),
            )
        project = fixture.request(
            "POST",
            fixture.prefix(),
            "editor",
            dict(
                name="Bounded context recording",
                instructions="Ship only to France or Germany. Never ship outside these countries.",
            ),
        )
        if project.status_code != 201:
            raise ValueError("project_setup_failed")
        fixture.project = project.json()["id"]
        # A confirmed fixture constraint is stored in the real memory table, beyond
        # the old eight-entry cutoff, to exercise mandatory pinning.
        with store(fixture.app_dsn).scope(
            fixture.identity("editor"), fixture.org, fixture.project
        ) as scope:
            files = ProjectFiles(scope)
            for index in range(10):
                entry = files.memory(dict(text=f"Reference {index}", source_refs=[]))
                entry.update(status="active", confirmed_by=fixture.members["editor"])
                files.save(entry)
            entry = files.memory(
                dict(
                    text="Ship only to France or Germany. Never ship outside these countries.",
                    source_refs=[],
                )
            )
            entry.update(
                kind="constraint",
                status="active",
                confirmed_by=fixture.members["editor"],
            )
            files.save(entry)
        conversation = fixture.request(
            "POST",
            fixture.project_path() + "/conversations",
            "editor",
            dict(title="Forty-turn state capture"),
        ).json()["id"]
        work_session = None
        if action == "work":
            created = fixture.request(
                "POST",
                fixture.project_path() + "/work-sessions",
                "editor",
                dict(conversation_id=conversation, folder_ids=[], base_versions=[]),
            )
            if created.status_code != 201:
                raise ValueError("work_session_setup_failed")
            work_session = created.json()["id"]
        stack.enter_context(patch.object(stream_transport, "attempt", stream))
        stack.enter_context(patch.object(GatewayModel, "post", staticmethod(post)))
        for number in range(1, count + 1):
            question = "Acknowledge this register checkpoint briefly. No tools or external research are needed."
            if number == 1:
                question = "Maintain this plan: Inspect source; Calculate allocation; Write register; Verify totals. Our project permits shipment only to France or Germany. Acknowledge briefly."
            elif number == 3:
                question = "The approved unit cost is EUR 37.25 for 48 units. Retain both figures exactly. Acknowledge briefly."
            elif number == count:
                question = "Use the earlier unit cost and quantity to calculate the total. Can we ship to Canada? State the four plan steps."
            else:
                question += (
                    "\nTransient audit log (no decisions or facts to retain):\n"
                    + ("checkpoint unchanged; no new allocation data. " * 250)
                )
            payload = dict(
                model="atlas-qwen",
                org_id=fixture.org,
                project_id=fixture.project,
                conversation_id=conversation,
                project_action=action,
                **({"work_session_id": work_session} if work_session else {}),
                messages=[dict(role="user", content=question)],
                stream=True,
            )
            started = time.monotonic()
            response = fixture.browser.post(
                "/v1/chat/completions", json=payload, headers=fixture.front()
            )
            elapsed = time.monotonic() - started
            if response.status_code != 200:
                evidence["failed_status"] = response.status_code
                save()
                raise ValueError("public_turn_failed")
            response_events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            evidence["turns"].append(
                dict(
                    number=number,
                    request=payload,
                    events=response_events,
                    elapsed_seconds=elapsed,
                )
            )

            # Wait only in the recorder, after the public answer has returned.
            async def drain() -> None:
                if tasks:
                    await asyncio.wait_for(asyncio.gather(*list(tasks)), 45)

            assert fixture.browser.portal is not None
            fixture.browser.portal.call(drain)
            state_response = fixture.request(
                "POST",
                fixture.project_path() + "/context",
                "editor",
                dict(
                    conversation_id=conversation, query="Inspect state", action="chat"
                ),
            )
            state = state_response.json()
            evidence["state_rows"].append(state)
            save()
            if len(state.get("represented_turn_ids", [])) != number:
                raise ValueError("state_update_did_not_persist")
            print(
                "Captured turn",
                number,
                "state turns",
                len(state["represented_turn_ids"]),
                "cost EUR",
                evidence["cost_eur"],
                flush=True,
            )
        ledger = fixture.request(
            "GET", fixture.project_path() + "/ledger", "editor"
        ).json()
        evidence["ledger"] = ledger
        save()


if __name__ == "__main__":
    capture(
        Path(sys.argv[1]).resolve(),
        int(sys.argv[2]) if len(sys.argv) > 2 else 40,
        action=sys.argv[3] if len(sys.argv) > 3 else "chat",
    )
