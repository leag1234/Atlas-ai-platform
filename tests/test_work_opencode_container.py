"""Tests for the per-turn opencode engine container (no docker required)."""

import asyncio
import json
import os
import unittest
from typing import Any
from unittest import mock

from services.orchestrator import work_egress
from services.orchestrator import work_opencode_container as container

API_KEY = "test-api-key-value"
BASE = "https://atlas.example.test/api/atlas/v1"
ENV = {
    "ATLAS_WORK_EGRESS": "registries",
    "ATLAS_WORK_ENGINE": "opencode",
    "ATLAS_WORK_ENGINE_API_KEY": API_KEY,
    "ATLAS_WORK_ENGINE_API_BASE": BASE,
}


class FakeWorkspace:
    key = "org/project/session/ws/g1"
    scope_key = "org/project/session/ws"
    scoped = True
    proxy_host = "egress-abc"

    def __init__(self) -> None:
        self.prepared = 0

    async def prepare(self) -> None:
        self.prepared += 1


def arguments() -> list[str]:
    return container.docker_arguments(
        "engine-1",
        FakeWorkspace(),  # type: ignore[arg-type]
        "atlas-work-egress",
        container.opencode_config(BASE, "atlas", "atlas-code"),
    )


def env_of(args: list[str]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for index, value in enumerate(args[:-1]):
        if value == "-e":
            name, sep, rest = args[index + 1].partition("=")
            result[name] = rest if sep else None
    return result


class DockerArgumentsTests(unittest.TestCase):
    def test_hardening_and_network(self) -> None:
        args = arguments()
        for flag in ("--read-only", "--cap-drop", "--security-opt", "--pids-limit"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--user") + 1], "1000:1000")
        self.assertEqual(args[args.index("--network") + 1], "atlas-work-egress")
        self.assertNotIn("-p", args)
        self.assertNotIn("--publish", args)
        self.assertNotIn("--add-host", args)
        self.assertEqual(args[args.index("--entrypoint") + 1], "opencode")
        self.assertEqual(args[-5:-4], ["serve"])

    def test_workspace_subpath_and_label(self) -> None:
        args = arguments()
        mount = args[args.index("--mount") + 1]
        self.assertTrue(
            mount.endswith(",volume-nocopy,volume-subpath=org/project/session/ws/g1")
        )
        self.assertIn("atlas.work=org/project/session/ws", args)

    def test_secrets_are_names_only(self) -> None:
        args = arguments()
        env = env_of(args)
        self.assertIsNone(env["OPENCODE_SERVER_PASSWORD"])
        self.assertIsNone(env["ATLAS_API_KEY"])
        self.assertNotIn(API_KEY, " ".join(args))

    def test_proxy_and_disable_variables(self) -> None:
        env = env_of(arguments())
        self.assertEqual(env["HTTPS_PROXY"], "http://egress-abc:3128")
        self.assertEqual(env["OPENCODE_DISABLE_AUTOUPDATE"], "1")
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"] or "{}")
        self.assertEqual(config["permission"]["bash"], "ask")
        self.assertEqual(config["permission"]["external_directory"], "deny")
        self.assertEqual(
            config["provider"]["atlas"]["options"]["apiKey"], "{env:ATLAS_API_KEY}"
        )
        self.assertEqual(config["provider"]["atlas"]["options"]["baseURL"], BASE)

    def test_api_base_must_be_https(self) -> None:
        with mock.patch.dict(os.environ, {"ATLAS_WORK_ENGINE_API_BASE": "http://x/v1"}):
            with self.assertRaises(RuntimeError):
                container.api_base()


class EgressAllowlistTests(unittest.TestCase):
    def test_engine_api_host_allowed_only_with_engine(self) -> None:
        with mock.patch.dict(os.environ, ENV):
            self.assertIn("atlas.example.test", work_egress.allowed_hosts())
            self.assertIn("atlas.example.test", work_egress.squid_configuration("s"))
            self.assertTrue(work_egress.decide("atlas.example.test").allowed)
        with mock.patch.dict(os.environ, {**ENV, "ATLAS_WORK_ENGINE": ""}):
            self.assertNotIn("atlas.example.test", work_egress.allowed_hosts())
            self.assertFalse(work_egress.decide("atlas.example.test").allowed)


class StartStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_passes_secrets_through_env_and_waits(self) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

        async def run(*args: str, env: dict[str, str] | None = None) -> Any:
            calls.append((args, env))
            if args[:2] == ("docker", "inspect"):
                return 0, b"172.30.0.5\n", b""
            return 0, b"cid\n", b""

        workspace = FakeWorkspace()
        with (
            mock.patch.dict(os.environ, ENV),
            mock.patch.object(container, "_run", run),
            mock.patch.object(container, "_wait_healthy", mock.AsyncMock()) as healthy,
        ):
            engine = await container.start(workspace, "atlas-code")  # type: ignore[arg-type]
        # process_work prepared the workspace; the engine must not prepare again.
        self.assertEqual(workspace.prepared, 0)
        self.assertEqual(engine.url, "http://172.30.0.5:4096")
        run_args, run_env = calls[0]
        self.assertEqual(run_args[:3], ("docker", "run", "-d"))
        self.assertNotIn(API_KEY, " ".join(run_args))
        self.assertNotIn(engine.password, " ".join(run_args))
        assert run_env is not None
        self.assertEqual(run_env["ATLAS_API_KEY"], API_KEY)
        self.assertEqual(run_env["OPENCODE_SERVER_PASSWORD"], engine.password)
        healthy.assert_awaited_once()

    async def test_health_timeout_removes_container(self) -> None:
        calls: list[tuple[str, ...]] = []

        async def run(*args: str, env: dict[str, str] | None = None) -> Any:
            calls.append(args)
            if args[:2] == ("docker", "inspect"):
                return 0, b"172.30.0.5\n", b""
            return 0, b"", b""

        with (
            mock.patch.dict(os.environ, ENV),
            mock.patch.object(container, "_run", run),
            mock.patch.object(
                container,
                "_wait_healthy",
                mock.AsyncMock(
                    side_effect=RuntimeError("opencode_engine_health_timeout")
                ),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(
                    container.start(FakeWorkspace(), "atlas-code"),  # type: ignore[arg-type]
                    timeout=20,
                )
        self.assertEqual(calls[-1][:3], ("docker", "rm", "-f"))

    async def test_refuses_without_egress(self) -> None:
        with mock.patch.dict(os.environ, {**ENV, "ATLAS_WORK_EGRESS": ""}):
            with self.assertRaises(RuntimeError):
                await container.start(FakeWorkspace(), "atlas-code")  # type: ignore[arg-type]
