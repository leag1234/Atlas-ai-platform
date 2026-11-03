"""Authenticated calls to the contained user-file service, never to arbitrary URLs."""

from packages.limits import LimitError

import asyncio
import base64
from dataclasses import dataclass
import json
import os
from pathlib import PurePosixPath
import re
import unicodedata
from typing import Any, Literal
from urllib.parse import quote

import aiohttp
from packages.ports import atlas_ports
from pydantic import BaseModel, ConfigDict, Field

from services.orchestrator.documents import Attachment, MAX_BYTES


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command: str = Field(min_length=1, max_length=15000)


class Publish(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, max_length=1000)
    strategy: Literal["created", "regenerated", "edited_in_place"]


def safe_result(value: dict[str, object]) -> dict[str, object]:
    key = os.environ.get("OPEN_TERMINAL_API_KEY", "")

    def scrub(item: object) -> object:
        if isinstance(item, str):
            return item.replace(key, "[REDACTED]") if key else item
        if isinstance(item, list):
            return [scrub(part) for part in item]
        if isinstance(item, dict):
            return {name: scrub(part) for name, part in item.items()}
        return item

    return {name: scrub(part) for name, part in value.items()}


def output_path(root: str, path: str) -> str:
    target = PurePosixPath(path)
    if not target.is_absolute():
        target = PurePosixPath(root) / target
    if (
        not target.is_relative_to(root)
        or ".." in target.parts
        or any(part.startswith(".") for part in target.parts)
        or target.suffix.lower()
        not in {".docx", ".xlsx", ".pptx", ".pdf", ".csv", ".md", ".png"}
    ):
        raise ValueError("output_must_be_document_in_request_directory")
    return str(target)


def safe_filename(name: str) -> str:
    """ASCII name the terminal stores unchanged (multipart encodes spaces and accents)."""
    base = PurePosixPath(name.replace("\\", "/")).name
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""

    def clean(part: str) -> str:
        part = unicodedata.normalize("NFKD", part).encode("ascii", "ignore").decode()
        part = re.sub(r"[^A-Za-z0-9._-]+", "_", part)
        return re.sub(r"_+", "_", part).strip("._-")

    stem, ext = clean(stem)[:120], clean(ext)[:16]
    stem = stem or "attachment"
    return f"{stem}.{ext}" if ext else stem


EXECUTIONS = asyncio.Semaphore(8)


class TerminalClient:
    def __init__(self, request_id: str) -> None:
        if not request_id.replace("-", "").isalnum():
            raise ValueError("invalid_request_id")
        if os.environ.get("ATLAS_TERMINAL_ENABLED") != "1":
            raise RuntimeError("terminal_unavailable")
        self.root = "/home/user/requests/" + request_id
        self.ready = False
        self.commands = 0
        self.failed_commands = 0
        self.files: list[str] = []
        self.strategies: list[str] = []

    async def request(
        self,
        method: str,
        path: str,
        timeout: float,
        *,
        payload: object = None,
        params: dict[str, str] | None = None,
        data: aiohttp.FormData | None = None,
    ) -> dict[str, object]:
        key = os.environ.get("OPEN_TERMINAL_API_KEY", "")
        if not key:
            raise RuntimeError("terminal_configuration_missing")
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=max(0.1, timeout)),
            trust_env=False,
            headers={"Authorization": "Bearer " + key},
        ) as client:
            async with client.request(
                method,
                atlas_ports().url("terminal") + path,
                json=payload,
                data=data,
                params=params,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise RuntimeError("terminal_http_" + str(response.status))
                raw = bytearray()
                async for part in response.content.iter_chunked(16384):
                    raw.extend(part)
                    if len(raw) > 8 * 1024 * 1024:
                        raise LimitError(
                            "terminal_response_limit", len(raw), 8388608, "bytes"
                        )
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("invalid_terminal_response")
        return safe_result(value)

    async def upload(self, attachment: Attachment, timeout: float) -> str:
        encoded = attachment.file_data
        if encoded.startswith("data:"):
            header, _, encoded = encoded.partition(",")
            if not header.endswith(";base64"):
                raise ValueError("invalid_attachment_encoding")
        raw = base64.b64decode(encoded, validate=True)
        if not 0 < len(raw) <= MAX_BYTES:
            raise ValueError(f"Attachment bytes {len(raw)}; limit {MAX_BYTES}")
        data = aiohttp.FormData()
        data.add_field(
            "file",
            raw,
            filename=safe_filename(attachment.filename),
            content_type="application/octet-stream",
        )
        result = await self.request(
            "POST", "/files/upload", timeout, params={"directory": self.root}, data=data
        )
        expected = self.root + "/" + safe_filename(attachment.filename)
        if result.get("path") != expected or result.get("size") != len(raw):
            raise ValueError("terminal_upload_integrity_failed")
        return expected

    async def upload_generated(
        self,
        filename: str,
        content: bytes,
        timeout: float,
        *,
        max_bytes: int = MAX_BYTES,
    ) -> str:
        """Place trusted, locally verified bytes in the existing user-file area."""
        if not 0 < len(content) <= max_bytes:
            raise ValueError(f"Generated bytes {len(content)}; limit {max_bytes}")
        name = safe_filename(filename)
        data = aiohttp.FormData()
        data.add_field(
            "file", content, filename=name, content_type="application/octet-stream"
        )
        result = await self.request(
            "POST", "/files/upload", timeout, params={"directory": self.root}, data=data
        )
        expected = self.root + "/" + name
        if result.get("path") != expected or result.get("size") != len(content):
            raise ValueError("terminal_generated_upload_integrity_failed")
        return expected

    async def initialize(self) -> None:
        await self.request(
            "POST",
            "/files/write",
            5,
            payload={"path": self.root + "/.request", "content": "ATLAS user files"},
        )

    async def publish(
        self, path: str, strategy: str, timeout: float
    ) -> dict[str, object]:
        target = output_path(self.root, path)
        # Reject symlinks before publication; Docker remains the security boundary.
        import shlex

        code = (
            "from pathlib import Path; p=Path("
            + repr(target)
            + "); assert p.is_file() and not p.is_symlink() and p.stat().st_size > 0; print(p.stat().st_size)"
        )
        result = await self.execute("python3 -c " + shlex.quote(code), timeout)
        if result.get("exit_code") != 0:
            return {"error": "output_file_missing_or_invalid"}
        url = "/api/v1/terminals/atlas-files/files/serve/" + quote(
            target.lstrip("/"), safe="/"
        )
        self.files.append(url)
        self.strategies.append(strategy)
        return {
            "download_url": url,
            "filename": PurePosixPath(target).name,
            "strategy": strategy,
        }

    def isolation_arguments(self, name: str) -> list[str]:
        """Throwaway container: no network, no capabilities, only this request's directory."""
        from services.orchestrator.code_workspace import enabled, volume

        ports = atlas_ports()
        subpath = "requests/" + self.root.rsplit("/", 1)[1]
        mounts = [
            "type=volume,src=%s,dst=%s,volume-subpath=%s"
            % (ports.name("atlas-terminal-files"), self.root, subpath)
        ]
        if enabled():
            mounts.append("type=volume,src=%s,dst=/code-workspaces,readonly" % volume())
        arguments = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--label",
            "atlas.owner=exec",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--read-only",
            "--user",
            "1000:1000",
            "--memory",
            "2g",
            "--memory-swap",
            "2g",
            "--cpus",
            "2",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m,uid=1000,gid=1000",
            "-e",
            "HOME=/tmp",
            "-w",
            self.root,
        ]
        for mount in mounts:
            arguments += ["--mount", mount]
        from services.orchestrator.terminal_stack import IMAGE

        return arguments + ["--entrypoint", "/bin/bash", IMAGE, "-c", "--"]

    async def isolated(self, command: str, timeout: float) -> dict[str, object]:
        """Run one command in its own container, so it cannot see other requests' files."""
        import uuid

        if not self.ready:
            await self.initialize()
            self.ready = True
        name = "atlas-exec-" + uuid.uuid4().hex
        limit = 1024 * 1024
        async with EXECUTIONS:
            process = await asyncio.create_subprocess_exec(
                *self.isolation_arguments(name),
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            raw = bytearray()

            async def collect() -> None:
                assert process.stdout is not None
                while chunk := await process.stdout.read(16384):
                    if len(raw) <= limit:
                        raw.extend(chunk)
                await process.wait()

            try:
                await asyncio.wait_for(collect(), timeout)
            except asyncio.TimeoutError:
                return {"status": "timeout"}
            finally:
                if process.returncode is None:
                    kill = await asyncio.create_subprocess_exec(
                        "docker",
                        "rm",
                        "-f",
                        name,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.shield(kill.wait())
                    await asyncio.shield(process.wait())
        text = bytes(raw[:limit]).decode("utf-8", "replace")
        return safe_result(
            {
                "status": "done",
                "exit_code": process.returncode,
                "output": [{"type": "output", "data": text}],
                "truncated": len(raw) > limit,
            }
        )

    async def execute(self, command: str, timeout: float) -> dict[str, object]:
        self.commands += 1
        result = await self.isolated(command, min(timeout, 30))
        if result.get("status") != "done":
            self.failed_commands += 1
            return {
                "error": "terminal_command_timeout",
                "limit_seconds": min(timeout, 30),
            }
        if result.get("exit_code") != 0:
            self.failed_commands += 1
        if result.get("truncated"):
            return {
                "error": "terminal_output_truncated",
                "detail": "The terminal discarded output. Read the complete document in bounded sections; do not summarize these fragments as the whole.",
            }
        return {
            k: result[k]
            for k in ("status", "exit_code", "output", "truncated")
            if k in result
        }


@dataclass(frozen=True)
class VerifiedDocument:
    """Bytes returned by the bounded renderer/verifier, never a model assertion."""

    content: bytes
    preview: bytes
    sha256: str


class ProjectPublisher:
    """Private publication with fresh scoped authority; no temporary terminal URL."""

    def __init__(self, execution: Any) -> None:
        self.execution = execution

    async def publish(
        self,
        document: VerifiedDocument,
        name: str,
        strategy: str,
        source_version_ids: list[str],
        expected_parent_revision: int,
        key: str,
        timeout: float,
    ) -> dict[str, Any]:
        import hashlib
        from services.orchestrator.chat_pipeline import retrieval_url
        from services.orchestrator.project_ui import broker_identity, bounded_json
        from services.retrieval.project_identity import ProjectError

        await self.execution.check()
        secret = os.environ.get("ATLAS_PROJECT_EXECUTION_SECRET", "")
        if len(secret.encode()) < 32:
            raise ProjectError(503, "dependency_unavailable")
        if hashlib.sha256(document.content).hexdigest() != document.sha256:
            raise ValueError("Verified bytes changed before publication")
        broker = self.execution.broker
        metadata = dict(
            self.execution.fence(),
            turn_id=self.execution.request_id,
            path=name,
            expected_parent_revision=expected_parent_revision,
            source_version_ids=source_version_ids,
            strategy=strategy,
            validation=dict(
                status="passed",
                verifier_exit=0,
                checks=["package_readable", "requested_content", "source_coverage"],
                warnings=[],
                artifact_sha256=document.sha256,
            ),
        )
        form = aiohttp.FormData()
        form.add_field(
            "metadata", json.dumps(metadata), content_type="application/json"
        )
        form.add_field(
            "file",
            document.content,
            filename="artifact",
            content_type="application/octet-stream",
        )
        form.add_field(
            "preview", document.preview, filename="preview", content_type="text/plain"
        )
        try:
            async with aiohttp.ClientSession(
                trust_env=False, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as client:
                identity = await broker_identity(
                    client, broker.subject, broker.org, broker.project
                )
                async with client.post(
                    retrieval_url() + "/internal/atlas/execution/publish",
                    data=form,
                    headers={
                        "X-Atlas-Project-Identity": str(identity["identity"]),
                        "X-Atlas-Execution-Secret": secret,
                        "X-Atlas-Execution-Audience": "atlas-retrieval-execution",
                        "Idempotency-Key": key,
                    },
                    allow_redirects=False,
                ) as response:
                    result = await bounded_json(response)
                    if response.status != 201:
                        error = ProjectError(
                            response.status,
                            str(result.get("error", "dependency_unavailable")),
                        )
                        error.body = result
                        raise error
        except (aiohttp.ClientError, TimeoutError):
            raise ProjectError(503, "dependency_unavailable") from None
        from services.orchestrator.file_results import project_deliverable_links

        project_deliverable_links(result, broker.org, broker.project)
        if result.get("sha256") != document.sha256 or result.get("size_bytes") != len(
            document.content
        ):
            raise ProjectError(503, "dependency_unavailable")
        return dict(result)
