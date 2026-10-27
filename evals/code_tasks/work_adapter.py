"""Measure the scoped Work engine with isolated project files and real inference.

The caller provides a disposable retrieval/gateway installation and organization.
Only the harness's extracted base workspace and public issue enter the project.
Verifiers are installed by run.py after this callable returns. No oracle is read.
This calls process_work itself with a collection callback before worker cleanup.
All inference request parameters come from that product path; the adapter only
collects the resulting bytes for the harness to verify afterwards.
"""

from __future__ import annotations

import asyncio
import base64
from decimal import Decimal
import io
import json
from pathlib import Path
import shutil
import tarfile
import time
from typing import Any
from uuid import uuid4

import aiohttp

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.code_mode import process_work
from services.orchestrator.code_workspace import Workspace
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context
from services.orchestrator.project_ui import broker_identity
from services.orchestrator.chat_pipeline import retrieval_url


async def public(
    subject: str,
    org: str,
    project: str | None,
    method: str,
    tail: str,
    *,
    data: Any = None,
) -> dict[str, Any]:
    async with aiohttp.ClientSession(trust_env=False) as client:
        identity = await broker_identity(client, subject, org, project)
        url = retrieval_url() + f"/api/atlas/orgs/{org}/projects"
        if project:
            url += "/" + project
        url += tail
        kwargs: dict[str, Any] = (
            {"data": data} if isinstance(data, aiohttp.FormData) else {"json": data}
        )
        async with client.request(
            method,
            url,
            headers={
                "X-Atlas-Project-Identity": identity["identity"],
                "Idempotency-Key": str(uuid4()),
            },
            **kwargs,
        ) as response:
            result = await response.json()
            if response.status not in (200, 201):
                raise RuntimeError(
                    f"project_http_{response.status}:{result.get('error')}"
                )
            return dict(result)


def restore(raw: bytes, destination: Path, folder: str) -> None:
    """Restore only regular resulting files, stripping the project folder UUID."""
    entries: list[tuple[Path, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if not parts or parts[0] == ".git":
                continue
            if parts[0] == folder:
                parts = parts[1:]
            if not parts or member.isdir():
                continue
            path = destination.joinpath(*parts)
            if (
                not member.isfile()
                or Path(member.name).is_absolute()
                or ".." in parts
                or ".git" in parts
                or not path.resolve().is_relative_to(destination.resolve())
            ):
                raise ValueError("unsafe_work_export")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("invalid_work_export")
            entries.append((path, source.read()))
    for child in destination.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for path, content in entries:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


class WorkAdapter:
    """Sequential callable; budget includes conservative unknown reservations."""

    def __init__(
        self, subject: str, org: str, output: Path, budget: str = "3.00"
    ) -> None:
        self.subject, self.org, self.output = subject, org, output
        self.budget = Decimal(budget)
        self.profile = "atlas-qwen"
        self.spent = Decimal(0)
        self.records: list[dict[str, Any]] = []
        self.consecutive_failures = 0
        output.mkdir(parents=True, exist_ok=True)

    @property
    def stopped(self) -> bool:
        return self.consecutive_failures >= 3

    def record_outcome(self, record: dict[str, Any]) -> None:
        failed = bool(record.get("error")) or record.get("reason") == "provider_error"
        self.consecutive_failures = self.consecutive_failures + 1 if failed else 0

    def __call__(self, prompt: str, workspace: Path) -> str:
        if self.stopped:
            raise RuntimeError("three_consecutive_failures_stop")
        return asyncio.run(self.run(prompt, workspace))

    async def run(self, prompt: str, workspace: Path) -> str:
        if self.stopped:
            raise RuntimeError("three_consecutive_failures_stop")
        started = time.monotonic()
        sequence = len(self.records) + 1
        record: dict[str, Any] = {"sequence": sequence, "state": "not_run"}
        self.records.append(record)
        item: Interaction | None = None
        pid: str | None = None
        session: dict[str, Any] | None = None
        ceiling = Decimal(0)
        try:
            project = await public(
                self.subject,
                self.org,
                None,
                "POST",
                "",
                data={"name": f"Code measurement {sequence}", "default_action": "work"},
            )
            pid = project["id"]
            record["project_id"] = pid
            folder = await public(
                self.subject,
                self.org,
                pid,
                "POST",
                "/folders",
                data={
                    "name": "Source",
                    "parent_id": None,
                    "locality": "server",
                    "device_id": None,
                    "device_folder_handle": None,
                    "model_use": "allow",
                },
            )
            files = []
            for path in sorted(workspace.rglob("*")):
                if not path.is_file():
                    continue
                if path.is_symlink():
                    raise ValueError("symlink_in_agent_input")
                form = aiohttp.FormData()
                form.add_field("path", path.relative_to(workspace).as_posix())
                form.add_field("expected_parent_revision", str(folder["revision"]))
                form.add_field(
                    "file",
                    path.read_bytes(),
                    filename=path.name,
                    content_type="application/octet-stream",
                )
                uploaded = await public(
                    self.subject,
                    self.org,
                    pid,
                    "POST",
                    "/files?folder_id=" + folder["id"],
                    data=form,
                )
                files.append(uploaded["file"])
                folder = next(
                    item
                    for item in (
                        await public(self.subject, self.org, pid, "GET", "/folders")
                    )["items"]
                    if item["id"] == folder["id"]
                )
            conversation = await public(
                self.subject,
                self.org,
                pid,
                "POST",
                "/conversations",
                data={"title": "Code task"},
            )
            session = await public(
                self.subject,
                self.org,
                pid,
                "POST",
                "/work-sessions",
                data={
                    "conversation_id": conversation["id"],
                    "folder_ids": [folder["id"]],
                    "base_versions": [
                        {"file_id": f["id"], "version_id": f["version_id"]}
                        for f in files
                    ],
                },
            )
            ceiling = Decimal(str(session["effective_limits"]["cost_eur"]))
            if self.spent + ceiling > self.budget:
                raise RuntimeError("measurement_budget_exhausted")
            record["session_id"] = session["id"]
            request = ChatRequest.model_validate(
                dict(
                    model=self.profile,
                    org_id=self.org,
                    project_id=pid,
                    conversation_id=conversation["id"],
                    project_action="work",
                    work_session_id=session["id"],
                    file_version_ids=[f["version_id"] for f in files],
                    messages=[dict(role="user", content=prompt)],
                )
            )
            item = Interaction()

            async def events(event: dict[str, Any]) -> None:
                with (self.output / f"{sequence:02d}-events.jsonl").open("a") as stream:
                    stream.write(json.dumps(event) + "\n")

            async def collect(worker: Workspace) -> None:
                exported = await worker.invoke(
                    "export", {"baseline": worker.metadata()["baseline"]}, 60
                )
                if exported.get("error"):
                    raise RuntimeError("work_export_failed")
                restore(base64.b64decode(exported["archive"]), workspace, folder["id"])

            sink = sink_context.set(events)
            try:
                await process_work(
                    request,
                    item,
                    subject=self.subject,
                    org_id=self.org,
                    project_id=pid,
                    session_id=session["id"],
                    collect_workspace=collect,
                )
            finally:
                sink_context.reset(sink)
            record.update(
                state=item.state,
                verification=item.verification,
                reason="provider_error"
                if "provider_error" in item.erreurs
                else ";".join(item.erreurs),
            )
            return item.reponse
        except Exception as error:
            record["state"] = "failed"
            record["error"] = type(error).__name__
            raise RuntimeError(type(error).__name__) from None
        finally:
            if item is not None:
                record["verification"] = item.verification
            self.record_outcome(record)
            charge = ceiling if item is not None else Decimal(0)
            unknown = item is not None
            if session is not None and pid is not None:
                try:
                    current = await public(
                        self.subject,
                        self.org,
                        pid,
                        "GET",
                        "/work-sessions/" + session["id"],
                    )
                    usage = current["usage"]
                    unknown = usage["unknown_eur"] > 0 or usage["reserved_eur"] > 0
                    charge = (
                        Decimal(str(usage["cost_eur"]))
                        + Decimal(str(usage["unknown_eur"]))
                        + Decimal(str(usage["reserved_eur"]))
                    )
                    record.update(
                        usage=usage,
                        tool_calls=usage["tool_calls"],
                        model_calls=usage["model_calls"],
                    )
                except (RuntimeError, aiohttp.ClientError, TimeoutError):
                    record["usage_error"] = "usage_unavailable"
            self.spent += charge
            record.update(
                cost_eur=float(charge),
                unknown_cost=unknown,
                duration_seconds=round(time.monotonic() - started, 3),
            )
            (self.output / "usage.json").write_text(
                json.dumps(self.records, indent=2) + "\n"
            )
