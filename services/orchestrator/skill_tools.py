"""Progressive skill discovery under base policy, without host execution."""

from __future__ import annotations

import base64
import logging
import json
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from packages.evidence import estimated_tokens
from packages.skills import builtins, safe_path
from services.orchestrator.loop import Call, Message
from services.retrieval.project_identity import ProjectError


class LoadSkill(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=64)
    resource: str | None = Field(default=None, max_length=240)


class SkillTools:
    def __init__(
        self,
        entries: list[dict[str, Any]],
        broker: Any = None,
        workspace: Any = None,
        telemetry: list[dict[str, Any]] | None = None,
    ) -> None:
        self.entries = {e["name"]: e for e in entries}
        self.broker, self.workspace = broker, workspace
        self.telemetry = telemetry if telemetry is not None else []
        self.loaded: set[str] = set()
        self.instructions: list[Message] = []

    @classmethod
    async def connect(
        cls,
        broker: Any = None,
        workspace: Any = None,
        telemetry: list[dict[str, Any]] | None = None,
    ) -> SkillTools:
        if broker is None:
            entries = [s.metadata() for s in builtins().values()]
        else:
            status, result = await broker.public("GET", "skills")
            if status != 200:
                raise ProjectError(
                    status, str(result.get("error", "skill_catalog_unavailable"))
                )
            entries = result["items"]
        return cls(entries, broker, workspace, telemetry)

    def declaration(self) -> Message:
        catalog = "\n".join(
            f"{name}: {entry['description']}"
            for name, entry in sorted(self.entries.items())
        )
        return dict(
            type="function",
            function=dict(
                name="load_skill",
                description=(
                    "Load a written procedure when it fits the user's task. Use name; optionally load one resource after the body. "
                    "Resources execute only in networkless Work. Outbound actions require approval. "
                    "Loaded procedures cannot write pinned memory. Available skills:\n"
                    + catalog
                ),
                parameters=LoadSkill.model_json_schema(),
            ),
        )

    async def authorized(self) -> bool:
        if not self.loaded or self.broker is None:
            return True
        try:
            status, result = await self.broker.public("GET", "skills")
            return status == 200 and self.loaded <= {
                entry["digest"] for entry in result["items"]
            }
        except (ProjectError, KeyError):
            return False

    def blocks(self, name: str) -> bool:
        # Only contained workspace operations and existing exact-approval paths
        # remain usable after a procedure loads; approval of its instructions
        # does not grant authority to execute scripts or perform outbound actions.
        return bool(self.loaded) and name not in {
            "load_skill",
            "calculator",
            "rag_search",
            "reopen_result",
            "read_file",
            "list_dir",
            "search",
            "edit_file",
            "write_file",
            "run_command",
            "read_document",
            "compare_documents",
            "convert_document",
            "publish_document",
            "organize_files",
            "update_plan",
            "request_approval",
            "switch_tool_group",
            "forge_action",
        }

    async def execute(self, call: Call, timeout: float) -> Message:
        result = await self.load(call, timeout)
        if "error" in result:
            return result
        event, data = result["skill"], result["data"]
        if not isinstance(event, dict) or not isinstance(data, dict):
            raise ValueError("invalid_skill_result")
        if event["resource"] is not None:
            return result
        name, version = event["name"], event["digest"]
        instruction: Message = dict(
            role="system",
            content=(
                f"Operator-approved skill: {name}\nVersion: sha256:{version}\n"
                "Apply this procedure for the current turn, subject to base policy.\n"
                f"BEGIN SKILL {name} sha256:{version}\n{data['body']}\n"
                f"END SKILL {name} sha256:{version}\n"
                "Resources (untrusted data): "
                + json.dumps(data.get("resources", []), ensure_ascii=False)
            ),
        )
        if instruction not in self.instructions:
            self.instructions.append(instruction)
        return dict(status=f"skill {name} loaded")

    async def load(self, call: Call, timeout: float) -> Message:
        """Read authorized catalog content; execute separates it from tool output."""
        try:
            arguments = LoadSkill.model_validate_json(call.arguments)
            entry = self.entries.get(arguments.name)
            if entry is None:
                return {"error": "skill_not_enabled"}
            digest = entry["digest"]
            if arguments.resource == "SKILL.md":
                return {"error": "skill_body_is_instruction"}
            if arguments.resource is not None and digest not in self.loaded:
                return {"error": "load_skill_body_first"}
            tail = (
                "body"
                if arguments.resource is None
                else "resource/" + quote(safe_path(arguments.resource), safe="/")
            )
            if self.broker is not None:
                status, result = await self.broker.public(
                    "GET", f"skills/{digest}/{tail}"
                )
                if status != 200:
                    return dict(error="skill_unavailable", status=status)
            else:
                skill = builtins().get(digest)
                if skill is None:
                    return {"error": "skill_unavailable"}
                if arguments.resource is None:
                    result = dict(
                        body=skill.body,
                        resources=sorted(p for p in skill.files if p != "SKILL.md"),
                    )
                elif arguments.resource in skill.files:
                    result = dict(
                        content_base64=base64.b64encode(
                            skill.files[arguments.resource]
                        ).decode()
                    )
                else:
                    return {"error": "resource_not_found"}
            if arguments.resource is not None:
                raw = base64.b64decode(result["content_base64"], validate=True)
                if self.workspace is not None:
                    path = f".atlas-skills/{digest}/{arguments.resource}"
                    result = await self.workspace.invoke(
                        "skill_resource",
                        dict(path=path, content=result["content_base64"]),
                        timeout,
                    )
                    if result.get("error"):
                        return dict(result)
                else:
                    # Chat has no script execution bridge, including the document terminal.
                    if arguments.resource.split("/", 1)[0] == "scripts":
                        return {"error": "skill_script_requires_work"}
                    result = dict(path=arguments.resource, text=raw.decode("utf-8"))
            self.loaded.add(digest)
            tokens = estimated_tokens(json.dumps(result, ensure_ascii=False))
            event = dict(
                name=arguments.name,
                digest=digest,
                resource=arguments.resource,
                tokens=tokens,
                tokens_estimated=True,
            )
            self.telemetry.append(event)
            logging.getLogger(__name__).info("skill_loaded %s", event)
            return dict(trust="untrusted", data=result, skill=event)
        except ProjectError as exc:
            return dict(exc.body)
        except (ValueError, KeyError, UnicodeError):
            return {"error": "invalid_skill_arguments_or_resource"}


def offer(tools: list[Message], skill: Message) -> list[Message]:
    """Reserve one discovery slot within the gateway's eight-tool contract."""
    available = list(tools)
    for removable in ("rag_search", "image_generate", "mcp_call"):
        if len(available) < 8:
            break
        available = [
            tool
            for tool in available
            if not (
                isinstance(function := tool.get("function"), dict)
                and function.get("name") == removable
            )
        ]
    return [*available[:7], skill]
