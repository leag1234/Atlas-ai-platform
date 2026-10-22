"""Chat commands select project scope without global user/session state."""

import re
from uuid import UUID

import aiohttp

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.model import GatewayModel
from services.orchestrator.stream_client import sink_context

MARKER = re.compile(r"\[atlas-project:([0-9a-f-]{36}):([0-9a-f-]{36})\]$")


async def select(request: ChatRequest, item: Interaction, base: str) -> bool:
    text = request.messages[-1].text.strip()
    if text.startswith("/project "):
        command, _, name = text.removeprefix("/project ").partition(" ")
        if command == "leave":
            answer = "Project context cleared. [atlas-project:none]"
        elif command in {"create", "use"} and 0 < len(name.strip()) <= 120:
            name = name.strip()
            if command == "create":
                project = await GatewayModel.post(base + "/projects", {"name": name}, 5)
                identifier = str(project["id"])
            else:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=5), trust_env=False
                ) as client:
                    async with client.get(
                        base + "/projects", allow_redirects=False
                    ) as response:
                        response.raise_for_status()
                        projects = await response.json()
                matches = [p for p in projects if name in (p["name"], p["id"])]
                if len(matches) != 1:
                    raise ValueError("project_name_missing_or_ambiguous")
                identifier = str(matches[0]["id"])
            UUID(identifier)
            conversation = await GatewayModel.post(
                base + f"/projects/{identifier}/conversations", {"name": "Chat"}, 5
            )
            answer = f"Project selected: {name}. [atlas-project:{identifier}:{conversation['id']}]"
        else:
            answer = "Use /project create <name>, /project use <name or ID>, or /project leave."
        item.reponse, item.state = answer, "done"
        sink = sink_context.get()
        if sink:
            await sink({"delta": {"content": answer}, "memory": True})
        return True
    if request.project_id is None:
        # Only an explicit user command can change scope. Model prose cannot.
        for index in range(len(request.messages) - 2, 0, -1):
            message, previous = request.messages[index], request.messages[index - 1]
            if message.role != "assistant" or previous.role != "user":
                continue
            if previous.text.strip() == "/project leave":
                request.messages = request.messages[index + 1 :]
                break
            if not previous.text.startswith(("/project create ", "/project use ")):
                continue
            match = MARKER.search(message.text)
            if match:
                project_id, conversation_id = match.groups()
                UUID(project_id)
                UUID(conversation_id)
                request.project_id, request.conversation_id = (
                    project_id,
                    conversation_id,
                )
                break
    return False
