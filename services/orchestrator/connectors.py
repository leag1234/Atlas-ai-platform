"""Authenticated connector API client and per-request discovered tool groups."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any
from uuid import UUID

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.loop import Call, Message
from services.orchestrator.project_ui import (
    broker_identity,
    bounded_json,
    browser_origin,
    public_error,
    verified_subject,
)
from services.retrieval.project_identity import ProjectError

router = APIRouter()
BROWSER_OPERATIONS = frozenset(
    {
        "register",
        "list",
        "member-rule",
        "project-list",
        "connect-token",
        "disconnect",
        "oauth-start",
        "oauth-finish",
        "discover",
        "call",
        "approve",
    }
)


class Client:
    def __init__(
        self,
        subject: str,
        org: str,
        project: str | None = None,
        conversation: str | None = None,
    ) -> None:
        self.subject, self.org, self.project, self.conversation = (
            subject,
            org,
            project,
            conversation,
        )

    async def call(self, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        from services.orchestrator.chat_pipeline import retrieval_url

        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=40)
        ) as client:
            identity = await broker_identity(
                client, self.subject, self.org, self.project
            )
            async with client.post(
                retrieval_url() + "/internal/atlas/connectors/" + operation,
                json=data,
                headers={"X-Atlas-Project-Identity": identity["identity"]},
                allow_redirects=False,
            ) as reply:
                result = await bounded_json(reply)
                if reply.status != 200:
                    raise ProjectError(
                        reply.status,
                        str(result.get("error", "connector_unavailable")),
                        str(result.get("message", "")),
                    )
                return result


@router.post("/api/mcp/{org}/{operation}")
@router.post("/api/atlas/mcp/{org}/{operation}")
async def public_api(org: UUID, operation: str, request: Request) -> Response:
    if (
        operation not in BROWSER_OPERATIONS
        or os.environ.get("ATLAS_MCP_CONNECTORS") != "1"
    ):
        return JSONResponse(dict(error="not_found"), status_code=404)
    try:
        browser_origin(request)
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
        ) as client:
            subject = await verified_subject(request, client)
        body = bytearray()
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    raise ProjectError(
                        413,
                        "connector_request_limit",
                        f"Connector request: {len(body)} bytes; limit 16384 bytes",
                    )
        data = json.loads(body)
        project = data.pop("project_id", None)
        if project is not None:
            project = str(UUID(project))
        result = await Client(subject, str(org), project).call(operation, data)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except ProjectError as exc:
        return public_error(exc)
    except (ValueError, TypeError, AttributeError, aiohttp.ClientError, TimeoutError):
        return JSONResponse(
            dict(error="connector_unavailable_or_refused"), status_code=400
        )


@router.get("/api/mcp/{org}/oauth/callback/{connector}")
@router.get("/api/atlas/mcp/{org}/oauth/callback/{connector}")
async def oauth_callback(org: UUID, connector: UUID, request: Request) -> Response:
    if os.environ.get("ATLAS_MCP_CONNECTORS") != "1":
        return JSONResponse(dict(error="not_found"), status_code=404)
    query = dict(request.query_params)
    # Access loggers inspect the ASGI scope on completion; codes must not be logged.
    request.scope["query_string"] = b""
    try:
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
        ) as client:
            subject = await verified_subject(request, client)
        result = await Client(subject, str(org)).call(
            "oauth-finish",
            dict(
                connector_id=str(connector),
                state=query.get("state", ""),
                code=query.get("code", ""),
            ),
        )
        return JSONResponse(
            result,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    except ProjectError as exc:
        return public_error(exc)
    except (ValueError, aiohttp.ClientError, TimeoutError):
        return JSONResponse(dict(error="oauth_callback_refused"), status_code=400)


async def authenticate(request: Request, payload: ChatRequest) -> None:
    if payload._api_key_request:
        return
    if os.environ.get("ATLAS_MCP_CONNECTORS") != "1":
        return
    browser_origin(request)
    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
    ) as client:
        subject = payload._project_subject or await verified_subject(request, client)
        if (
            payload.org_id
            and payload.connector_org_id
            and payload.org_id != payload.connector_org_id
        ):
            raise ProjectError(409, "connector_scope_conflict")
        org = payload.org_id or payload.connector_org_id
        if org is not None:
            org = str(UUID(org))
        if org is None:
            memberships = (await broker_identity(client, subject, None, None))[
                "memberships"
            ]
            if len(memberships) != 1:
                # Ordinary chat cannot guess an organization for multi-org members.
                return
            org = str(memberships[0]["org_id"])
        payload._connector_scope = dict(
            subject=subject,
            org=org,
            project=payload.project_id,
            conversation=payload.conversation_id,
        )


class Selection:
    def __init__(self, client: Client, groups: list[dict[str, Any]]) -> None:
        self.client = client
        self.groups: dict[str, tuple[str, ...]] = {}
        self.catalog: dict[str, Any] = {}
        self.targets: dict[str, tuple[str, str]] = {}
        self.labels: dict[str, Any] = {}
        self.active = "chat"
        self.base: list[dict[str, Any]] = []
        self.model: Any = None
        self.approvals: list[dict[str, Any]] = []
        for group in groups:
            key = "connector_" + group["id"].replace("-", "")
            self.labels[key] = dict(name=group["name"], tools={})
            names = []
            for tool in group["tools"]:
                name = (
                    "mcp_"
                    + hashlib.sha256(
                        (group["id"] + ":" + tool["name"]).encode()
                    ).hexdigest()[:24]
                )
                self.targets[name] = (group["id"], tool["name"])
                self.catalog[name] = dict(
                    type="function",
                    function=dict(
                        name=name,
                        description=group["name"]
                        + ": "
                        + tool["name"]
                        + ". "
                        + tool["description"],
                        parameters=tool["inputSchema"],
                    ),
                )
                self.labels[key]["tools"][name] = tool["name"]
                names.append(name)
            self.groups[key] = tuple(names)

    def bind(self, model: Any) -> None:
        from services.orchestrator.api_key_edge import key_call

        if key_call.get():
            return
        self.model = model
        self.base = [
            tool for tool in model.tools if tool["function"]["name"] != "mcp_call"
        ]
        self.refresh()

    def refresh(self, focus: str | None = None) -> None:
        switch = dict(
            type="function",
            function=dict(
                name="switch_connector_group",
                description="Select chat tools or a connector for the next step. Chat tools: "
                + json.dumps([tool["function"]["name"] for tool in self.base])
                + ". Groups: "
                + json.dumps(self.labels)
                + ". Optional focus selects a discovered tool within the group. Approval must come from the user interface.",
                parameters=dict(
                    type="object",
                    properties=dict(
                        group=dict(type="string", enum=["chat", *self.groups]),
                        focus=dict(type="string"),
                    ),
                    required=["group"],
                    additionalProperties=False,
                ),
            ),
        )
        selected = self.base
        if self.active == "chat" and focus:
            selected = sorted(
                selected, key=lambda tool: tool["function"]["name"] != focus
            )
        if self.active != "chat":
            names = self.groups[self.active]
            if focus in names:
                names = (str(focus), *(name for name in names if name != focus))
            selected = [self.catalog[name] for name in names]
        self.model.tools = [switch, *selected[:7]]

    async def execute(self, call: Call, timeout: float) -> Message:
        from services.orchestrator.api_key_edge import key_call

        if key_call.get():
            return {"error": "insufficient_scope", "missing_scope": "connectors"}
        if call.name == "switch_connector_group":
            try:
                data = json.loads(call.arguments)
            except ValueError:
                return dict(error="invalid_connector_group")
            if not isinstance(data, dict) or not isinstance(data.get("group"), str):
                return dict(error="invalid_connector_group")
            group = data.get("group")
            if set(data) - {"group", "focus"} or group not in {"chat", *self.groups}:
                return dict(error="invalid_connector_group")
            if data.get("focus") is not None and data["focus"] not in (
                tuple(tool["function"]["name"] for tool in self.base)
                if group == "chat"
                else self.groups.get(group, ())
            ):
                return dict(error="invalid_connector_focus")
            self.active = group
            self.refresh(data.get("focus"))
            return dict(
                group=group, tools=[t["function"]["name"] for t in self.model.tools]
            )
        if call.name not in self.targets:
            return dict(error="connector_tool_not_selected")
        if self.model is not None and call.name not in {
            t["function"]["name"] for t in self.model.tools
        }:
            return dict(error="connector_tool_not_selected")
        connector, tool = self.targets[call.name]
        try:
            async with asyncio.timeout(timeout):
                result = await self.client.call(
                    "call",
                    dict(
                        connector_id=connector,
                        tool=tool,
                        arguments=json.loads(call.arguments),
                        conversation_id=self.client.conversation,
                    ),
                )
                if result.get("state") == "approval_required":
                    self.approvals.append(result)
                    from services.orchestrator.stream_client import sink_context

                    sink = sink_context.get()
                    if sink is not None:
                        await sink(dict(phase="connector_approval", approval=result))
                return result
        except ProjectError as exc:
            return dict(error=exc.body["error"], message=exc.body["message"])


async def prepare(request: ChatRequest) -> Selection | None:
    if request._api_key_request:
        return None
    if not request._connector_scope:
        return None
    scope = request._connector_scope
    client = Client(
        str(scope["subject"]),
        str(scope["org"]),
        scope.get("project"),
        scope.get("conversation"),
    )
    result = await client.call("discover", {})
    selection = Selection(client, result["connectors"])
    selection.approvals = request._connector_approvals
    return selection
