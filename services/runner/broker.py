"""Persistent Linux sessions, incremental bundles and reconcilable commands."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import threading
import time
from typing import Any
from uuid import UUID

from services.orchestrator.project_forge import snapshot_files
from services.orchestrator.code_workspace import redact
from services.runner import remote_process
from services.runner.policy import RunnerError, commands, decision, guard_root
from services.runner.ssh import Keys, SSH, process

Json = dict[str, Any]


class Broker:
    def __init__(self, root: Path, keys: Keys) -> None:
        self.root, self.keys = root, keys
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()

    def probe(self, auth: Json, cached: bool = False) -> Json:
        from datetime import datetime, timezone

        ssh = self.transport(auth)
        personal = bool(auth["target"].get("owner_member_id"))
        path = ssh.root / "probe.json"
        if cached and path.exists():
            previous = json.loads(path.read_text())
            if previous["auth"]["target"] == auth["target"]:
                return dict(previous["result"])
        try:
            # A connected computer works in the folder its owner connected from.
            root = ssh.home_root() if personal and not ssh.relayed() else None
            result = ssh.probe()
            if root:
                result["work_root"] = root
        except RunnerError as exc:
            result = dict(status="unavailable", error=str(exc))
        result["timestamp"] = datetime.now(timezone.utc).isoformat()
        # A personal machine is contacted only when its owner verifies or uses
        # it: no probe record, so the periodic sweep never connects to it.
        if not personal:
            path.write_text(
                json.dumps(dict(auth=auth, checked_at=time.time(), result=result))
            )
        return result

    def scan(self, auth: Json) -> Json:
        return dict(fingerprints=self.transport(auth).scan())

    def forget(self, auth: Json) -> Json:
        """Remove a personal machine: session state, pinned host, private key."""
        with self.lock:
            org, target = str(UUID(auth["org_id"])), auth["target"]
            identifier = str(UUID(target["id"]))
            reachable = True
            for path in sorted(self.root.glob(org + "/*/*/session.json")):
                saved = json.loads(path.read_text())
                if saved.get("target", {}).get("id") != identifier:
                    continue
                if reachable:
                    try:
                        self.close(saved)
                    except RunnerError:
                        # Unreachable or already revoked by its owner: the
                        # remote directory is his to remove; stop trying.
                        reachable = False
                if path.parent.exists():
                    shutil.rmtree(path.parent)
            hosts = self.root / "hosts" / org / identifier
            if hosts.exists():
                shutil.rmtree(hosts)
            if target.get("transport") == "relay":
                from services.runner.relay import revoke

                # The relay stops registering this computer's host key.
                revoke(identifier)
            self.keys.forget(org, target["credential_ref"])
            return dict(state="forgotten", remote_cleanup=reachable)

    def remember(self, auth: Json, state: Json | None = None) -> None:
        local, _ = self.paths(auth)
        local.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = local / "session.json"
        previous = json.loads(path.read_text()) if path.exists() else dict(commands=[])
        commands = {item["id"]: item for item in previous["commands"]}
        if auth.get("command"):
            command = dict(auth["command"])
            if state:
                command.update(state)
            commands[command["id"]] = command
        value = dict(auth, commands=list(commands.values()), last_activity=time.time())
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value))
        temporary.replace(path)

    def sweep(self, now: float | None = None) -> None:
        current = time.time() if now is None else now
        for path in (self.root / "hosts").glob("*/*/probe.json"):
            previous = json.loads(path.read_text())
            if current - previous["checked_at"] >= 900:
                self.probe(previous["auth"])
        for path in self.root.glob("*/*/*/session.json"):
            auth = json.loads(path.read_text())
            if current - auth["last_activity"] < 86400:
                continue
            try:
                self.close(auth)
            except RunnerError:
                if auth.get("target", {}).get("transport") == "relay":
                    # A computer its owner disconnected: Atlas's records there
                    # are a few small files in ~/.atlas; nothing else to undo.
                    shutil.rmtree(path.parent)
                    continue
                # Keep the exact directory and command identities for the next
                # sweep; never announce successful cleanup after a lost link.
                import logging

                logging.getLogger(__name__).warning(
                    "runner_cleanup_unknown session=%s", auth["session"]["id"]
                )

    def paths(self, auth: Json) -> tuple[Path, str]:
        session = auth["session"]
        identifiers = [str(UUID(auth[k])) for k in ("org_id", "project_id")]
        identifiers += [str(UUID(session["id"]))]
        local = self.root.joinpath(*identifiers)
        if auth["target"].get("transport") == "relay":
            # A connected computer: commands run in the person's own project.
            return local, auth["target"]["work_root"]
        remote = auth["target"]["work_root"] + "/atlas-" + "-".join(identifiers)
        return local, remote

    @staticmethod
    def control(auth: Json) -> str:
        """Where a connected computer keeps Atlas's records of this session."""
        identifiers = [str(UUID(auth[k])) for k in ("org_id", "project_id")]
        identifiers += [str(UUID(auth["session"]["id"]))]
        return "~/.atlas/runs/" + "-".join(identifiers)

    def transport(self, auth: Json) -> SSH:
        return SSH(
            self.keys,
            self.root / "hosts",
            auth["org_id"],
            auth["target"],
            auth.get("jump"),
        )

    def helper(
        self, ssh: SSH, remote: str, data: Json, auth: Json | None = None
    ) -> Json:
        source = Path(remote_process.__file__).read_text()
        if ssh.relayed():
            if auth is None:
                raise RunnerError("runner_unauthorized")
            data = dict(root=remote, control=self.control(auth), in_place=True, **data)
        else:
            data = dict(root=remote + "/workspace", **data)
        result: Json = json.loads(
            ssh.run("python3 -c " + shlex.quote(source), json.dumps(data).encode())
        )
        if result.get("error") and result.get("state") not in {
            "failed",
            "timed_out",
            "cancelled",
        }:
            raise RunnerError(str(result["error"]))
        return result

    def sync(self, auth: Json, content: bytes) -> int:
        digest = auth["command"]["code_digest"]
        entries = snapshot_files(content, digest)
        if len(content) > 32 * 1024 * 1024:
            raise RunnerError("snapshot_size_limit")
        local, remote = self.paths(auth)
        ssh = self.transport(auth)
        local.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_file = local / "sync.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        # Check target disk, including caches, before transferring another bundle.
        code = (
            "import pathlib,shutil; p=pathlib.Path("
            + repr(remote)
            + "); print(sum(f.stat().st_size for f in p.rglob('*') if f.is_file() and not f.is_symlink()) if p.exists() else 0); print(shutil.disk_usage("
            + repr(auth["target"]["work_root"])
            + ").free)"
        )
        used, free = map(int, ssh.run("python3 -c " + shlex.quote(code)).split())
        if (
            used + len(content) > auth["target"]["disk_quota_bytes"]
            or free < len(content) * 2
        ):
            raise RunnerError("runner_disk_quota")
        if state.get("digest") == digest:
            return 0
        repo = local / "repository"
        repo.mkdir(exist_ok=True)

        def git(*args: str) -> bytes:
            return process(
                [
                    "git",
                    "-c",
                    "user.name=Atlas",
                    "-c",
                    "user.email=runner@atlas.invalid",
                    "-c",
                    "core.hooksPath=/dev/null",
                    *args,
                ],
                cwd=repo,
            )

        if not (repo / ".git").exists():
            git("init", "-b", "atlas")
        for child in repo.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        for entry in entries:
            path = repo / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(entry["content"], validate=True))
            path.chmod(0o755 if entry["mode"] == "100755" else 0o644)
        git("add", "--all")
        git("commit", "--allow-empty", "-m", "Scratch " + digest)
        head = git("rev-parse", "HEAD").decode().strip()
        bundle = local / "sync.bundle"
        revisions = ["atlas"] + (["^" + state["head"]] if state.get("head") else [])
        git("bundle", "create", str(bundle.resolve()), *revisions)
        ssh.run("mkdir -p " + shlex.quote(remote))
        ssh.copy(bundle, remote + "/sync.bundle")
        # Hard reset updates tracked source only; caches/untracked artifacts remain.
        if state.get("head"):
            command = (
                "git -C "
                + shlex.quote(remote + "/workspace")
                + " -c core.hooksPath=/dev/null fetch "
                + shlex.quote(remote + "/sync.bundle")
                + " atlas && git -C "
                + shlex.quote(remote + "/workspace")
                + " -c core.hooksPath=/dev/null reset --hard FETCH_HEAD"
            )
        else:
            command = (
                "git -c core.hooksPath=/dev/null clone -b atlas "
                + shlex.quote(remote + "/sync.bundle")
                + " "
                + shlex.quote(remote + "/workspace")
            )
        ssh.run(command + " && rm -- " + shlex.quote(remote + "/sync.bundle"))
        state_file.write_text(json.dumps(dict(digest=digest, head=head)))
        size = bundle.stat().st_size
        bundle.unlink()
        return size

    @staticmethod
    def written(auth: Json, content: bytes) -> Json | None:
        """The one file an `atlas-write PATH` command writes, from its snapshot.

        Only a connected computer has files of its own to write; the snapshot
        holds exactly that file, already bound to the command by its digest.
        """
        argv = commands(auth["command"]["command"])
        if argv[0][0] != "atlas-write":
            return None
        if auth["target"].get("transport") != "relay" or len(argv) != 1:
            raise RunnerError("unsupported_target")
        if len(argv[0]) != 2:
            raise RunnerError("invalid_command")
        entries = snapshot_files(content, auth["command"]["code_digest"])
        if len(entries) != 1 or entries[0]["path"] != argv[0][1]:
            raise RunnerError("invalid_command")
        return dict(path=entries[0]["path"], content=entries[0]["content"])

    def authorize(self, auth: Json) -> None:
        from datetime import datetime, timezone
        from services.runner.contract import RemoteRun

        command, target, grant, session = (
            auth["command"],
            auth["target"],
            auth["grant"],
            auth["session"],
        )
        RemoteRun.model_validate(
            dict(
                target=target["id"],
                command=command["command"],
                cwd=command["cwd"],
                timeout_s=command["timeout_s"],
            )
        )
        owner = target.get("owner_member_id")
        if owner is not None and command.get("member_id") != owner:
            # A personal machine runs commands of its owner only, whatever the
            # caller presents as a grant.
            raise RunnerError("runner_unauthorized")
        if (
            grant.get("target_id", target["id"]) != target["id"]
            or grant.get("project_id", auth["project_id"]) != auth["project_id"]
            or not grant.get("enabled", True)
            or session.get("target_id", target["id"]) != target["id"]
            or command.get("runner_session_id", session["id"]) != session["id"]
        ):
            raise RunnerError("runner_unauthorized")
        if command["decision"] == "approved":
            approval = auth.get("approval", {})
            remote = approval.get("remote", {})
            if (
                approval.get("decision") != "allowed"
                or not approval.get("consumed_at")
                or datetime.fromisoformat(approval["expires_at"])
                <= datetime.now(timezone.utc)
                or remote.get("command") != command["command"]
                or remote.get("code_digest") != command["code_digest"]
                or remote.get("target_id") != target["id"]
                or approval.get("requested_by_member_id") != command["member_id"]
                or approval.get("session_id") != command["work_session_id"]
            ):
                raise RunnerError("approval_required")

    def run(self, auth: Json, content: bytes) -> Json:
        with self.lock:
            self.authorize(auth)
            target, command = auth["target"], auth["command"]
            outcome = decision(
                command["command"],
                target["permission_mode"],
                target["allow_rules"],
                target["deny_rules"],
                target["sensitive_rules"],
                auth["grant"]["allow_rules"],
                guard_root(target),
            )
            if outcome == "denied" or not auth["grant"]["code_egress_approved_at"]:
                raise RunnerError("runner_policy_denied")
            if outcome == "awaiting_approval" and command["decision"] != "approved":
                raise RunnerError("approval_required")
            local, remote = self.paths(auth)
            checkpoint = local / (command["id"] + ".json")
            if checkpoint.exists():
                return self.status(auth)
            written = self.written(auth, content)
            self.remember(auth)
            # A connected computer works in its owner's project: nothing to sync.
            relayed = target.get("transport") == "relay"
            sent = (
                len(content)
                if written is not None
                else 0
                if relayed
                else self.sync(auth, content)
            )
            self.authorize(auth)
            # Intent is durable before launch. A network failure never resubmits.
            checkpoint.write_text(json.dumps(dict(state="unknown", bytes_sent=sent)))
            launch = dict(
                operation="launch",
                command_id=command["id"],
                command=command["command"],
                cwd=command["cwd"],
                timeout_s=command["timeout_s"],
                disk_quota_bytes=target["disk_quota_bytes"],
            )
            if written is not None:
                launch["write"] = written
            value = self.helper(self.transport(auth), remote, launch, auth)
            return dict(
                value,
                bytes_sent=sent,
                synced_digest=hashlib.sha256(content).hexdigest(),
            )

    def status(self, auth: Json, cancel: bool = False, log: bool = False) -> Json:
        local, remote = self.paths(auth)
        value = self.helper(
            self.transport(auth),
            remote,
            dict(
                operation="cancel" if cancel else "log" if log else "status",
                command_id=auth["command"]["id"],
            ),
            auth,
        )
        checkpoint = local / (auth["command"]["id"] + ".json")
        value["bytes_sent"] = (
            json.loads(checkpoint.read_text()).get("bytes_sent", 0)
            if checkpoint.exists()
            else 0
        )
        value["duration_s"] = max(
            0, value.get("ended_at", time.time()) - value.get("started_at", time.time())
        )
        if "output" in value:
            value["output"] = redact(value["output"])
        if "content" in value:
            text = base64.b64decode(value["content"]).decode("utf-8", errors="replace")
            value["content"] = base64.b64encode(str(redact(text)).encode()).decode()
            from services.runner.results import report

            reports = self.helper(
                self.transport(auth), remote, dict(operation="reports"), auth
            )
            value["reports"] = [
                dict(
                    name=item["name"],
                    **report(base64.b64decode(item["content"]), item["name"]),
                )
                for item in reports["reports"]
            ]
            value["bytes_received"] += reports["bytes_received"]
            if text.startswith("TAP version") or "\n1.." in text:
                value["reports"].append(report(text.encode(), "output.tap"))
        with self.lock:
            self.remember(auth, value)
        return value

    def fetch(self, auth: Json, pattern: str) -> Json:
        _, remote = self.paths(auth)
        return self.helper(
            self.transport(auth),
            remote,
            dict(operation="fetch", path_or_glob=pattern),
            auth,
        )

    def close(self, auth: Json) -> Json:
        with self.lock:
            local, remote = self.paths(auth)
            # Authorization supplies all current commands; settle each before removal.
            for command in auth.get("commands", []):
                if command["state"] in {"running", "unknown"}:
                    bound = dict(auth, command=command)
                    self.status(bound, cancel=True)
                    for _ in range(20):
                        if self.status(bound)["state"] not in {"running", "unknown"}:
                            break
                        time.sleep(0.1)
                    else:
                        raise RunnerError("runner_cleanup_unknown")
            if auth["target"].get("transport") == "relay":
                # Only Atlas's own records: the project is the person's.
                self.transport(auth).run(
                    'rm -rf -- "$HOME"/' + shlex.quote(self.control(auth)[2:])
                )
            else:
                self.transport(auth).run("rm -rf -- " + shlex.quote(remote))
            if local.exists():
                shutil.rmtree(local)
            return dict(state="closed")
