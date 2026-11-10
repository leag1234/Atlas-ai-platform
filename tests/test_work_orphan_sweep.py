"""Tests for the startup sweep of orphaned Work containers (no docker required)."""

import os
import unittest
from unittest import mock

from services.orchestrator import work_orphan_sweep as sweep

NOW = 1_800_000_000.0


def created(age_seconds: float) -> str:
    from datetime import datetime, timezone

    stamp = datetime.fromtimestamp(NOW - age_seconds, tz=timezone.utc)
    return stamp.isoformat().replace("+00:00", "Z")


class FakeDocker:
    """Records docker calls and answers ps/inspect/rm from a fixed inventory."""

    def __init__(self, containers: list[tuple[str, str]]) -> None:
        self.containers = containers
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, *args: str) -> tuple[int, bytes, bytes]:
        self.calls.append(args)
        if args[:2] == ("docker", "ps"):
            return (
                0,
                (
                    "\n".join(f"id{i}" for i in range(len(self.containers))) + "\n"
                ).encode(),
                b"",
            )
        if args[:2] == ("docker", "inspect"):
            lines = "\n".join(f"/{name}\t{stamp}" for name, stamp in self.containers)
            return 0, (lines + "\n").encode(), b""
        if args[:2] == ("docker", "rm"):
            return 0, b"", b""
        raise AssertionError("unexpected docker call: " + repr(args))

    def removed(self) -> list[str]:
        for args in self.calls:
            if args[:2] == ("docker", "rm"):
                return list(args[3:])
        return []


class IsOwnContainerTests(unittest.TestCase):
    def test_base_zero_names(self) -> None:
        with mock.patch.dict(os.environ, {"ATLAS_PORT_BASE": "0"}):
            self.assertTrue(sweep.is_own_container("atlas-opencode-abc"))
            self.assertTrue(sweep.is_own_container("atlas-work-egress-abc"))
            self.assertTrue(sweep.is_own_container("atlas-code-abc"))
            self.assertFalse(sweep.is_own_container("atlas-opencode-abc-6300"))
            self.assertFalse(sweep.is_own_container("atlas-chat-ui"))
            self.assertFalse(sweep.is_own_container("atlas-open-terminal"))

    def test_suffixed_names(self) -> None:
        with mock.patch.dict(os.environ, {"ATLAS_PORT_BASE": "6300"}):
            self.assertTrue(sweep.is_own_container("atlas-opencode-abc-6300"))
            self.assertTrue(sweep.is_own_container("atlas-work-egress-abc-6300"))
            self.assertTrue(sweep.is_own_container("atlas-code-abc-6300"))
            self.assertFalse(sweep.is_own_container("atlas-opencode-abc"))
            self.assertFalse(sweep.is_own_container("atlas-opencode-abc-6301"))
            self.assertFalse(sweep.is_own_container("atlas-opencode-a-b-6300"))


class SweepTests(unittest.IsolatedAsyncioTestCase):
    async def run_sweep(
        self, containers: list[tuple[str, str]], env: dict[str, str] | None = None
    ) -> tuple[list[str], FakeDocker]:
        docker = FakeDocker(containers)
        with (
            mock.patch.dict(os.environ, env or {"ATLAS_PORT_BASE": "0"}),
            mock.patch.object(sweep, "_docker", docker),
        ):
            removed = await sweep.sweep(now=NOW)
        return removed, docker

    async def test_removes_only_aged_own_containers(self) -> None:
        limit = sweep.max_age_seconds()
        containers = [
            ("atlas-opencode-old", created(limit + 10)),
            ("atlas-work-egress-old", created(limit + 3600)),
            ("atlas-code-old", created(limit + 1)),
            ("atlas-opencode-fresh", created(limit - 10)),
            ("atlas-work-egress-fresh", created(60)),
        ]
        removed, docker = await self.run_sweep(containers)
        self.assertEqual(
            sorted(removed),
            ["atlas-code-old", "atlas-opencode-old", "atlas-work-egress-old"],
        )
        self.assertEqual(sorted(docker.removed()), sorted(removed))

    async def test_never_touches_another_port_base(self) -> None:
        limit = sweep.max_age_seconds()
        containers = [
            ("atlas-opencode-old-6300", created(limit + 9999)),
            ("atlas-work-egress-old-6300", created(limit + 9999)),
            ("atlas-code-old-6300", created(limit + 9999)),
            ("atlas-opencode-old", created(limit + 9999)),
        ]
        removed, docker = await self.run_sweep(containers, {"ATLAS_PORT_BASE": "0"})
        self.assertEqual(removed, ["atlas-opencode-old"])
        self.assertEqual(docker.removed(), ["atlas-opencode-old"])

    async def test_suffixed_instance_sweeps_only_its_own_suffix(self) -> None:
        limit = sweep.max_age_seconds()
        containers = [
            ("atlas-opencode-old-6300", created(limit + 9999)),
            ("atlas-opencode-old", created(limit + 9999)),
        ]
        removed, docker = await self.run_sweep(containers, {"ATLAS_PORT_BASE": "6300"})
        self.assertEqual(removed, ["atlas-opencode-old-6300"])
        self.assertEqual(docker.removed(), ["atlas-opencode-old-6300"])

    async def test_nothing_to_remove_skips_rm(self) -> None:
        removed, docker = await self.run_sweep([("atlas-opencode-fresh", created(5))])
        self.assertEqual(removed, [])
        self.assertEqual(docker.removed(), [])

    async def test_empty_inventory(self) -> None:
        removed, docker = await self.run_sweep([])
        self.assertEqual(removed, [])
        self.assertEqual(len(docker.calls), 1)

    async def test_list_failure_returns_empty(self) -> None:
        async def failing(*args: str) -> tuple[int, bytes, bytes]:
            return 1, b"", b"docker daemon down"

        with (
            mock.patch.dict(os.environ, {"ATLAS_PORT_BASE": "0"}),
            mock.patch.object(sweep, "_docker", failing),
        ):
            self.assertEqual(await sweep.sweep(now=NOW), [])

    async def test_docker_missing_does_not_raise(self) -> None:
        async def missing(*args: str) -> tuple[int, bytes, bytes]:
            raise FileNotFoundError("docker")

        with (
            mock.patch.dict(os.environ, {"ATLAS_PORT_BASE": "0"}),
            mock.patch.object(sweep, "_docker", missing),
        ):
            self.assertEqual(await sweep.sweep(now=NOW), [])

    async def test_margin_and_wall_clock_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"ATLAS_CODE_WALL_SECONDS": "300", "ATLAS_WORK_SWEEP_MARGIN_SECONDS": "60"},
        ):
            self.assertEqual(sweep.max_age_seconds(), 360.0)
        with mock.patch.dict(os.environ, {"ATLAS_CODE_WALL_SECONDS": "bogus"}):
            self.assertEqual(sweep.max_turn_seconds(), 1200.0)

    async def test_sweep_at_startup_delegates(self) -> None:
        with mock.patch.object(
            sweep, "sweep", mock.AsyncMock(return_value=["atlas-code-x"])
        ) as inner:
            removed = await sweep.sweep_at_startup()
        self.assertEqual(removed, ["atlas-code-x"])
        inner.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
