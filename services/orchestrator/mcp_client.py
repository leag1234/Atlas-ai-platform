"""Administrator-owned MCP policy; no write execution from model arguments."""

from packages.limits import LimitError
from packages.validation import describe_validation

import json
import os
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from services.orchestrator.mcp_transport import invoke


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MCPCall(Strict):
    server: str = Field(max_length=64, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    tool: str = Field(max_length=64, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    arguments: dict[str, object]


class Tool(Strict):
    effect: Literal["read", "write"]
    fields: list[str]
    fixed: dict[str, object] = Field(default_factory=dict)


class Server(Strict):
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: dict[str, str] = Field(default_factory=dict)
    tools: dict[str, Tool]


def configuration() -> dict[str, Server]:
    path = os.environ.get("ATLAS_MCP_CONFIG")
    if not path:
        return {}
    with Path(path).open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise LimitError("mcp_config_limit", len(raw), 65536, "bytes")
    return TypeAdapter(dict[str, Server]).validate_json(raw)


def resolve(call: MCPCall) -> tuple[Server, Tool, dict[str, str], list[str]]:
    server = configuration()[call.server]
    tool = server.tools[call.tool]
    credentials = {key: os.environ[name] for key, name in server.secret_env.items()}
    secrets = list(credentials.values())
    if len(call.model_dump_json().encode()) > 16384:
        raise LimitError(
            "mcp_arguments", len(call.model_dump_json().encode()), 16384, "bytes"
        )
    if (
        any(not value for value in secrets)
        or len(call.model_dump_json().encode()) > 16384
    ):
        raise ValueError("mcp_arguments")
    if set(call.arguments) - set(tool.fields) - set(tool.fixed) or any(
        key in call.arguments and call.arguments[key] != value
        for key, value in tool.fixed.items()
    ):
        raise ValueError("mcp_scope")
    call.arguments = {**call.arguments, **tool.fixed}
    if len(call.model_dump_json().encode()) > 16384:
        raise LimitError(
            "mcp_arguments", len(call.model_dump_json().encode()), 16384, "bytes"
        )
    if any(secret in call.model_dump_json() for secret in secrets):
        raise ValueError("mcp_secret_argument")
    env = {**server.env, **credentials}
    return server, tool, env, secrets


def declaration() -> dict[str, object]:
    names = {
        name: {tool: policy.model_dump() for tool, policy in server.tools.items()}
        for name, server in configuration().items()
    }
    for server in configuration().values():
        if any(
            os.environ[name] in json.dumps(names) for name in server.secret_env.values()
        ):
            raise ValueError("mcp_secret_configuration")
    return {
        "type": "function",
        "function": {
            "name": "mcp_call",
            "description": Path("prompts/mcp.txt").read_text() + json.dumps(names),
            "parameters": MCPCall.model_json_schema(),
        },
    }


def audit(call: MCPCall, state: str, started: float) -> None:
    directory = Path(os.environ.get("ATLAS_MCP_LOG_DIR", "BRAIN/mcp"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with os.fdopen(
        os.open(
            directory / "actions.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        ),
        "w",
    ) as stream:
        stream.write(
            json.dumps(
                {
                    "server": call.server,
                    "tool": call.tool,
                    "state": state,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                }
            )
            + "\n"
        )


async def execute(raw: str, timeout: float) -> dict[str, object]:
    call, started, state = None, time.monotonic(), "error"
    try:
        if len(raw.encode()) > 16384:
            raise LimitError("mcp_arguments", len(raw.encode()), 16384, "bytes")
        candidate = MCPCall.model_validate_json(raw)
        server, tool, env, secrets = resolve(candidate)
        call = candidate
        if tool.effect == "write":
            state = "confirmation_required"
            from services.orchestrator.mcp_confirmation import prepare

            return prepare(call)
        result = await invoke(
            server.command,
            server.args,
            env,
            secrets,
            call.tool,
            call.arguments,
            timeout,
        )
        state = "read_ok"
        return {"trust": "untrusted", "data": result}
    except ValidationError as exc:
        if all(failure["type"] == "missing" for failure in exc.errors()):
            return {"error": "mcp_unavailable_or_refused"}
        return {
            "error": "mcp_unavailable_or_refused",
            "message": describe_validation(exc),
        }
    except LimitError as exc:
        return {"error": exc.code, "message": exc.detail}
    except Exception:
        return {"error": "mcp_unavailable_or_refused"}
    finally:
        if call is not None:
            audit(call, state, started)
