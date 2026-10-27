"""REQ-PROJ-05: credential-isolated forge adapters, called only by the broker.

Git's compare-and-swap ref update binds publication to the approved base. The
commit object is built locally, so its identity is durable before any remote
write and an interrupted push can be reconciled by reads. No worker imports this
module or receives its connector configuration.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import ssl
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Protocol
from urllib.parse import urlsplit

import httpx

Json = dict[str, Any]
Outcome = Literal["succeeded", "absent", "conflict", "unknown"]


class ForgeError(Exception):
    """Fixed, credential-free errors only; never wrap remote output."""


class ForgeUnknown(ForgeError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def snapshot_files(content: bytes, digest: str) -> list[Json]:
    if hashlib.sha256(content).hexdigest() != digest:
        raise ForgeError("snapshot_integrity")
    try:
        value = json.loads(content)
        if set(value) != {"files"} or not isinstance(value["files"], list):
            raise ValueError
        paths: list[str] = []
        for item in value["files"]:
            if set(item) != {"path", "mode", "content"}:
                raise ValueError
            path = item["path"]
            if (
                not isinstance(path, str)
                or len(path) > 1024
                or any(
                    part in {"", ".", "..", ".git"} for part in path.lower().split("/")
                )
                or any(ord(c) < 32 or c in "\\\x7f" for c in path)
                or item["mode"] not in {"100644", "100755"}
            ):
                raise ValueError
            decoded = base64.b64decode(item["content"], validate=True)
            if base64.b64encode(decoded).decode() != item["content"]:
                raise ValueError
            paths.append(path)
        if paths != sorted(set(paths)) or canonical(value) != content:
            raise ValueError
        known = set(paths)
        if any(
            "/".join(path.split("/")[:i]) in known
            for path in paths
            for i in range(1, len(path.split("/")))
        ):
            raise ValueError
        return list(value["files"])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ForgeError("invalid_snapshot") from None


def branch_name(value: str) -> None:
    # git-check-ref-format rules, without accepting options or revision syntax.
    if (
        not value
        or value.startswith(("-", "/"))
        or value.endswith(("/", "."))
        or ".." in value
        or "@{" in value
        or value == "@"
        or any(ord(c) < 33 or c in "~^:?*[\\\x7f" for c in value)
        or any(
            not p or p.startswith(".") or p.endswith(".lock") for p in value.split("/")
        )
    ):
        raise ForgeError("invalid_branch")


class ForgeAdapter(Protocol):
    def connector_digest(self) -> str: ...
    def import_snapshot(
        self, limit: int, revision: str | None = None
    ) -> tuple[str, bytes]: ...
    def bind(self, action: Json) -> Json: ...
    def prepare(self, operation: Json, content: bytes | None) -> str | None: ...
    def reconcile(self, operation: Json) -> Outcome: ...
    def send(self, operation: Json, content: bytes | None) -> None: ...


@dataclass(frozen=True)
class Connector:
    remote_ref: str
    api_url: str
    repository: str
    author_name: str
    author_email: str
    token: str = field(repr=False, compare=False)
    ca_file: str | None = None
    username: str = "x-access-token"


class GitHubForge:
    """Git smart HTTPS for atomic refs; GitHub REST for pull requests.

    Connector URLs are operator-pinned, never derived from action parameters.
    The injected HTTP transport is a test seam; production uses HTTPS only.
    """

    authorization_scheme = "Bearer"
    accept = "application/vnd.github+json"
    page_size = 100
    page_parameter = "per_page"

    def __init__(
        self, connector: Connector, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.config = connector
        self.transport = transport
        for value in (connector.remote_ref, connector.api_url):
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ForgeError("invalid_connector")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", connector.repository):
            raise ForgeError("invalid_connector")
        if any(c in connector.username for c in ":\r\n"):
            raise ForgeError("invalid_connector")
        if any(c in connector.author_name + connector.author_email for c in "\r\n<>"):
            raise ForgeError("invalid_connector")

    def environment(self) -> dict[str, str]:
        # Deliberately do not inherit worker/provider credentials, git hooks,
        # SSH agents, credential helpers, proxies or user Git configuration.
        authorization = base64.b64encode(
            (self.config.username + ":" + self.config.token).encode()
        ).decode()
        result = {
            "PATH": os.defpath,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "5",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": "Authorization: Basic " + authorization,
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": "",
            "GIT_CONFIG_KEY_2": "http.followRedirects",
            "GIT_CONFIG_VALUE_2": "false",
            "GIT_CONFIG_KEY_3": "protocol.allow",
            "GIT_CONFIG_VALUE_3": "never",
            "GIT_CONFIG_KEY_4": "protocol.https.allow",
            "GIT_CONFIG_VALUE_4": "always",
        }

        if self.config.ca_file:
            result["GIT_SSL_CAINFO"] = self.config.ca_file
        return result

    def git(
        self,
        args: list[str],
        cwd: Path | None = None,
        content: bytes | None = None,
        extra: dict[str, str] | None = None,
    ) -> bytes:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                input=content,
                capture_output=True,
                timeout=30,
                env={**self.environment(), **(extra or {})},
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ForgeUnknown("forge_unavailable") from None
        if result.returncode:
            raise ForgeUnknown("forge_rejected_or_unavailable")
        return result.stdout

    def refs(self) -> dict[str, str]:
        raw = self.git(["ls-remote", "--heads", self.config.remote_ref])
        result: dict[str, str] = {}
        for line in raw.decode().splitlines():
            sha, ref = line.split("\t", 1)
            if not re.fullmatch(r"[a-f0-9]{40}", sha) or not ref.startswith(
                "refs/heads/"
            ):
                raise ForgeUnknown("invalid_forge_response")
            result[ref[11:]] = sha
        return result

    @contextmanager
    def checkout(self, revision: str) -> Iterator[Path]:
        root = Path(os.environ.get("ATLAS_FORGE_SCRATCH_ROOT", "BRAIN/forge"))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="atlas-forge-", dir=root) as directory:
            path = Path(directory)
            self.git(["init", "--bare", "--object-format=sha1", "."], path)
            self.git(
                ["fetch", "--no-tags", "--depth=1", self.config.remote_ref, revision],
                path,
            )
            yield path

    def connector_digest(self) -> str:
        return hashlib.sha256(
            canonical(
                dict(
                    remote_ref=self.config.remote_ref,
                    api_url=self.config.api_url,
                    repository=self.config.repository,
                    author_name=self.config.author_name,
                    author_email=self.config.author_email,
                    username=self.config.username,
                    ca_file=self.config.ca_file,
                )
            )
        ).hexdigest()

    def import_snapshot(
        self, limit: int, revision: str | None = None
    ) -> tuple[str, bytes]:
        if revision is None:
            metadata = self.request("GET", "")
            branch = metadata.get("default_branch")
            if not isinstance(branch, str):
                raise ForgeError("invalid_default_branch")
            branch_name(branch)
            revision = self.refs().get(branch)
        if revision is None or not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ForgeError("invalid_base_commit")
        files = []
        total = 0
        with self.checkout(revision) as path:
            entries = self.git(["ls-tree", "-r", "-z", "--long", revision], path).split(
                b"\0"
            )
            if len(entries) > 20001:
                raise ForgeError("repository_entry_limit")
            for entry in entries:
                if not entry:
                    continue
                meta, name = entry.split(b"\t", 1)
                mode, kind, oid, size = meta.split()
                if mode not in (b"100644", b"100755") or kind != b"blob":
                    raise ForgeError("unsupported_repository_entry")
                total += int(size)
                if total > limit:
                    raise ForgeError("repository_byte_limit")
                content = self.git(["cat-file", "blob", oid.decode("ascii")], path)
                files.append(
                    dict(
                        path=name.decode("utf-8"),
                        mode=mode.decode(),
                        content=base64.b64encode(content).decode(),
                    )
                )
        payload = canonical(dict(files=sorted(files, key=lambda f: f["path"])))
        snapshot_files(payload, hashlib.sha256(payload).hexdigest())
        return revision, payload

    def bind(self, action: Json) -> Json:
        params, kind = action["parameters"], action["kind"]
        for key in ("message", "body", "title"):
            if "Atlas-Operation:" in params.get(key, ""):
                raise ForgeError("reserved_marker")
        if kind == "publish_change":
            branch_name(params["branch"])
            branch_name(params["target"])
            refs = self.refs()
            if (
                params["branch"] in refs
                or refs.get(params["target"]) != action["base_commit"]
            ):
                raise ForgeError("base_mismatch")
            return dict(
                connector_digest=self.connector_digest(),
                target_commit=action["base_commit"],
            )
        names = (
            [params["name"]]
            if kind == "branch"
            else [action["target_branch"]]
            if kind == "commit"
            else [params["head"], params["target"]]
        )
        for name in names:
            branch_name(name)
        binding = dict(connector_digest=self.connector_digest())
        refs = self.refs()
        if kind == "branch":
            if names[0] in refs:
                raise ForgeError("base_mismatch")
            with self.checkout(action["base_commit"]):
                pass
            return binding
        target = names[0] if kind == "commit" else names[1]
        if refs.get(target) != action["base_commit"]:
            raise ForgeError("base_mismatch")
        if kind == "pull_request":
            if names[0] not in refs or names[0] == target:
                raise ForgeError("base_mismatch")
            return dict(binding, head_commit=refs[names[0]], target_commit=refs[target])
        return binding

    def commit(self, path: Path, operation: Json, content: bytes) -> str:
        action = operation["action"]
        entries = snapshot_files(content, action["snapshot"]["sha256"])
        self.git(["read-tree", "--empty"], path)
        for item in entries:
            blob = (
                self.git(
                    ["hash-object", "-w", "--stdin"],
                    path,
                    base64.b64decode(item["content"]),
                )
                .decode()
                .strip()
            )
            self.git(
                [
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    item["mode"],
                    blob,
                    item["path"],
                ],
                path,
            )
        tree = self.git(["write-tree"], path).decode().strip()
        message = (
            action["parameters"]["message"]
            + "\n\nAtlas-Operation: "
            + operation["id"]
            + "\n"
        )
        extra = {
            "GIT_AUTHOR_NAME": self.config.author_name,
            "GIT_AUTHOR_EMAIL": self.config.author_email,
            "GIT_COMMITTER_NAME": self.config.author_name,
            "GIT_COMMITTER_EMAIL": self.config.author_email,
            "GIT_AUTHOR_DATE": operation["created_at"],
            "GIT_COMMITTER_DATE": operation["created_at"],
        }
        return (
            self.git(
                ["commit-tree", tree, "-p", action["base_commit"]],
                path,
                message.encode(),
                extra,
            )
            .decode()
            .strip()
        )

    def prepare(self, operation: Json, content: bytes | None) -> str | None:
        if operation["action"]["kind"] != "commit":
            return None
        if content is None:
            raise ForgeError("snapshot_missing")
        with self.checkout(operation["action"]["base_commit"]) as path:
            return self.commit(path, operation, content)

    def request(self, method: str, suffix: str, data: Json | None = None) -> Any:
        try:
            with httpx.Client(
                transport=self.transport,
                trust_env=False,
                verify=ssl.create_default_context(cafile=self.config.ca_file),
                follow_redirects=False,
                timeout=15,
            ) as client:
                response = client.request(
                    method,
                    self.config.api_url.rstrip("/")
                    + "/repos/"
                    + self.config.repository
                    + suffix,
                    headers={
                        "Authorization": self.authorization_scheme
                        + " "
                        + self.config.token,
                        "Accept": self.accept,
                    },
                    json=data,
                )
                if response.status_code not in (200, 201):
                    raise ForgeUnknown("forge_rejected_or_unavailable")
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise ForgeUnknown("forge_unavailable") from None

    def reconcile(self, operation: Json) -> Outcome:
        action, params = operation["action"], operation["action"]["parameters"]
        try:
            if action["kind"] == "pull_request":
                marker = "<!-- Atlas-Operation: " + operation["id"] + " -->"
                for page in range(1, 101):
                    rows = self.request(
                        "GET",
                        f"/pulls?state=all&{self.page_parameter}={self.page_size}&page={page}",
                    )
                    if not isinstance(rows, list):
                        return "unknown"
                    for row in rows:
                        if marker in (row.get("body") or ""):
                            matches = (
                                row["title"] == params["title"]
                                and row["body"] == params["body"] + "\n\n" + marker
                                and row["head"]["ref"] == params["head"]
                                and row["base"]["ref"] == params["target"]
                                and row["head"]["sha"]
                                == operation["binding"]["head_commit"]
                                and row["base"]["sha"] == action["base_commit"]
                            )
                            if matches and isinstance(row.get("html_url"), str):
                                url = urlsplit(row["html_url"])
                                configured = urlsplit(self.config.remote_ref)
                                if (
                                    url.scheme == "https"
                                    and (url.hostname, url.port)
                                    == (configured.hostname, configured.port)
                                    and not url.username
                                    and not url.password
                                ):
                                    operation["pr_url"] = row["html_url"]
                            return "succeeded" if matches else "conflict"
                    if len(rows) < self.page_size:
                        return "absent"
                return "unknown"
            refs = self.refs()
            branch = (
                params["name"]
                if action["kind"] == "branch"
                else action["target_branch"]
            )
            actual = refs.get(branch)
            expected = (
                action["base_commit"]
                if action["kind"] == "branch"
                else operation["commit"]
            )
            if actual == expected:
                return "succeeded"
            if action["kind"] == "branch":
                return "absent" if actual is None else "conflict"
            if actual == action["base_commit"]:
                return "absent"
            # Divergence is not proof of absence: another writer may have moved
            # the branch after our successful push. Never retry on that evidence.
            return "unknown"
        except (ForgeError, KeyError, TypeError, ValueError):
            return "unknown"

    def send(self, operation: Json, content: bytes | None) -> None:
        action, params = operation["action"], operation["action"]["parameters"]
        binding = self.bind(action)
        if binding != operation["binding"]:
            raise ForgeError("base_mismatch")
        if action["kind"] == "pull_request":
            self.request(
                "POST",
                "/pulls",
                dict(
                    title=params["title"],
                    head=params["head"],
                    base=params["target"],
                    body=params["body"]
                    + "\n\n<!-- Atlas-Operation: "
                    + operation["id"]
                    + " -->",
                ),
            )
            return
        with self.checkout(action["base_commit"]) as path:
            expected = "" if action["kind"] == "branch" else action["base_commit"]
            branch = (
                params["name"]
                if action["kind"] == "branch"
                else action["target_branch"]
            )
            commit = action["base_commit"]
            if action["kind"] == "commit":
                if content is None:
                    raise ForgeError("snapshot_missing")
                commit = self.commit(path, operation, content)
                if commit != operation["commit"]:
                    raise ForgeError("commit_integrity")
            ref = "refs/heads/" + branch
            self.git(
                [
                    "push",
                    "--porcelain",
                    "--force-with-lease=" + ref + ":" + expected,
                    self.config.remote_ref,
                    commit + ":" + ref,
                ],
                path,
            )


class ForgejoForge(GitHubForge):
    """Gitea-compatible REST with the same credential-isolated atomic Git writes.

    Gitea API tokens use the token scheme; list pagination uses limit, not
    GitHub's per_page. One row per page also respects an operator's lower cap.
    """

    authorization_scheme = "token"
    accept = "application/json"
    page_size = 1
    page_parameter = "limit"

    def __init__(
        self, connector: Connector, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        super().__init__(connector, transport=transport)
        remote, api = urlsplit(connector.remote_ref), urlsplit(connector.api_url)
        if (remote.hostname, remote.port) != (
            api.hostname,
            api.port,
        ) or api.path.rstrip("/") != "/api/v1":
            raise ForgeError("invalid_connector")


# Configured adapters are selected explicitly by the retrieval broker.
AdapterFactory = Callable[[Json], ForgeAdapter]
