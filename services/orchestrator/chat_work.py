"""Step 1 of unified chat: a plain chat that needs development escalates to Work.

Behind ATLAS_CHAT_WORK=1 (default off), a coding request in a chat without a
project creates a hidden personal "Chats" scratch project (once per user) and a
Work session bound to the conversation, then runs the turn through process_work,
streaming its events into the chat like the project UI does. The Work wall clock
limit applies, not the chat 120 s limit.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

from packages.work_intent import needs_work
from services.orchestrator.chat_pipeline import retrieval_url
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.interactions import Interaction

logger = logging.getLogger(__name__)

SCRATCH_PROJECT_NAME = "Chats"
SCRATCH_PROJECT_HIDDEN = True


def chat_work_enabled() -> bool:
    return os.environ.get("ATLAS_CHAT_WORK") == "1"


def work_gate_open() -> bool:
    from services.orchestrator.code_ui import project_mode
    from services.orchestrator.code_workspace import work_enabled

    return project_mode() and work_enabled()


def should_escalate(request: ChatRequest) -> bool:
    """Routing decision for a plain chat turn (no project, no code mode)."""
    if not chat_work_enabled() or not work_gate_open():
        return False
    if request.project_id is not None or request.org_id is not None:
        return False
    if request.code_mode or request.project_action != "chat":
        return False
    if request.continuation or request.has_attachments:
        return False
    if any(message.images for message in request.messages):
        return False
    history = [
        {"role": message.role, "content": message.text}
        for message in request.messages[:-1]
    ]
    return needs_work(request.user_text, history)


async def _identity(client: aiohttp.ClientSession, subject: str) -> dict[str, Any]:
    from services.orchestrator.project_ui import broker_identity

    return await broker_identity(client, subject, None, None)


async def _public(
    client: aiohttp.ClientSession,
    subject: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    from services.orchestrator.project_ui import bounded_json, broker_identity

    resolved = await broker_identity(client, subject, *(_org_project(path)))
    async with client.request(
        method,
        retrieval_url() + path,
        json=body,
        headers={"X-Atlas-Project-Identity": str(resolved["identity"])},
        allow_redirects=False,
    ) as reply:
        result = await bounded_json(reply)
        return reply.status, result


def _org_project(path: str) -> tuple[str | None, str | None]:
    parts = path.strip("/").split("/")
    if (
        len(parts) >= 2
        and parts[0] == "api"
        and parts[1] == "atlas"
        and parts[2] == "orgs"
    ):
        org = parts[3]
        project = parts[5] if len(parts) > 5 and parts[4] == "projects" else None
        return org, project
    return None, None


async def _find_scratch_project(
    client: aiohttp.ClientSession, subject: str, org: str
) -> str | None:
    # Hidden projects are excluded from the user listing, so the escalation
    # resolves the scratch project through its own scoped read.
    status, listing = await _public(
        client,
        subject,
        "GET",
        f"/api/atlas/orgs/{org}/projects?include_hidden=1",
    )
    if status != 200:
        return None
    for item in listing.get("items", []):
        if (
            isinstance(item, dict)
            and item.get("name") == SCRATCH_PROJECT_NAME
            and bool(item.get("hidden")) is SCRATCH_PROJECT_HIDDEN
        ):
            return str(item["id"])
    return None


async def _ensure_scratch_project(
    client: aiohttp.ClientSession, subject: str, org: str
) -> str:
    existing = await _find_scratch_project(client, subject, org)
    if existing is not None:
        return existing
    status, created = await _public(
        client,
        subject,
        "POST",
        f"/api/atlas/orgs/{org}/projects",
        {
            "name": SCRATCH_PROJECT_NAME,
            "instructions": "",
            "default_action": "work",
            "hidden": SCRATCH_PROJECT_HIDDEN,
        },
    )
    if status not in (200, 201) or not isinstance(created.get("id"), str):
        raise RuntimeError("scratch_project_unavailable")
    return str(created["id"])


async def _find_binding(
    client: aiohttp.ClientSession,
    subject: str,
    org: str,
    project: str,
    chat_conversation_id: str,
) -> dict[str, str] | None:
    status, bound = await _public(
        client,
        subject,
        "GET",
        f"/api/atlas/orgs/{org}/projects/{project}/conversations/chat-binding"
        f"?chat_conversation_id={chat_conversation_id}",
    )
    if status != 200 or not isinstance(bound.get("id"), str):
        return None
    binding = bound.get("chat_binding")
    if not isinstance(binding, dict):
        return None
    session_id = binding.get("work_session_id")
    if not isinstance(session_id, str):
        return None
    return {"conversation_id": str(bound["id"]), "work_session_id": session_id}


async def _bind_conversation(
    client: aiohttp.ClientSession,
    subject: str,
    org: str,
    project: str,
    conversation_id: str,
    chat_conversation_id: str,
    work_session_id: str,
) -> None:
    status, _ = await _public(
        client,
        subject,
        "PATCH",
        f"/api/atlas/orgs/{org}/projects/{project}/conversations/{conversation_id}",
        {
            "chat_binding": {
                "chat_conversation_id": chat_conversation_id,
                "work_session_id": work_session_id,
            }
        },
    )
    if status != 200:
        raise RuntimeError("scratch_binding_unavailable")


async def _ensure_conversation(
    client: aiohttp.ClientSession,
    subject: str,
    org: str,
    project: str,
    chat_conversation_id: str | None,
) -> tuple[str, str | None]:
    """Bind once per chat conversation: reuse the stored mapping on later turns."""
    if chat_conversation_id:
        bound = await _find_binding(client, subject, org, project, chat_conversation_id)
        if bound is not None:
            return bound["conversation_id"], bound["work_session_id"]
    status, created = await _public(
        client,
        subject,
        "POST",
        f"/api/atlas/orgs/{org}/projects/{project}/conversations",
        {"title": "Chat"},
    )
    if status not in (200, 201) or not isinstance(created.get("id"), str):
        raise RuntimeError("scratch_conversation_unavailable")
    return str(created["id"]), None


async def _ensure_work_session(
    client: aiohttp.ClientSession,
    subject: str,
    org: str,
    project: str,
    conversation_id: str,
) -> str:
    status, session = await _public(
        client,
        subject,
        "POST",
        f"/api/atlas/orgs/{org}/projects/{project}/work-sessions",
        {
            "conversation_id": conversation_id,
            "folder_ids": [],
            "base_versions": [],
        },
    )
    if status not in (200, 201) or not isinstance(session.get("id"), str):
        raise RuntimeError("work_session_unavailable")
    return str(session["id"])


async def escalate_to_work(request: ChatRequest, item: Interaction) -> bool:
    """Create (once per user) the hidden scratch project and a Work session bound
    to this conversation, then run the turn through process_work. Returns True
    when the turn was handed to Work."""
    if not should_escalate(request):
        return False
    subject = str(
        getattr(request, "_project_subject", None)
        or getattr(request, "daily_user", "")
        or ""
    ).strip()
    if not subject or subject == "unknown":
        logger.info(
            "chat_work_fallback conversation=%s reason=no_subject",
            request.conversation_id or "unknown",
        )
        return False
    chat_conversation_id = (
        str(request.conversation_id) if request.conversation_id else None
    )
    try:
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=30)
        ) as client:
            identity = await _identity(client, subject)
            memberships = identity.get("memberships", [])
            if not memberships:
                logger.info(
                    "chat_work_fallback conversation=%s reason=no_organization",
                    chat_conversation_id or "unknown",
                )
                return False
            org = str(memberships[0]["org_id"])
            project = await _ensure_scratch_project(client, subject, org)
            existing = (
                await _find_binding(client, subject, org, project, chat_conversation_id)
                if chat_conversation_id
                else None
            )
            if existing is not None:
                bound_conversation, session_id = (
                    existing["conversation_id"],
                    existing["work_session_id"],
                )
            else:
                bound_conversation, _ = await _ensure_conversation(
                    client, subject, org, project, chat_conversation_id
                )
                session_id = await _ensure_work_session(
                    client, subject, org, project, bound_conversation
                )
                if chat_conversation_id:
                    await _bind_conversation(
                        client,
                        subject,
                        org,
                        project,
                        bound_conversation,
                        chat_conversation_id,
                        session_id,
                    )
    except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError) as exc:
        logger.warning(
            "chat_work_fallback conversation=%s reason=setup_failed error=%s",
            chat_conversation_id or "unknown",
            type(exc).__name__,
        )
        return False

    from services.orchestrator.code_mode import process_work, work_failure_reply
    from services.orchestrator.project_transcript import work_dispatch

    scoped = request.model_copy(
        update={
            "org_id": org,
            "project_id": project,
            "conversation_id": bound_conversation,
            "project_action": "work",
            "work_session_id": session_id,
            "messages": [ChatMessage(role="user", content=request.user_text)],
        }
    )
    scoped._project_subject = subject
    dispatch = work_dispatch.get()
    try:
        await process_work(
            scoped,
            item,
            subject=subject,
            org_id=org,
            project_id=project,
            session_id=session_id,
        )
    finally:
        if dispatch is not None:
            pass
    if item.state == "failed":
        reply = work_failure_reply(item, scoped.lang)
        if reply is not None:
            item.reponse = reply
    item.task_type = "work"
    logger.info(
        "chat_work_escalated conversation=%s session=%s",
        chat_conversation_id or "unknown",
        session_id,
    )
    return True
