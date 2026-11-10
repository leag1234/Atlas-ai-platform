"""Step 1 of unified chat: a plain chat that needs development escalates to Work.

Behind ATLAS_CHAT_WORK=1 (default off), a coding request in a chat without a
project creates a hidden personal "Chats" scratch project (once per user) and a
Work session bound to the conversation, then runs the turn through process_work,
streaming its events into the chat like the project UI does. The Work wall clock
limit applies, not the chat 120 s limit.
"""

from __future__ import annotations

import asyncio
import re
import logging
import os
from typing import Any
from uuid import uuid4

import aiohttp

from packages.work_intent import continues_work, work_reason
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


def eligible(request: ChatRequest) -> bool:
    """A plain chat turn that may reach Work: no project, no code mode, no attachment."""
    if not chat_work_enabled() or not work_gate_open():
        return False
    if request.project_id is not None or request.org_id is not None:
        return False
    if request.code_mode or request.project_action != "chat":
        return False
    if request.continuation or request.has_attachments:
        return False
    # The interface sends its own tasks (title, tags, suggestions) through the
    # same endpoint, quoting the conversation: they are never the user's request.
    if request.user_text.lstrip().startswith("### Task:"):
        return False
    return not any(message.images for message in request.messages)


def should_escalate(request: ChatRequest) -> bool:
    """Routing decision for a first turn: the request itself needs Work."""
    if not eligible(request):
        return False
    history = [
        {"role": message.role, "content": message.text}
        for message in request.messages[:-1]
    ]
    return work_reason(request.user_text, history) is not None


async def _identity(client: aiohttp.ClientSession, subject: str) -> dict[str, Any]:
    from services.orchestrator.project_ui import broker_identity

    return await broker_identity(client, subject, None, None)


async def _public(
    client: aiohttp.ClientSession,
    subject: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    revision: object = None,
) -> tuple[int, dict[str, Any]]:
    from services.orchestrator.project_ui import bounded_json, broker_identity

    resolved = await broker_identity(client, subject, *(_org_project(path)))
    headers = {"X-Atlas-Project-Identity": str(resolved["identity"])}
    if method != "GET":
        # Every mutating project request carries its own idempotency key.
        headers["Idempotency-Key"] = str(uuid4())
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    async with client.request(
        method,
        retrieval_url() + path,
        json=body,
        headers=headers,
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
    revision: object,
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
        revision=revision,
    )
    if status != 200:
        raise RuntimeError("scratch_binding_unavailable")


async def _create_conversation(
    client: aiohttp.ClientSession, subject: str, org: str, project: str, title: str
) -> tuple[str, object]:
    """A project conversation for this chat; returns its id and revision."""
    status, created = await _public(
        client,
        subject,
        "POST",
        f"/api/atlas/orgs/{org}/projects/{project}/conversations",
        {"title": title},
    )
    if status not in (200, 201) or not isinstance(created.get("id"), str):
        raise RuntimeError("scratch_conversation_unavailable")
    return str(created["id"]), created.get("revision", 0)


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


_DELIVERED_KINDS = (
    ("docx", r"\bdocx\b|\bword\b", "Word document"),
    ("xlsx", r"\bxlsx\b|\bexcel\b", "Excel workbook"),
    ("pptx", r"\bpptx\b|\bpowerpoint\b", "PowerPoint presentation"),
    ("pdf", r"\bpdf\b", "PDF document"),
)


def delivered_file_hint(request: ChatRequest) -> str:
    """For a change request, name the kind of file this conversation delivered last.

    "Add a sentence and update the document" does not say Word again: without
    this the changed letter came back as a text file (staging 2026-11-10). The
    hint goes to Work only; the person never sees it.
    """
    for message in reversed(request.conversation_messages[:-1]):
        text = message.text.lower()
        for extension, pattern, name in _DELIVERED_KINDS:
            if re.search(pattern, text):
                return (
                    f"\n\n(This continues the {name} delivered earlier in this "
                    f"conversation: write the updated document in full and deliver "
                    f"it as a .{extension} file.)"
                )
    return ""


async def escalate_to_work(request: ChatRequest, item: Interaction) -> bool:
    """Create (once per user) the hidden scratch project and a Work session bound
    to this conversation, then run the turn through process_work. Returns True
    when the turn was handed to Work."""
    if not eligible(request):
        return False
    chat_conversation_id = str(getattr(request, "native_chat_id", "") or "") or None
    reason = work_reason(request.user_text)
    # A change or a rerun in a conversation that already reached Work goes back
    # to its workspace; anything else stays an ordinary chat turn.
    follow_up = reason is None and continues_work(request.user_text)
    if reason is None and not (follow_up and chat_conversation_id):
        return False
    # The same subject as the project interface (the verified email), so the
    # links to the produced files open for the person who asked. The budget
    # identity (an interface user id) is another member and got 404 on them.
    subject = str(
        getattr(request, "_project_subject", None)
        or getattr(request, "artifact_owner", "")
        or ""
    ).strip()
    if not subject or "@" not in subject:
        logger.info(
            "chat_work_fallback conversation=%s reason=no_subject",
            request.conversation_id or "unknown",
        )
        return False
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
            if follow_up:
                known = await _find_scratch_project(client, subject, org)
                bound = (
                    await _find_binding(
                        client, subject, org, known, str(chat_conversation_id)
                    )
                    if known is not None
                    else None
                )
                if bound is None:
                    return False
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
                title = " ".join(request.user_text.split())[:80] or "Chat"
                bound_conversation, revision = await _create_conversation(
                    client, subject, org, project, title
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
                        revision,
                    )
    except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError) as exc:
        logger.warning(
            "chat_work_fallback conversation=%s reason=setup_failed error=%s step=%s",
            chat_conversation_id or "unknown",
            type(exc).__name__,
            str(exc)[:40] if isinstance(exc, RuntimeError) else "",
        )
        return False

    from services.orchestrator.code_mode import process_work, work_failure_reply

    scoped = request.model_copy(
        update={
            "org_id": org,
            "project_id": project,
            "conversation_id": bound_conversation,
            "project_action": "work",
            "work_session_id": session_id,
            # The earlier turns of this chat come along (text only, the last ten):
            # without them a change request found "an empty workspace" and asked
            # which document was meant (staging 2026-11-10).
            "messages": [
                *(
                    ChatMessage(role=message.role, content=message.text[:8000])
                    for message in request.conversation_messages[:-1][-10:]
                    if message.role in {"user", "assistant"} and message.text.strip()
                ),
                ChatMessage(
                    role="user",
                    content=request.user_text
                    + (delivered_file_hint(request) if follow_up else ""),
                ),
            ],
        }
    )
    scoped._project_subject = subject
    scoped.__dict__["approvals_unattended"] = True
    before = (item.state, list(item.erreurs), item.reponse)
    failure = ""
    try:
        await process_work(
            scoped,
            item,
            subject=subject,
            org_id=org,
            project_id=project,
            session_id=session_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - any Work failure falls back to the chat
        failure = type(exc).__name__
    if failure or item.state != "done":
        reply = None if failure else work_failure_reply(item, scoped.lang)
        logger.warning(
            "chat_work_incomplete conversation=%s state=%s errors=%s exception=%s explained=%s",
            chat_conversation_id or "unknown",
            item.state,
            ",".join(str(error) for error in item.erreurs)[:120],
            failure,
            reply is not None,
        )
        produced = str(item.work_failure.get("answer", ""))
        if (
            item.work_failure.get("category") == "tool_error"
            and "/deliverables/" in produced
        ):
            # The file exists and its links are in the answer: a tool error left
            # open on the way (a refused read, a refused switch) does not turn a
            # delivered file into a failure, in the chat (staging 2026-11-10).
            item.reponse, item.task_type, item.state = produced, "work", "done"
            item.erreurs, item.work_failure = before[1], {}
            return True
        if reply is not None and item.work_failure.get("category") != "tool_error":
            # A known limit (time, steps, budget) or failing tests: the explanation,
            # with what was produced, is the finished answer of this turn.
            item.reponse, item.task_type, item.state = reply, "work", "done"
            item.work_failure = {}
            return True
        # Anything else: the chat answers by itself, as it did before Work existed
        # behind it. The person never gets a raw failure for a turn the chat can answer.
        item.state, item.erreurs, item.reponse = before[0], before[1], before[2]
        item.work_failure = {}
        return False
    item.task_type = "work"
    logger.info(
        "chat_work_escalated reason=%s conversation=%s session=%s",
        reason or "follow_up",
        chat_conversation_id or "unknown",
        session_id,
    )
    return True
