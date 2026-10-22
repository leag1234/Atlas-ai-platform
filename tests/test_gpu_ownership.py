"""Ownership and capacity preservation through executable, offline provider doubles."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from typing import Any

import image_lifecycle
from packages.gpu_ownership import GPUDisabled, gpu_owner
from services.orchestrator.image_tool import (
    images_enabled,
    unavailable_reply,
    explicit_image_request,
    guest_capability_instruction,
)

ROOT = Path(__file__).resolve().parents[1]


class OwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "BRAIN").mkdir()
        (self.root / "infra").symlink_to(ROOT / "infra")
        (self.root / "packages").symlink_to(ROOT / "packages")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for tool in ("scw", "ssh", "scp", "curl", "date", "sleep", "flag"):
            target = self.bin / tool
            target.write_text(
                f"#!{sys.executable}\nexec(compile(open({str(ROOT / 'tests/gpu_fakes.py')!r}).read(), 'gpu_fakes.py', 'exec'))\n"
            )
            target.chmod(0o755)
        self.state = self.root / "provider.json"
        self.env = {
            "PATH": str(self.bin) + ":" + os.environ["PATH"],
            "PYTHONPATH": str(ROOT),
            "HOME": str(self.root),
            "ATLAS_PORT_BASE": "12500",
            "ATLAS_GPU_OWNER": "dev-test",
            "ATLAS_DEV_GPU": "1",
            "SCW_DEFAULT_PROJECT_ID": "test-project",
            "SCW_DEFAULT_ZONE": "fr-par-2",
            "GPU_FAKE_STATE": str(self.state),
            "LOCAL_MODEL": "org/model",
            "GPU_CLIENT_IP": "192.0.2.1",
            "GPU_MAX_EUR_H": "2",
            "GPU_SSH_PUBLIC_KEY": "",
            "ATLAS_GPU_ROOT": str(self.root),
            "ATLAS_GPU_ENV_FILE": str(self.root / "absent.env"),
            "ATLAS_GPU_FLAG_SCRIPT": str(self.bin / "flag"),
        }
        self.save({})

    def save(self, data: dict[str, Any]) -> None:
        self.state.write_text(json.dumps(data))

    def load(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.state.read_text())
        return value

    def fixture(
        self, owner: str | None = "demo", name: str = "atlas-gpu"
    ) -> dict[str, Any]:
        tags = ["atlas-m1"] + (["atlas-owner=" + owner] if owner else [])
        return {
            "instance/server": [
                {
                    "id": "demo-server",
                    "name": name,
                    "tags": tags,
                    "volumes": {"0": {"id": "demo-root"}},
                    "public_ips": [
                        {"id": "demo-ip", "address": "192.0.2.10", "family": "inet"}
                    ],
                    "security_group": {"id": "demo-group"},
                    "state": "running",
                    "commercial_type": "L4-1-24G",
                }
            ],
            "block/volume": [{"id": "demo-root", "tags": tags}],
            "instance/ip": [{"id": "demo-ip", "tags": tags}],
            "instance/security-group": [{"id": "demo-group", "tags": tags}],
            "instance/server-type": [
                {
                    "name": "L4-1-24G",
                    "availability": "shortage",
                    "hourly_price": {"units": 1, "nanos": 0},
                }
            ],
        }

    def run_gpu(self, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "infra/gpu.py"), command],
            env=self.env,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def mutations(self) -> list[list[str]]:
        return [
            c for c in self.load().get("calls", []) if c[0] == "scw" and c[3] != "list"
        ]

    def test_dev_never_touches_demo(self) -> None:
        for owner in ("demo", None):
            with self.subTest(owner=owner):
                self.save(self.fixture(owner))
                result = self.run_gpu("down")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.mutations(), [])
                self.assertEqual(len(self.load()["instance/server"]), 1)

    def test_explicit_demo_migrates_legacy_server_and_children(self) -> None:
        self.env["ATLAS_GPU_OWNER"] = "demo"
        self.save(self.fixture(None))
        result = self.run_gpu("down")
        self.assertEqual(result.returncode, 0, result.stderr)
        updates = [c for c in self.mutations() if c[3] == "update"]
        self.assertEqual(len(updates), 4)
        self.assertTrue(all("tags.1=atlas-owner=demo" in c for c in updates))
        self.assertEqual(self.load()["instance/server"], [])
        self.assertEqual(self.load()["block/volume"], [])
        self.assertEqual(self.load()["instance/ip"], [])

    def test_foreign_or_untagged_same_name_refuses_all_mutations(self) -> None:
        for command in ("up", "down", "delete", "stop", "reboot", "resume", "replace"):
            for owner in ("foreign", None):
                with self.subTest(command=command, owner=owner):
                    self.save(self.fixture(owner, "atlas-gpu-dev-test"))
                    result = self.run_gpu(command)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("ownership refusal", result.stderr)
                    self.assertEqual(self.mutations(), [])

    def test_foreign_attached_resource_refuses_before_migration(self) -> None:
        self.env["ATLAS_GPU_OWNER"] = "demo"
        for kind in ("block/volume", "instance/ip", "instance/security-group"):
            with self.subTest(kind=kind):
                data = self.fixture(None)
                data[kind][0]["tags"] = ["atlas-owner=foreign"]
                self.save(data)
                result = self.run_gpu("down")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.mutations(), [])

    def test_creation_tags_every_resource_and_uses_owner_name(self) -> None:
        data = self.fixture()
        data["instance/server-type"][0]["availability"] = "available"
        self.save(data)
        result = self.run_gpu("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.load()
        for kind in (
            "instance/server",
            "block/volume",
            "instance/ip",
            "instance/security-group",
        ):
            created = [r for r in data[kind] if r["id"].endswith("-created")]
            self.assertTrue(created)
            self.assertTrue(
                all(
                    "atlas-owner=dev-test" in r["tags"] and "atlas-m1" in r["tags"]
                    for r in created
                )
            )
        self.assertEqual(data["instance/server"][-1]["name"], "atlas-gpu-dev-test")
        result = self.run_gpu("down")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [r["id"] for r in self.load()["instance/server"]], ["demo-server"]
        )

    def test_owner_default_is_stable_worktree_hash(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            expected = (
                "dev-"
                + hashlib.sha256(str(Path.cwd().resolve()).encode()).hexdigest()[:10]
            )
            self.assertEqual(gpu_owner(), expected)
            self.assertEqual(gpu_owner(), expected)

    def test_implicit_demo_cannot_adopt_legacy(self) -> None:
        self.env.pop("ATLAS_GPU_OWNER")
        self.env["ATLAS_STABLE"] = "1"
        self.save(self.fixture(None))
        self.assertNotEqual(self.run_gpu("down").returncode, 0)
        self.assertEqual(self.mutations(), [])

    def test_disabled_dev_never_checks_http_or_launches(self) -> None:
        with (
            patch.dict(os.environ, {"ATLAS_PORT_BASE": "12500"}, clear=True),
            patch.object(image_lifecycle, "require_env"),
            patch("image_lifecycle.subprocess.Popen") as launch,
            patch("image_lifecycle.urllib.request.urlopen") as http,
        ):
            self.assertFalse(images_enabled())
            self.assertIn("not available", unavailable_reply("en"))
            self.assertFalse(image_lifecycle.ready())
            with self.assertRaises(GPUDisabled):
                image_lifecycle.start_worker()
            with self.assertRaises(GPUDisabled):
                asyncio.run(image_lifecycle.ensure_worker(895))
            launch.assert_not_called()
            http.assert_not_called()

    def test_disabled_image_request_returns_unavailable_without_gateway(self) -> None:
        from unittest.mock import AsyncMock
        from services.orchestrator.chat_pipeline import _process
        from services.orchestrator.chat_schema import ChatMessage, ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import GatewayModel

        with (
            patch.dict(os.environ, {"ATLAS_PORT_BASE": "12500"}, clear=True),
            patch.object(GatewayModel, "connect", AsyncMock()) as connect,
            patch.object(GatewayModel, "post", AsyncMock()) as post,
        ):
            question = "Create an image of a garden"
            item = Interaction(question=question)
            asyncio.run(
                _process(
                    ChatRequest(
                        messages=[ChatMessage(role="user", content=question)],
                        ui_locale="en",
                        lang="en",
                    ),
                    item,
                )
            )
            self.assertEqual(item.reponse, unavailable_reply("en"))
            self.assertEqual(item.state, "done")
            self.assertEqual(item.cout_eur, 0.0)
            connect.assert_not_awaited()
            post.assert_not_awaited()

    def test_disabled_dev_draw_requests_are_unavailable_without_disabling_other_tools(
        self,
    ) -> None:
        with patch.dict(os.environ, {}, clear=True):
            for question in ("draw a sheep", "dessine un arbre", "zeichne einen Baum"):
                self.assertTrue(explicit_image_request(question))
            self.assertFalse(images_enabled())
            self.assertNotIn("file publishing", guest_capability_instruction())
            self.assertNotIn("code execution", guest_capability_instruction())
        with patch.dict(os.environ, {"ATLAS_STABLE": "1"}, clear=True):
            self.assertFalse(explicit_image_request("draw a sheep"))

    def test_direct_dev_provisioning_requires_explicit_opt_in(self) -> None:
        self.env.pop("ATLAS_DEV_GPU")
        self.save(self.fixture())
        result = self.run_gpu("up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not available", result.stderr)
        self.assertEqual(self.mutations(), [])

    def test_configured_workers_do_not_require_development_opt_in(self) -> None:
        for source in ("environment", "file"):
            with (
                self.subTest(source=source),
                patch.dict(os.environ, {"ATLAS_PORT_BASE": "12500"}, clear=True),
                patch(
                    "packages.gpu_ownership.Path", side_effect=lambda p: self.root / p
                ),
                patch("image_lifecycle.Path", side_effect=lambda p: self.root / p),
                patch("image_lifecycle.urllib.request.urlopen") as http,
                patch("image_lifecycle.subprocess.Popen") as launch,
            ):
                if source == "environment":
                    os.environ["ATLAS_IMAGE_GPU_IP"] = "127.0.0.9"
                else:
                    (self.root / "BRAIN/gpu_ip.txt").write_text("127.0.0.9\n")
                http.return_value.__enter__.return_value.status = 200
                self.assertTrue(images_enabled())
                self.assertTrue(image_lifecycle.ready())
                asyncio.run(image_lifecycle.ensure_worker(1))
                self.assertEqual(
                    http.call_args.args[0], "http://127.0.0.9:20500/health"
                )
                launch.assert_not_called()

    def test_injected_ready_worker_does_not_require_opt_in(self) -> None:
        with (
            patch.dict(os.environ, {"ATLAS_PORT_BASE": "12500"}, clear=True),
            patch.object(image_lifecycle, "ready", return_value=True),
            patch.object(image_lifecycle, "start_worker") as launch,
        ):
            asyncio.run(image_lifecycle.ensure_worker(1))
            launch.assert_not_called()

    def test_disabled_dev_cannot_manage_owned_resources(self) -> None:
        self.env.pop("ATLAS_DEV_GPU")
        for command in ("down", "delete", "stop", "reboot", "resume", "replace"):
            with self.subTest(command=command):
                data = self.fixture("dev-test", "atlas-gpu-dev-test")
                data["instance/server-type"][0]["availability"] = "available"
                self.save(data)
                result = self.run_gpu(command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not available", result.stderr)
                self.assertEqual(self.mutations(), [])

    def test_image_launcher_disabled_before_cloud_discovery(self) -> None:
        self.env.pop("ATLAS_DEV_GPU", None)
        result = subprocess.run(
            ["bash", str(ROOT / "infra/imagegen.sh"), "serve"],
            env=self.env,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not available", result.stderr)
        self.assertEqual(self.load(), {})

    def test_foreign_orphan_names_refuse_cleanup(self) -> None:
        for kind, name in (
            ("instance/security-group", "atlas-gpu-dev-test"),
            ("block/volume", "atlas-gpu-dev-test-weights"),
        ):
            with self.subTest(kind=kind):
                self.save(
                    {
                        kind: [
                            {
                                "id": "foreign-resource",
                                "name": name,
                                "tags": ["atlas-owner=other"],
                            }
                        ]
                    }
                )
                result = self.run_gpu("down")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ownership refusal", result.stderr)
                self.assertEqual(self.mutations(), [])

    def test_cleanup_state_does_not_authorize_foreign_volume(self) -> None:
        state = {
            "id": "foreign-root",
            "owner": "dev-test",
            "project": "test-project",
            "zone": "fr-par-2",
        }
        (self.root / "BRAIN/gpu-root-dev-test.json").write_text(json.dumps(state))
        self.save(
            {"block/volume": [{"id": "foreign-root", "tags": ["atlas-owner=other"]}]}
        )
        result = self.run_gpu("down")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(), [])

    def test_stable_opt_in_unchanged(self) -> None:
        for enabled in ("0", "1"):
            with patch.dict(
                os.environ,
                {"ATLAS_STABLE": "1", "ATLAS_STABLE_IMAGES": enabled},
                clear=True,
            ):
                self.assertEqual(images_enabled(), enabled == "1")
        with patch.dict(os.environ, {"ATLAS_DEV_GPU": "1"}, clear=True):
            self.assertTrue(images_enabled())

    def test_window_shortage_preserves_capacity_after_all_recoveries(self) -> None:
        self.save(self.fixture())
        self.env["GPU_SSH_PUBLIC_KEY"] = "ssh-ed25519 AAAA test"
        result = subprocess.run(
            ["bash", str(ROOT / "infra/stable/gpu-window/gpu_window.sh"), "2000"],
            env=self.env,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("deletion blocked", result.stdout)
        self.assertEqual(len(self.load()["instance/server"]), 1)
        self.assertTrue(self.load()["restarted"])
        self.assertEqual(len([c for c in self.mutations() if c[3] == "reboot"]), 1)
        self.assertFalse(any(c[3] in {"delete", "terminate"} for c in self.mutations()))
        self.assertGreaterEqual(self.load()["time"], 900)

    def test_window_transient_health_loss_keeps_server_until_deadline(self) -> None:
        data = self.fixture()
        data["health"] = "runtime-loss"
        self.save(data)
        self.env["GPU_SSH_PUBLIC_KEY"] = "ssh-ed25519 AAAA test"
        result = subprocess.run(
            ["bash", str(ROOT / "infra/stable/gpu-window/gpu_window.sh"), "250"],
            env=self.env,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("health restored; retaining server", result.stdout)
        self.assertIn("deadline reached: deleting", result.stdout)
        self.assertFalse(any(c[3] in {"reboot", "create"} for c in self.mutations()))
        self.assertEqual(self.load()["instance/server"], [])

    def test_window_recovers_by_restart_or_reboot_without_replacement(self) -> None:
        for mode in ("restart", "reboot"):
            with self.subTest(mode=mode):
                data = self.fixture()
                data["health"] = mode
                self.save(data)
                self.env["GPU_SSH_PUBLIC_KEY"] = "ssh-ed25519 AAAA test"
                result = subprocess.run(
                    [
                        "bash",
                        str(ROOT / "infra/stable/gpu-window/gpu_window.sh"),
                        "1000",
                    ],
                    env=self.env,
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("health restored; retaining server", result.stdout)
                self.assertNotIn("deleting for replacement", result.stdout)
                reboots = [c for c in self.mutations() if c[3] == "reboot"]
                self.assertEqual(len(reboots), int(mode == "reboot"))
                self.assertEqual(self.load()["instance/server"], [])

    def test_window_loop_reboots_once_and_releases_at_deadline_during_shortage(
        self,
    ) -> None:
        self.save(self.fixture())
        self.env["GPU_SSH_PUBLIC_KEY"] = "ssh-ed25519 AAAA test"
        result = subprocess.run(
            ["bash", str(ROOT / "infra/stable/gpu-window/gpu_window_loop.sh"), "2600"],
            env=self.env,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reboot already attempted", result.stdout)
        self.assertIn("deletion blocked", result.stdout)
        self.assertEqual(len([c for c in self.mutations() if c[3] == "reboot"]), 1)
        self.assertEqual(len([c for c in self.mutations() if c[3] == "terminate"]), 1)
        self.assertEqual(self.load()["instance/server"], [])

    def test_window_deletes_after_recovery_only_with_available_capacity(self) -> None:
        data = self.fixture()
        data["instance/server-type"][0]["availability"] = "available"
        self.save(data)
        self.env["GPU_SSH_PUBLIC_KEY"] = "ssh-ed25519 AAAA test"
        result = subprocess.run(
            ["bash", str(ROOT / "infra/stable/gpu-window/gpu_window.sh"), "2000"],
            env=self.env,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("capacity available: deleting", result.stdout)
        self.assertEqual(self.load()["instance/server"], [])
