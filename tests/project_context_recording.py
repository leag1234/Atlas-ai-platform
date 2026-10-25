"""Opt-in genuine Work capture on a disposable stack; never invoked by discovery.

Run with ATLAS_PROVIDER=hetzner and an injected ATLAS_PROVIDER_API_KEY. The
candidate stays under BRAIN until the keyless replay validates it. No credentials
or authenticated request headers are recorded. Existing provider_recording owns
stream capture and request matching; m24_dependencies owns terminal preflight.
"""

from __future__ import annotations

import copy
import gzip
import io
import json
import os
import sys
from contextlib import aclosing, ExitStack
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

from docx import Document
from fastapi import FastAPI
from psycopg.types.json import Jsonb
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))

from provider_recording import capture_stream, request_manifest

BOUND = 16000


def capture(attempt: Path, *, prepare_only: bool = False) -> None:
    from agent_provider import AgentProvider
    import provider_settings
    import stream_transport
    from m24_dependencies import file_dependencies
    from test_project_work import ProjectWorkHTTPTests
    from services.orchestrator import code_mode
    from services.orchestrator.code_tools import WorkTools
    from services.orchestrator.code_workspace import volume
    from services.orchestrator.model import GatewayModel
    from services.orchestrator.project_transcript import TranscriptBroker
    from services.retrieval.project_files import ProjectFiles
    from services.retrieval.project_scoped_api import store
    import subprocess

    if os.environ.get("ATLAS_PROVIDER") != "hetzner":
        raise ValueError("recording_requires_hetzner")
    preset = provider_settings.selected()
    if preset is None or preset["price"] != dict(currency="EUR", input=0, output=0):
        raise ValueError("recording_requires_free_preset")
    if not prepare_only and not provider_settings.api_key():
        raise ValueError("recording_requires_credential")
    attempt.mkdir(parents=True, exist_ok=False)
    evidence: dict[str, Any] = dict(
        mode="live",
        provider="hetzner",
        bound=BOUND,
        keep_results=4,
        provider_rows=[],
        model_rows=[],
        tool_rows=[],
        storage_rows=[],
    )
    secrets = [
        value
        for key, value in os.environ.items()
        if len(value) >= 8
        and any(word in key.upper() for word in ("KEY", "SECRET", "TOKEN", "PASSWORD"))
    ]

    def save() -> None:
        raw = json.dumps(evidence, ensure_ascii=False, default=str).encode()
        if any(secret.encode() in raw for secret in secrets):
            raise ValueError("secret_in_recording")
        (attempt / "candidate.json.gz").write_bytes(gzip.compress(raw, mtime=0))

    def row(kind: str, request: Any, response: Any) -> dict[str, Any]:
        return copy.deepcopy(
            dict(
                kind=kind,
                request=request,
                response=response,
                manifest=request_manifest(kind, request),
            )
        )

    live_started = False
    original_stream = stream_transport.attempt
    original_post = GatewayModel.post
    original_tool = WorkTools.execute
    original_broker = TranscriptBroker.call
    original_turn = code_mode.execute_turn

    async def stream(request: Any, model: str, timeout: float) -> Any:
        nonlocal live_started
        if not live_started:
            counter = Path("BRAIN/P1a-live-counter.json")
            used = json.loads(counter.read_text()) if counter.exists() else []
            if len(used) >= 2:
                raise RuntimeError("live_recording_attempts_exhausted")
            used.append(str(attempt.relative_to(Path.cwd())))
            counter.write_text(json.dumps(used) + "\n")
            live_started = True
            print("Live recording attempt", len(used), flush=True)
        value = dict(request.model_dump(exclude={"timeout"}), provider_model=model)

        def record(events: Any) -> None:
            evidence["provider_rows"].append(row("stream", value, events))
            save()

        async with aclosing(
            capture_stream(original_stream(request, model, timeout), record)
        ) as events:
            async for event in events:
                yield event

    async def post(url: str, payload: Any, timeout: float) -> Any:
        value = copy.deepcopy(payload)
        response = await original_post(url, payload, timeout)
        if url.endswith("/agent/config"):
            evidence["configuration"] = response
        if url.endswith("/agent/complete"):
            evidence["model_rows"].append(row("complete", value, response))
            save()
            print("Captured model dispatch", len(evidence["model_rows"]), flush=True)
        return response

    async def tool(runtime: Any, call: Any, timeout: float) -> Any:
        result = await original_tool(runtime, call, timeout)
        evidence["tool_rows"].append(row("tool", asdict(call), result))
        save()
        return result

    async def broker(runtime: Any, operation: str, data: Any, key: Any = None) -> Any:
        result = await original_broker(runtime, operation, data, key)
        if operation in {"result-put", "result-open"}:
            evidence["storage_rows"].append(row(operation, data, result))
            save()
        return result

    async def turn(
        request: Any,
        model: Any,
        tools: Any,
        limits: Any,
        workspace_context: Any = None,
        references: Any = None,
    ) -> Any:
        evidence["turn"] = dict(
            request=request.model_dump(),
            limits=asdict(limits),
            workspace_context=workspace_context,
        )
        result = await original_turn(
            request, model, tools, limits, workspace_context, references
        )
        evidence["result"] = asdict(result)
        evidence["context_samples"] = model.context_samples
        save()
        return result

    # These are real provider endpoints; the disposable gateway needs no embedding
    # model because this Work session selects immutable document versions directly.
    gateway = FastAPI()

    @gateway.post("/agent/config")
    async def configuration(payload: dict[str, Any]) -> Any:
        return AgentProvider().configuration(payload.get("local_enabled", True))

    @gateway.post("/agent/complete")
    async def complete(payload: dict[str, Any]) -> Any:
        return await AgentProvider().complete(payload)

    settings = dict(
        ATLAS_PORT_BASE=os.environ.get("ATLAS_PORT_BASE", "31000"),
        ATLAS_STABLE="0",
        GPU_LOCAL="0",
        ATLAS_IMAGE_ON_DEMAND="0",
        ATLAS_CONTEXT_KEEP_RESULTS="4",
        ATLAS_CODE_COST_EUR="0.30",
        ATLAS_CODE_MAX_STEPS="40",
        ATLAS_INTERACTION_DIR=str((attempt / "runtime-logs").resolve()),
    )
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, settings))
        # Preflight must complete before any provider request, with owned cleanup.
        stack.enter_context(file_dependencies(attempt))
        fixture_type = ProjectWorkHTTPTests
        fixture_type.setUpClass()
        stack.callback(fixture_type.doClassCleanups)
        fixture = fixture_type(
            methodName="test_work_handoff_runs_tools_and_releases_after_cleanup"
        )
        stack.callback(fixture.doCleanups)
        fixture.setUp()
        if fixture.real_worker_skip_reason:
            raise RuntimeError("recording_worker_unavailable")
        stack.callback(
            subprocess.run, ["docker", "volume", "rm", volume()], capture_output=True
        )
        url = getattr(
            __import__("test_project_auth").ProjectAuthTests.serve, "__func__"
        )(fixture_type, gateway)
        stack.enter_context(patch.dict(os.environ, {"ATLAS_GATEWAY_URL": url}))
        # The isolation fixture starts with 100-byte storage. Provision this test
        # project's capacity explicitly; production ceilings remain unchanged.
        policy = fixture.policy
        for key in ("stored_bytes", "versioned_bytes", "temporary_bytes"):
            policy["quotas"][key] = 300000000
        policy["quotas"]["upload_bytes"] = 16777216
        policy["quotas"]["per_turn"]["cost_eur"] = 0.30
        from services.retrieval.project_policy import Policy

        Policy.model_validate(policy)
        with psycopg.connect(fixture.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s", (Jsonb(policy),)
            )
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s WHERE org_id=%s",
                (Jsonb(policy), fixture.org),
            )
        source_paths = [
            "contracts/project-mode.md",
            "docs/17-project-mode.md",
            "README.md",
        ]
        source = "\n\n".join(Path(path).read_text() for path in source_paths)
        if len(source) < 60000:
            raise ValueError("recording_source_too_short")
        selected = []
        with store(fixture.app_dsn).scope(
            fixture.identity("editor"), fixture.org, fixture.project
        ) as scope:
            files = ProjectFiles(scope)
            folder = files.folder(
                dict(name="Project documents", locality="server", model_use="allow")
            )
            for index in range(30):
                doc = Document()
                doc.add_paragraph(source[index * 2000 : (index + 1) * 2000])
                buffer = io.BytesIO()
                doc.save(buffer)
                folder = files.get("folder", folder["id"])
                file = files.create_file(
                    folder["id"],
                    f"section-{index + 1:02d}.docx",
                    buffer.getvalue(),
                    folder["revision"],
                )
                selected.append(dict(file_id=file["id"], version_id=file["version_id"]))
        conversation = fixture.request(
            "POST",
            fixture.project_path() + "/conversations",
            "editor",
            dict(title="Context clearing live recording"),
        )
        if conversation.status_code != 201:
            raise RuntimeError("recording_conversation_setup")
        session = fixture.request(
            "POST",
            fixture.project_path() + "/work-sessions",
            "editor",
            dict(
                conversation_id=conversation.json()["id"],
                folder_ids=[folder["id"]],
                base_versions=selected,
            ),
        )
        if session.status_code != 201:
            raise RuntimeError("recording_session_setup")
        question = (
            "Audit all 30 selected project documents in order. Each document is one small "
            "consecutive section of the project contract/design. Read every document using "
            "read_document, one tool call per response, before drawing conclusions. "
            "For each call supply only file_id and version from the list below; omit selector "
            "and cursor because every file has just one short paragraph. Do not skip files, "
            "combine calls or summarize before reading all 30. After reading file 30, use "
            "reopen_result with only the stored result id of file 1 (omit range), then give "
            "a useful concise summary of isolation and context rules quoting a detail from "
            "the reopened file. No web search or file creation is needed.\n"
            + "\n".join(
                f"{i + 1}. "
                + json.dumps(dict(file_id=f["file_id"], version=f["version_id"]))
                for i, f in enumerate(selected)
            )
        )
        payload = dict(
            model=code_mode.code_profile(),
            org_id=fixture.org,
            project_id=fixture.project,
            conversation_id=conversation.json()["id"],
            project_action="work",
            work_session_id=session.json()["id"],
            messages=[dict(role="user", content=question)],
            stream=True,
        )
        from services.retrieval.project_work_contract import validate

        validate("ProjectChatRequest", payload)
        evidence["public_request"] = payload
        evidence["source_paths"] = source_paths
        if prepare_only:
            evidence["mode"] = "preflight-only"
            save()
            print(
                "Terminal and Work fixture preflight completed; no provider calls",
                flush=True,
            )
            return
        for target, name, replacement in (
            (stream_transport, "attempt", stream),
            (GatewayModel, "post", staticmethod(post)),
            (WorkTools, "execute", tool),
            (TranscriptBroker, "call", broker),
            (code_mode, "execute_turn", turn),
        ):
            stack.enter_context(patch.object(target, name, replacement))
        response = fixture.browser.post(
            "/v1/chat/completions", json=payload, headers=fixture.front()
        )
        evidence["http_status"] = response.status_code
        evidence["public_response"] = (
            [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            if response.status_code == 200
            else response.json()
        )
        save()
        print("Work HTTP status", response.status_code, flush=True)
        if response.status_code != 200:
            raise RuntimeError("recording_http_failed")


if __name__ == "__main__":
    import sys

    capture(Path(sys.argv[1]).resolve(), prepare_only="--prepare-only" in sys.argv[2:])
