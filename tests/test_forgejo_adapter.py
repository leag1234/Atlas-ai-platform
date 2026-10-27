"""Gitea-compatible REST over a fake HTTP server, using real atomic Git refs."""

import base64
import hashlib
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import httpx
import test_project_forge as fixture
from services.orchestrator.project_forge import (
    Connector,
    ForgejoForge,
    ForgeError,
    canonical,
)


class ForgejoTransport(httpx.BaseTransport):
    def __init__(self, remote: fixture.LocalForge) -> None:
        self.remote = remote
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        with httpx.Client(trust_env=False) as client:
            response = client.request(
                request.method,
                self.remote.url + request.url.raw_path.decode().removeprefix("/api/v1"),
                headers=request.headers,
                content=request.read(),
            )
        return httpx.Response(response.status_code, content=response.content)


class LocalForgejo(ForgejoForge):
    def __init__(self, remote: fixture.LocalForge) -> None:
        self.remote = remote
        super().__init__(
            Connector(
                "https://forge.example/demo.git",
                "https://forge.example/api/v1",
                "org/demo",
                "Test",
                "test@example.invalid",
                "synthetic-token",
            ),
            transport=ForgejoTransport(remote),
        )

    def environment(self) -> dict[str, str]:
        return dict(
            super().environment(),
            GIT_CONFIG_COUNT="7",
            GIT_CONFIG_KEY_5="protocol.http.allow",
            GIT_CONFIG_VALUE_5="always",
            GIT_CONFIG_KEY_6="url." + self.remote.url + "/.insteadOf",
            GIT_CONFIG_VALUE_6="https://forge.example/",
        )


class ForgejoAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.remote = fixture.LocalForge(Path(directory.name))
        self.addCleanup(self.remote.close)
        scratch = patch.dict(os.environ, {"ATLAS_FORGE_SCRATCH_ROOT": directory.name})
        scratch.start()
        self.addCleanup(scratch.stop)
        self.adapter = LocalForgejo(self.remote)

    def operation(self, action: dict[str, Any]) -> dict[str, Any]:
        return dict(
            id=str(uuid4()),
            created_at="2026-10-25T00:00:00Z",
            action=action,
            binding=self.adapter.bind(action),
        )

    def test_branch_commit_and_pr_share_atomic_refs_and_token_rest(self) -> None:
        branch = self.operation(
            dict(
                kind="branch", base_commit=self.remote.base, parameters=dict(name="fix")
            )
        )
        self.adapter.send(branch, None)
        self.assertEqual(self.adapter.reconcile(branch), "succeeded")
        content = canonical(
            dict(
                files=[
                    dict(
                        path="answer.py",
                        mode="100644",
                        content=base64.b64encode(b"answer = 42\n").decode(),
                    )
                ]
            )
        )
        commit = self.operation(
            dict(
                kind="commit",
                base_commit=self.remote.base,
                target_branch="fix",
                snapshot=dict(sha256=hashlib.sha256(content).hexdigest()),
                parameters=dict(message="Repair"),
            )
        )
        commit["commit"] = self.adapter.prepare(commit, content)
        self.adapter.send(commit, content)
        self.assertEqual(self.remote.git("show", "fix:answer.py"), "answer = 42")
        pr = self.operation(
            dict(
                kind="pull_request",
                base_commit=self.remote.base,
                parameters=dict(
                    head="fix", target="main", title="Repair", body="Tested"
                ),
            )
        )
        self.adapter.send(pr, None)
        self.assertEqual(self.adapter.reconcile(pr), "succeeded")
        transport = self.adapter.transport
        assert isinstance(transport, ForgejoTransport)
        self.assertTrue(
            all(
                r.headers["Authorization"] == "token synthetic-token"
                for r in transport.requests
            )
        )
        self.assertIn("limit=1", str(transport.requests[-1].url))
        self.assertEqual(self.remote.writes, 3)
        revision, snapshot = self.adapter.import_snapshot(100, commit["commit"])
        self.assertEqual(revision, commit["commit"])
        self.assertEqual(snapshot, content)

    def test_https_origin_and_private_configuration_are_explicit(self) -> None:
        for config in (
            replace(self.adapter.config, api_url="http://forge.example/api/v1"),
            replace(self.adapter.config, api_url="https://other.example/api/v1"),
            replace(
                self.adapter.config,
                remote_ref="https://user:secret@forge.example/repo.git",
            ),
        ):
            with self.assertRaises(ForgeError):
                ForgejoForge(config)
        with self.assertRaises(ForgeError):
            self.adapter.import_snapshot(0, "not-a-commit")
