"""Opt-in live provider proof using local MCP/OAuth and actual HTTP services."""

import base64
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb
import uvicorn

from connector_fake import FakeMCP
import test_project_isolation as isolation
from packages.ports import atlas_ports
from services.retrieval.project_install import upgrade_work
from services.retrieval.project_scoped_api import store
from services.retrieval.project_files import ProjectFiles


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    from gateway_cpu import CPUModels
    from http_gateway import serve

    root = Path("BRAIN/mcp-connectors")
    root.mkdir(parents=True, exist_ok=True)
    ports = atlas_ports()
    fixture = isolation.ProjectIsolationTests()
    fixture.setUpClass()
    servers: list[uvicorn.Server] = []
    threads: list[threading.Thread] = []
    gateway = None
    fake = FakeMCP(public_read_only=True)
    transcript: dict[str, Any] = dict(mode="live", cost_eur=0, steps=[])
    try:
        fixture.setUp()
        upgrade_work(fixture.admin_dsn)
        public, personal = str(uuid4()), str(uuid4())
        with psycopg.connect(fixture.admin_dsn) as db:
            for table, field in [
                ("organizations", "policy"),
                ("installation", "ceilings"),
            ]:
                db.execute(
                    f"UPDATE atlas_projects.{table} SET {field}=jsonb_set({field}, '{{connector_allowlist}}', %s)",
                    (Jsonb([public, personal]),),
                )
        with store(fixture.app_dsn).scope(
            fixture.identity("owner", fixture.project), fixture.org, fixture.project
        ) as scope:
            files = ProjectFiles(scope)
            conversation = dict(
                files.new(),
                title="Shared connector check",
                owner_member_id=fixture.members["owner"],
                visibility="shared",
            )
            files.put("conversation", conversation)
        proxy = secrets.token_urlsafe(48)
        environment = dict(
            ATLAS_MCP_CONNECTORS="1",
            ATLAS_MCP_ALLOW_LOCAL="1",
            ATLAS_MCP_BROKER_KEY=base64.b64encode(os.urandom(32)).decode(),
            ATLAS_MCP_BROKER_DB=str(Path(fixture.directory.name) / "broker.sqlite"),
            ATLAS_PROJECT_IDENTITY_SECRET=secrets.token_urlsafe(48),
            ATLAS_PROJECT_IDENTITY_DSN=fixture.app_dsn,
            ATLAS_PROJECT_ORIGIN=ports.url("adapter"),
            ATLAS_PROXY_SECRET=proxy,
            ATLAS_GATEWAY_URL=ports.url("gateway"),
            ATLAS_RETRIEVAL_URL=ports.url("retrieval"),
            ATLAS_INTERACTION_DIR=str(root / "chat-records"),
            ATLAS_WEB_CACHE=str(Path(fixture.directory.name) / "web.sqlite"),
            GPU_LOCAL="0",
            ATLAS_TERMINAL_ENABLED="0",
            ATLAS_PROJECT_MODE="0",
        )
        with patch.dict(os.environ, environment):
            gateway = serve(CPUModels(), ports.port("gateway"))
            thread = threading.Thread(target=gateway.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
            for app, service in [
                ("services.retrieval.api:app", "retrieval"),
                ("services.orchestrator.chat_api:app", "adapter"),
            ]:
                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        host="127.0.0.1",
                        port=ports.port(service),
                        log_level="error",
                        access_log=False,
                    )
                )
                servers.append(server)
                thread = threading.Thread(target=server.run, daemon=True)
                thread.start()
                threads.append(thread)
            deadline = time.monotonic() + 30
            while not all(server.started for server in servers):
                if time.monotonic() > deadline:
                    raise RuntimeError("local_stack_start_timeout")
                time.sleep(0.1)
            with httpx.Client(base_url=ports.url("adapter"), timeout=130) as client:

                def headers(member: str) -> dict[str, str]:
                    return {
                        "Origin": ports.url("adapter"),
                        "X-Atlas-User": "administrator"
                        if member == "admin"
                        else member,
                        "X-Atlas-Proxy-Secret": proxy,
                    }

                def call(
                    operation: str, data: dict[str, Any], member: str = "editor"
                ) -> dict[str, Any]:
                    response = client.post(
                        f"/api/atlas/mcp/{fixture.org}/{operation}",
                        json=data,
                        headers=headers(member),
                    )
                    if response.status_code != 200:
                        raise RuntimeError(
                            f"connector_{operation}_status_{response.status_code}: {response.json().get('error')}"
                        )
                    return dict(response.json())

                for identifier, name, kind, auth, path in [
                    (public, "Public Handbook", "shared", "none", "/public"),
                    (personal, "My Account", "personal", "oauth", "/personal"),
                ]:
                    config = dict(
                        id=identifier,
                        name=name,
                        kind=kind,
                        auth=auth,
                        url=fake.url + path,
                    )
                    if auth == "oauth":
                        config.update(
                            client_id="atlas-local-check",
                            redirect_uri=ports.url("adapter")
                            + f"/api/atlas/mcp/{fixture.org}/oauth/callback/{identifier}",
                        )
                    call("register", config, "admin")
                    for member in ("editor", "owner"):
                        call(
                            "member-rule",
                            dict(
                                connector_id=identifier,
                                member_id=fixture.members[member],
                                rule="allow",
                            ),
                            "admin",
                        )
                transcript["steps"].append(
                    dict(admin_registered=True, member_rules=True)
                )
                question = "Using the Public Handbook connector, what is the Atlas handbook release date?"
                response = client.post(
                    "/v1/chat/completions",
                    headers=headers("editor"),
                    json=dict(
                        model="atlas-qwen",
                        connector_org_id=fixture.org,
                        messages=[dict(role="user", content=question)],
                        stream=False,
                    ),
                )
                result = response.json()
                transcript["cost_eur"] = result.get("atlas", {}).get("cost_eur", 0)
                answer = (
                    result.get("choices", [{}])[0].get("message", {}).get("content", "")
                )
                transcript["steps"].append(
                    dict(
                        question=question,
                        status=response.status_code,
                        answer=answer,
                        calls=list(fake.calls),
                        usage=result.get("usage"),
                        atlas=result.get("atlas"),
                    )
                )
                (root / "live-transcript.json").write_text(
                    json.dumps(transcript, indent=2)
                )
                if (
                    response.status_code != 200
                    or "12" not in answer
                    or "November" not in answer
                    or not any(
                        c["tool"] == "search" and c["account"] == "public"
                        for c in fake.calls
                    )
                ):
                    raise RuntimeError("live_chat_missing_connector_evidence")
                start = call("oauth-start", dict(connector_id=personal))
                redirect = httpx.get(start["authorization_url"], follow_redirects=False)
                query = {
                    k: v[0]
                    for k, v in parse_qs(
                        urlsplit(redirect.headers["Location"]).query
                    ).items()
                }
                # Exercise the actual browser callback, keeping code/state out of evidence.
                callback = client.get(
                    f"/api/atlas/mcp/{fixture.org}/oauth/callback/{personal}",
                    params=query,
                    headers={
                        k: v for k, v in headers("editor").items() if k != "Origin"
                    },
                )
                assert callback.status_code == 200
                private = call(
                    "call",
                    dict(
                        connector_id=personal,
                        tool="search",
                        arguments=dict(query="release"),
                    ),
                )
                assert "12 November" in json.dumps(private)
                denied = client.post(
                    f"/api/atlas/mcp/{fixture.org}/call",
                    json=dict(
                        connector_id=personal,
                        tool="search",
                        arguments=dict(query="release"),
                    ),
                    headers=headers("owner"),
                )
                assert denied.status_code == 409
                call(
                    "project-list",
                    dict(project_id=fixture.project, connectors=[personal]),
                    "owner",
                )
                pending = call(
                    "call",
                    dict(
                        project_id=fixture.project,
                        conversation_id=conversation["id"],
                        connector_id=personal,
                        tool="search",
                        arguments=dict(query="release"),
                    ),
                )
                assert pending["state"] == "approval_required"
                approved = call(
                    "approve",
                    dict(
                        project_id=fixture.project,
                        approval_id=pending["approval_id"],
                        allow=True,
                    ),
                )
                assert "12 November" in json.dumps(approved)
                again = call(
                    "call",
                    dict(
                        project_id=fixture.project,
                        conversation_id=conversation["id"],
                        connector_id=personal,
                        tool="search",
                        arguments=dict(query="release"),
                    ),
                )
                assert again["state"] == "approval_required"
                call("disconnect", dict(connector_id=personal))
                transcript["steps"].append(
                    dict(
                        oauth_callback=callback.status_code,
                        own_account=private,
                        other_member_status=denied.status_code,
                        shared_approval=pending,
                        approved=approved,
                        repeat_state=again["state"],
                        disconnected=True,
                    )
                )
                transcript["completed"] = True
                (root / "live-transcript.json").write_text(
                    json.dumps(transcript, indent=2)
                )
                print(
                    json.dumps(
                        dict(
                            completed=True,
                            cost_eur=transcript["cost_eur"],
                            transcript=str(root / "live-transcript.json"),
                        )
                    )
                )
    finally:
        for server in servers:
            server.should_exit = True
        if gateway is not None:
            gateway.shutdown()
            gateway.server_close()
        for thread in threads:
            thread.join(timeout=10)
        fake.close()
        fixture.doCleanups()
        fixture.doClassCleanups()


if __name__ == "__main__":
    main()
