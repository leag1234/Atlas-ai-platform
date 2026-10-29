"""Authorized registry and credential broker; no credential crosses this boundary."""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator
from referencing import Registry
from pydantic import Field

from services.orchestrator.mcp_http import HTTP
from services.orchestrator.mcp_transport import invoke
from services.retrieval.connector_store import BrokerStore, Closed, Connector, allowed
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_store import Scope


class Tool(Closed):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    description: str = Field(default="", max_length=4096)
    inputSchema: dict[str, Any]
    annotations: dict[str, Any] = Field(default_factory=dict)


class Action(Closed):
    connector_id: str
    tool: str
    arguments: dict[str, Any]
    conversation_id: str | None = None


def check_references(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("mcp_schema_depth")
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef"} and (
                not isinstance(child, str) or not child.startswith("#")
            ):
                raise ValueError("mcp_remote_schema_ref")
            check_references(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            check_references(child, depth + 1)


class Service:
    def __init__(self, scope: Scope, store: BrokerStore) -> None:
        self.scope, self.store = scope, store
        self.org, self.member = scope.identity.org, scope.identity.member

    def connector(self, identifier: str, require_allow: bool = True) -> Connector:
        UUID(identifier)
        value = self.store.get(self.org, "connector", "", identifier)
        if value is None:
            raise ProjectError(404, "connector_not_found")
        connector = Connector.model_validate(value)
        if require_allow:
            rule = self.store.get(self.org, "rule", self.member, identifier)
            project = None
            if self.scope.project is not None:
                grant = self.store.get(
                    self.org, "project", self.scope.project_id, "connectors"
                )
                project = grant["connectors"] if grant else []
            if not allowed(
                self.scope.policy["connector_allowlist"],
                identifier,
                rule["rule"] if rule else None,
                project=project,
            ):
                raise ProjectError(403, "connector_policy_denied")
        return connector

    def token(self, connector: Connector) -> str:
        if connector.auth == "none":
            return ""
        credential = self.store.get(self.org, "credential", self.member, connector.id)
        if credential is None or credential.get("expires", 0) < time.time():
            raise ProjectError(409, "connector_account_required")
        return str(credential["token"])

    async def exchange(
        self, connector: Connector, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        token = self.token(connector)
        if connector.url:
            result = await HTTP(connector.url, token).exchange(method, params)
        else:
            command = json.loads(os.environ.get("ATLAS_MCP_COMMANDS", "{}"))[
                connector.command_id
            ]
            # Exact deployment allowlist: neither member nor administrator may add args/env.
            env = {"PATH": os.defpath}
            if token:
                env[str(command.get("token_env", "MCP_ACCESS_TOKEN"))] = token
            result = await invoke(
                command["command"],
                command.get("args", []),
                env,
                [token] if token else [],
                str(params.get("name", "")),
                params.get("arguments", {}),
                15,
                method=method,
            )
        return result

    async def discover(self, connector: Connector) -> list[dict[str, Any]]:
        result = await self.exchange(connector, "tools/list", {})
        values = result.get("tools")
        if not isinstance(values, list) or len(values) > 128:
            raise ValueError("mcp_discovery_limit")
        tools = []
        names: set[str] = set()
        for value in values:
            # MCP optional fields outside our declaration subset are not model authority.
            if not isinstance(value, dict):
                raise ValueError("mcp_tool_invalid")
            tool = Tool.model_validate(
                {k: v for k, v in value.items() if k in Tool.model_fields}
            )
            Draft202012Validator.check_schema(tool.inputSchema)
            check_references(tool.inputSchema)
            if tool.name in names or tool.inputSchema.get("type") != "object":
                raise ValueError("mcp_tool_schema_invalid")
            names.add(tool.name)
            tools.append(tool.model_dump())
        return tools

    def conversation(self, identifier: str | None) -> bool:
        if self.scope.project is None:
            if identifier is not None:
                raise ProjectError(403, "connector_scope_denied")
            return False
        if identifier is None:
            raise ProjectError(422, "conversation_required")
        UUID(identifier)
        from services.retrieval.project_files import ProjectFiles

        conversation = ProjectFiles(self.scope).get("conversation", identifier)
        # Legacy conversations are project-readable; only explicit privacy is private.
        shared = conversation.get("visibility") != "private"
        if not shared and conversation["owner_member_id"] != self.member:
            raise ProjectError(404, "not_found")
        return shared

    async def call(
        self, action: Action, approved: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        shared = self.conversation(action.conversation_id)
        connector = self.connector(action.connector_id)
        tools = await self.discover(connector)
        tool = next((tool for tool in tools if tool["name"] == action.tool), None)
        if tool is None:
            raise ProjectError(404, "connector_tool_not_found")
        Draft202012Validator(tool["inputSchema"], registry=Registry()).validate(
            action.arguments
        )
        token = self.token(connector)
        if len(action.model_dump_json().encode()) > 16384:
            raise ProjectError(
                413,
                "connector_arguments_limit",
                f"Connector arguments: {len(action.model_dump_json().encode())} bytes; limit 16384 bytes",
            )
        if token and token in action.model_dump_json():
            raise ValueError("connector_secret_argument")
        digest = hashlib.sha256(
            json.dumps(
                dict(
                    connector=connector.model_dump(),
                    tool=tool,
                    credential_digest=hashlib.sha256(token.encode()).hexdigest(),
                    command=json.loads(os.environ.get("ATLAS_MCP_COMMANDS", "{}")).get(
                        connector.command_id
                    )
                    if connector.command_id
                    else None,
                ),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        personal = connector.kind == "personal" and shared
        needs_approval = personal or tool["annotations"].get("readOnlyHint") is not True
        if needs_approval and approved is None:
            identifier = str(uuid4())
            self.store.put(
                self.org,
                "approval",
                self.member,
                identifier,
                dict(
                    action=action.model_dump(),
                    project=self.scope.identity.project,
                    digest=digest,
                    expires=time.time() + 300,
                ),
            )
            self.record("approval_requested", action, "intent", identifier)
            return dict(
                state="approval_required",
                approval_id=identifier,
                connector=connector.name,
                action=action.tool,
                visibility_warning="The result will be visible to every member of this project."
                if personal
                else "This action may change external data.",
            )
        if approved is not None and approved["digest"] != digest:
            raise ProjectError(409, "connector_configuration_changed")
        result = await self.exchange(
            connector, "tools/call", dict(name=action.tool, arguments=action.arguments)
        )
        self.record("tool_completed", action, "succeeded")
        return dict(trust="untrusted", data=result)

    def record(
        self, kind: str, action: Action, outcome: str, approval_id: str | None = None
    ) -> None:
        if self.scope.project is None:
            self.scope.event("connector_" + kind)
            return
        from services.retrieval.project_ledger import BYTE_KEYS, append

        append(
            self.scope,
            kind=kind,
            request_id=str(uuid4()),
            actor=dict(kind="member", member_id=self.member, service=None),
            resource_refs=[],
            source=dict(kind="connector", ref=action.connector_id),
            destination=dict(kind="server", ref=self.scope.project_id),
            byte_counts=dict.fromkeys(BYTE_KEYS, 0),
            outcome=outcome,
            conversation_id=action.conversation_id,
            approval_id=approval_id,
            connector=True,
        )

    async def execute(self, operation: str, data: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "register": set(Connector.model_fields),
            "list": set(),
            "discover": set(),
            "member-rule": {"connector_id", "member_id", "rule"},
            "project-list": {"connectors"},
            "connect-token": {"connector_id", "token"},
            "disconnect": {"connector_id"},
            "oauth-start": {"connector_id"},
            "oauth-finish": {"connector_id", "state", "code"},
            "call": set(Action.model_fields),
            "approve": {"approval_id", "allow"},
        }
        if operation not in fields or set(data) - fields[operation]:
            raise ValueError("connector_operation_invalid")
        if operation == "register":
            self.scope.admin()
            connector = Connector.model_validate(data)
            if self.store.get(self.org, "connector", "", connector.id) is not None:
                raise ProjectError(409, "connector_already_registered")
            if len(self.store.list(self.org, "connector")) >= 128:
                raise ProjectError(413, "connector_registry_limit")
            self.store.put(
                self.org, "connector", "", connector.id, connector.model_dump()
            )
            self.scope.event("connector_registered")
            return connector.model_dump()
        if operation == "member-rule":
            self.scope.admin()
            connector = self.connector(str(data["connector_id"]), False)
            member = str(UUID(data["member_id"]))
            if (
                self.scope.db.execute(
                    "SELECT id FROM atlas_projects.members WHERE org_id=%s AND id=%s",
                    (self.org, member),
                ).fetchone()
                is None
            ):
                raise ProjectError(404, "member_not_found")
            if data["rule"] not in {"allow", "deny"}:
                raise ValueError("connector_rule_invalid")
            self.store.put(
                self.org, "rule", member, connector.id, dict(rule=data["rule"])
            )
            self.scope.event("connector_member_rule")
            return dict(state="updated")
        if operation == "project-list":
            if self.scope.project is None or self.scope.role != "owner":
                raise ProjectError(403, "permission_denied")
            identifiers = data["connectors"]
            if not isinstance(identifiers, list) or len(identifiers) > 128:
                raise ValueError("connector_list_invalid")
            for identifier in identifiers:
                self.connector(identifier, False)
                if identifier not in self.scope.policy["connector_allowlist"]:
                    raise ProjectError(403, "connector_policy_denied")
            self.store.put(
                self.org,
                "project",
                self.scope.project_id,
                "connectors",
                dict(connectors=identifiers),
            )
            self.scope.event("connector_project_list", self.scope.project_id)
            return dict(state="updated")
        if operation in {"list", "discover"}:
            groups = []
            for value in self.store.list(self.org, "connector"):
                try:
                    connector = self.connector(value["id"])
                    group: dict[str, Any] = dict(
                        id=connector.id,
                        name=connector.name,
                        kind=connector.kind,
                        auth=connector.auth,
                    )
                    if operation == "discover":
                        group["tools"] = await self.discover(connector)
                    groups.append(group)
                except ProjectError as exc:
                    if exc.status not in {403, 409}:
                        raise
            return dict(connectors=groups)
        if operation == "approve":
            if type(data.get("allow")) is not bool:
                raise ValueError("connector_decision_required")
            pending = self.store.get(
                self.org, "approval", self.member, str(data["approval_id"]), take=True
            )
            if (
                pending is None
                or pending["expires"] < time.time()
                or pending["project"] != self.scope.identity.project
            ):
                raise ProjectError(409, "connector_approval_expired")
            action = Action.model_validate(pending["action"])
            self.conversation(action.conversation_id)
            self.connector(action.connector_id)
            self.record(
                "approval_decided",
                action,
                "succeeded" if data["allow"] else "denied",
                str(data["approval_id"]),
            )
            if not data["allow"]:
                return dict(
                    state="denied",
                    message="The connector was not run; no result is available.",
                )
            return await self.call(Action.model_validate(pending["action"]), pending)
        if operation == "call":
            return await self.call(Action.model_validate(data))
        connector = self.connector(str(data["connector_id"]), operation != "disconnect")
        if operation == "connect-token":
            token = data["token"]
            if (
                connector.auth != "token"
                or not isinstance(token, str)
                or not 1 <= len(token) <= 8192
                or any(c in token for c in "\r\n")
            ):
                raise ValueError("connector_token_invalid")
            self.store.put(
                self.org,
                "credential",
                self.member,
                connector.id,
                dict(token=token, expires=1e20),
            )
            return dict(state="connected")
        if operation == "disconnect":
            self.store.disconnect(self.org, self.member, connector.id)
            return dict(state="disconnected")
        from services.retrieval import connector_oauth

        if operation == "oauth-start":
            return await connector_oauth.start(
                self.store, self.org, self.member, connector
            )
        if operation == "oauth-finish":
            return await connector_oauth.finish(
                self.store,
                self.org,
                self.member,
                connector,
                str(data["state"]),
                str(data["code"]),
            )
        raise ProjectError(404, "connector_operation_unknown")
