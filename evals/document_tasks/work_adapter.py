"""Trusted HTTP adapter for the existing Project Work transport, with boundary capture.

The deployment's observer is an operator command, not an agent tool. It prepares
scenario state and captures worker/provider boundaries unavailable through the
public API. Missing observations fail closed; this adapter never invents them.
Both modes call the same Work loop; recorded mode uses a recorded gateway.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

import aiohttp

from evals.document_tasks.formats import digest, json_bytes, safe_path
from services.retrieval.project_work_contract import matches, schemas


def denial_leaked_bytes(status: int, raw: bytes) -> int:
    """A denial status does not excuse content-bearing or non-contract bodies."""
    if status not in {403, 404}:
        return len(raw)
    if not raw:
        return 0
    try:
        error = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return len(raw)
    if not matches(error, schemas()["Error"]):
        return len(raw)
    expected = "not_found" if status == 404 else "permission_denied"
    safe_message = error["message"].strip().casefold().rstrip(".")
    return (
        0
        if error["error"] == expected and safe_message == expected.replace("_", " ")
        else len(raw)
    )


class ProjectWorkRunner:
    def __init__(
        self, endpoint: str, token: str, binding: dict[str, Any], observer: list[str]
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port in {8000, 8020, 3000, 5432}
        ):
            raise ValueError("invalid_or_prohibited_evaluation_endpoint")
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.binding = binding
        self.observer = observer
        self.org = str(UUID(binding["org_id"]))
        self.project = str(UUID(binding["project_id"]))
        self.session = str(UUID(binding["session_id"]))
        self.conversation = str(UUID(binding["conversation_id"]))
        self.base = f"/api/atlas/orgs/{self.org}/projects/{self.project}"
        self.client: aiohttp.ClientSession
        self.http: list[dict[str, Any]] = []
        self.approval_attempts: list[dict[str, Any]] = []
        self.cancelled = False
        self.cancel_triggered = False
        self.sequences: dict[str, int] = {}
        self.event_pages: list[list[dict[str, Any]]] = []
        self.request_id: str | None = None

    async def request(
        self,
        method: str,
        path: str,
        data: Any = None,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        if (
            not path.startswith("/api/atlas/")
            or ".." in path
            or "?" in path.split("#")[0]
            and "#" in path
        ):
            raise ValueError("invalid_scoped_api_path")
        supplied = {
            "Authorization": "Bearer " + (self.token if token is None else token),
            "Idempotency-Key": str(uuid4()),
            **(headers or {}),
        }
        async with self.client.request(
            method,
            self.endpoint + path,
            json=data,
            headers=supplied,
            allow_redirects=False,
        ) as response:
            buffer = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                buffer.extend(chunk)
                if len(buffer) > 32 * 1024 * 1024:
                    raise ValueError("oversized_api_response")
            raw = bytes(buffer)
            self.http.append(
                dict(
                    method=method,
                    path=path,
                    status=response.status,
                    response_sha256=digest(raw),
                    response_bytes=len(raw),
                )
            )
            return response.status, raw

    async def get_json(self, tail: str) -> dict[str, Any]:
        status, raw = await self.request("GET", self.base + "/" + tail)
        if status != 200:
            raise ValueError("project_api_read_failed:" + str(status))
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("invalid_api_json")
        return value

    async def items(self, tail: str) -> list[dict[str, Any]]:
        result = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(100):
            data = await self.get_json(
                tail
                + ("&" if "?" in tail else "?")
                + "cursor="
                + quote(cursor, safe="")
                if cursor
                else tail
            )
            result.extend(data["items"])
            cursor = data["next_cursor"]
            if cursor is None:
                return result
            if cursor in seen:
                raise ValueError("pagination_cycle")
            seen.add(cursor)
        raise ValueError("pagination_limit")

    async def observe(
        self, phase: str, public: dict[str, Any], output: Path
    ) -> dict[str, Any]:
        # Secrets remain in the inherited process environment; never argv/stdout.
        request = dict(
            phase=phase,
            task=public,
            binding=self.binding,
            http=self.http,
            approval_attempts=self.approval_attempts,
            cancel_requested=self.cancelled,
            event_pages=self.event_pages,
            displayed_events=sorted(self.sequences, key=lambda k: self.sequences[k]),
        )
        process = await asyncio.create_subprocess_exec(
            *self.observer,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(json_bytes(request)), 60
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError("observer_timeout") from None
        if process.returncode != 0 or len(stdout) > 16 * 1024 * 1024:
            raise ValueError("observer_failed")
        value = json.loads(stdout)
        if not isinstance(value, dict) or value.get("phase") != phase:
            raise ValueError("observer_contract")
        (output / ("observer-" + phase + ".json")).write_bytes(json_bytes(value))
        return value

    async def snapshot(
        self, public: dict[str, Any], *, current: bool
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        files = await self.items("files")
        by_id = {f["id"]: f for f in files}
        for entry in public["input_manifest"]:
            bound = self.binding["inputs"][entry["path"]]
            file_id, version = (
                str(UUID(bound["file_id"])),
                str(UUID(bound["version_id"])),
            )
            metadata = by_id.get(file_id)
            if metadata is None:
                result[entry["path"]] = "deleted"
                continue
            if current:
                version = metadata["version_id"]
            status, raw = await self.request(
                "GET", self.base + f"/files/{file_id}/content?version={version}"
            )
            if status != 200:
                result[entry["path"]] = "unavailable:" + str(status)
                continue
            name = metadata["relative_path"] if current else entry["path"]
            result[name] = digest(raw)
        return result

    def events(self, raw: str) -> list[dict[str, Any]]:
        result = []
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            value = json.loads(line[6:])
            seq, event_id = (
                value["seq"],
                value.get("event_id", "progress:" + str(value["seq"])),
            )
            if event_id in self.sequences and self.sequences[event_id] != seq:
                raise ValueError("conflicting_replay")
            self.sequences[event_id] = seq
            result.append(dict(seq=seq, event_id=event_id))
        return result

    async def cancellation(self) -> None:
        state = await self.get_json("work-sessions/" + self.session)
        lease = state.get("lease")
        if not lease or not lease.get("request_id"):
            raise ValueError("cannot_cancel_without_active_request")
        self.request_id = lease["request_id"]
        status, _ = await self.request(
            "POST",
            self.base + f"/work-sessions/{self.session}/cancel",
            dict(request_id=self.request_id),
        )
        if status != 202:
            raise ValueError("cancel_rejected")
        self.cancelled = True

    async def monitor(self, identifier: str, done: asyncio.Event) -> None:
        handled: set[str] = set()
        while not done.is_set():
            if identifier == "D28":
                state = await self.get_json("work-sessions/" + self.session)
                pending = [
                    a for a in state["pending_approvals"] if a["id"] not in handled
                ]
                for approval in pending:
                    action = "deny" if not handled else "expire"
                    # Wait for genuine expiry, never adjust production time or TTL.
                    if action == "expire":
                        expires = datetime.fromisoformat(
                            approval["expires_at"].replace("Z", "+00:00")
                        )
                        if datetime.now(timezone.utc) <= expires:
                            continue
                    decision = dict(
                        allow=action == "expire",
                        action_digest=approval["action_digest"],
                    )
                    status, raw = await self.request(
                        "POST",
                        self.base + f"/approvals/{approval['id']}/decision",
                        decision,
                    )
                    replay_status, replay = await self.request(
                        "POST",
                        self.base + f"/approvals/{approval['id']}/decision",
                        decision,
                    )
                    self.approval_attempts.append(
                        dict(
                            id=approval["id"],
                            decision=action,
                            status=status,
                            response=json.loads(raw),
                            replay_status=replay_status,
                            replay_response=json.loads(replay),
                        )
                    )
                    handled.add(approval["id"])
            if identifier == "D30" and not self.cancelled:
                status, raw = await self.request(
                    "GET", self.base + f"/events?session_id={self.session}&after=0"
                )
                if status != 200:
                    raise ValueError("event_read_failed")
                for line in raw.decode().splitlines():
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        # The observer confirms this was production, not just read.
                        if (
                            event.get("type") == "tool"
                            and event.get("status") == "running"
                            and event.get("tool")
                            in {
                                "publish_document",
                                "write_file",
                                "run_command",
                                "convert_document",
                            }
                        ):
                            self.cancel_triggered = True
                            await self.cancellation()
                            break
            try:
                await asyncio.wait_for(done.wait(), 0.2)
            except TimeoutError:
                continue

    async def foreign_probes(self) -> list[dict[str, Any]]:
        foreign = self.binding["foreign"]
        token = os.environ[foreign["owner_token_env"]]
        other = f"/api/atlas/orgs/{UUID(foreign['org_id'])}/projects/{UUID(foreign['project_id'])}"
        if other == self.base:
            raise ValueError("foreign_scope_must_differ")
        paths = dict(
            file=f"files/{UUID(foreign['file_id'])}/content",
            citation="sources/" + safe_path(foreign["chunk_id"]),
            session=f"work-sessions/{UUID(foreign['session_id'])}",
            ledger="ledger?session_id=" + str(UUID(foreign["session_id"])),
            download=f"deliverables/{UUID(foreign['deliverable_id'])}/download",
        )
        probes = []
        for resource, tail in paths.items():
            owner_status, owner_raw = await self.request(
                "GET", other + "/" + tail, token=token
            )
            for addressing, base in (
                ("own_project", self.base),
                ("foreign_project", other),
            ):
                status, raw = await self.request("GET", base + "/" + tail)
                probes.append(
                    dict(
                        resource=resource,
                        addressing=addressing,
                        status=status,
                        owner_status=owner_status,
                        target_exists=owner_status == 200 and bool(owner_raw),
                        body_sha256=digest(raw),
                        leaked_bytes=denial_leaked_bytes(status, raw),
                    )
                )
        return probes

    def work_request(self, public: dict[str, Any]) -> dict[str, Any]:
        context = {"inputs": self.binding["inputs"]}
        if "foreign" in self.binding:
            context["foreign"] = {
                key: value
                for key, value in self.binding["foreign"].items()
                if key != "owner_token_env"
            }
        for key in ("local_folder_id", "memory_id", "stale_turn_id"):
            if key in self.binding:
                context[key] = self.binding[key]
        prompt = (
            public["prompt"]
            + "\n\nScoped resource bindings:\n"
            + json.dumps(context, sort_keys=True)
        )
        return dict(
            model=self.binding["profile"],
            org_id=self.org,
            project_id=self.project,
            conversation_id=self.conversation,
            project_action="work",
            work_session_id=self.session,
            messages=[dict(role="user", content=prompt)],
            stream=True,
        )

    async def run(
        self, public: dict[str, Any], inputs: Path, output: Path, mode: str
    ) -> None:
        timeout = aiohttp.ClientTimeout(total=public["limits"]["wall_seconds"])
        async with aiohttp.ClientSession(timeout=timeout) as client:
            self.client = client
            if set(self.binding["inputs"]) != {
                i["path"] for i in public["input_manifest"]
            }:
                raise ValueError("binding_input_manifest_mismatch")
            for item in public["input_manifest"]:
                if (
                    digest((inputs / safe_path(item["path"])).read_bytes())
                    != item["sha256"]
                ):
                    raise ValueError("public_input_changed")
            prepared = await self.observe("prepare", public, output)
            if prepared.get("ready") is not True:
                raise ValueError("observer_scenario_not_ready")
            before = await self.snapshot(public, current=False)
            if before != {e["path"]: e["sha256"] for e in public["input_manifest"]}:
                raise ValueError("remote_inputs_do_not_match_frozen_fixture")
            session = await self.get_json("work-sessions/" + self.session)
            if (
                session["conversation_id"] != self.conversation
                or session["state"] != "ready"
            ):
                raise ValueError("fresh_work_session_required")
            if any(
                session["effective_limits"][k] > v for k, v in public["limits"].items()
            ):
                raise ValueError("deployment_limits_exceed_frozen_task_limits")
            old_deliverables = {d["id"] for d in await self.items("deliverables")}
            if old_deliverables:
                raise ValueError("fresh_project_without_deliverables_required")
            done = asyncio.Event()
            monitor = asyncio.create_task(self.monitor(public["id"], done))
            # Only public text and scoped UUIDs enter the Work request. There is no
            # oracle, verifier path, private expectation or observer command here.
            body = self.work_request(public)
            chunks = []
            started = time.monotonic()
            try:
                async with client.post(
                    self.endpoint + "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": "Bearer " + self.token},
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise ValueError(
                            "work_transport_rejected:" + str(response.status)
                        )
                    while line := await response.content.readline():
                        if len(line) > 262144:
                            raise ValueError("oversized_stream_event")
                        if monitor.done() and monitor.exception():
                            raise ValueError(
                                "scenario_monitor_failed"
                            ) from monitor.exception()
                        if (
                            line.startswith(b"data: ")
                            and line.strip() != b"data: [DONE]"
                        ):
                            event = json.loads(line[6:])
                            for choice in event.get("choices", []):
                                content = choice.get("delta", {}).get("content")
                                if isinstance(content, str):
                                    chunks.append(content)
            finally:
                done.set()
                await monitor
            claim = "".join(chunks)
            (output / "answer.txt").write_text(claim)
            try:
                # An optional single JSON fence is presentation, not content.
                candidate = claim.strip()
                if candidate.startswith("```json\n") and candidate.endswith("```"):
                    candidate = candidate[8:-3].strip()
                value = json.loads(candidate)
                (output / "answer.json").write_bytes(json_bytes(value))
            except json.JSONDecodeError:
                pass  # Preserve the raw response; a required JSON result then fails.
            publications = []
            files = {f["version_id"]: f for f in await self.items("files")}
            for deliverable in await self.items("deliverables"):
                if deliverable["id"] in old_deliverables:
                    continue
                file = files[deliverable["file_version_id"]]
                name = safe_path(file["relative_path"])
                status, raw = await self.request(
                    "GET", self.base + f"/deliverables/{deliverable['id']}/download"
                )
                if (
                    status != 200
                    or digest(raw) != file["sha256"]
                    or len(raw) != file["size_bytes"]
                ):
                    raise ValueError("deliverable_download_mismatch")
                destination = output / "artifacts" / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
                publications.append(
                    dict(path=name, sha256=digest(raw), size_bytes=len(raw))
                )
            after = await self.snapshot(public, current=True)
            extra: dict[str, Any] = {}
            if public["id"] == "D24":
                extra["stale_turn_id"] = str(UUID(self.binding["stale_turn_id"]))
            if public["id"] == "D27":
                extra["probes"] = await self.foreign_probes()
            if public["id"] == "D29":
                status, raw = await self.request(
                    "POST",
                    self.base + "/local-reads",
                    dict(
                        folder_id=str(UUID(self.binding["local_folder_id"])),
                        file_handle="opaque-local-handle",
                        range=None,
                        purpose="document evaluation",
                    ),
                )
                extra["local_read"] = dict(
                    status=status, error=json.loads(raw).get("error")
                )
            if public["id"] == "D30":
                for _ in range(2):
                    status, raw = await self.request(
                        "GET", self.base + f"/events?session_id={self.session}&after=0"
                    )
                    if status != 200:
                        raise ValueError("reload_failed")
                    self.event_pages.append(self.events(raw.decode()))
                extra.update(
                    event_pages=self.event_pages,
                    displayed_events=sorted(
                        self.sequences, key=lambda k: self.sequences[k]
                    ),
                )
            observed = await self.observe("collect", public, output)
            observations = dict(
                observed["observations"],
                **extra,
                origin="trusted-work-capture",
                before=before,
                after=after,
                publications=publications,
            )
            (output / "observations.json").write_bytes(json_bytes(observations))
            session = await self.get_json("work-sessions/" + self.session)
            ledger = await self.items("ledger")
            summary = await self.get_json("ledger/summary?session_id=" + self.session)
            if observed["provider_mode"] != mode:
                raise ValueError("provider_mode_mismatch")
            capture = dict(
                origin="project-work",
                task_id=public["id"],
                base_archive_sha256=public["base_archive_sha256"],
                mode=mode,
                session_id=self.session,
                final_claim=claim,
                usage=session["usage"],
                ledger=ledger,
                ledger_summary=summary,
                calls=observed["calls"],
                resource_maxima=observed["resource_maxima"],
                provider_completion_ids=observed["provider_completion_ids"],
                recording_sha256=observed["recording_sha256"],
                http=self.http,
                elapsed_seconds=time.monotonic() - started,
            )
            (output / "capture.json").write_bytes(json_bytes(capture))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token-env", default="ATLAS_DOCUMENT_EVAL_TOKEN")
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument(
        "--observer-command",
        type=Path,
        required=True,
        help="JSON argv array for deployment-owned boundary observer",
    )
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("recorded", "live"), required=True)
    args = parser.parse_args()
    public = json.loads(args.task.read_text())
    bindings = json.loads(args.bindings.read_text())
    command = json.loads(args.observer_command.read_text())
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(s, str) for s in command)
    ):
        raise ValueError("invalid_observer_command")
    runner = ProjectWorkRunner(
        args.endpoint, os.environ[args.token_env], bindings[public["id"]], command
    )
    asyncio.run(runner.run(public, args.inputs, args.output, args.mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
