"""Work execution image: pinned Go toolchain, offline defaults and sandbox limits."""

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from services.orchestrator import code_worker, terminal_stack
from services.orchestrator import work_opencode_container as container
from services.orchestrator.code_tools import DESCRIPTIONS, Command
from services.orchestrator.code_workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ROOT / "infra/docker"
DIGEST = "sha256:" + "a" * 64


class WorkImageConfigurationTests(unittest.TestCase):
    def test_default_is_the_pinned_upstream_image(self) -> None:
        self.assertEqual(terminal_stack.work_image(""), terminal_stack.UPSTREAM_IMAGE)
        self.assertRegex(terminal_stack.UPSTREAM_IMAGE, r"@sha256:[0-9a-f]{64}$")

    def test_override_must_be_pinned(self) -> None:
        self.assertEqual(terminal_stack.work_image(DIGEST), DIGEST)
        self.assertEqual(
            terminal_stack.work_image(" atlas-work@" + DIGEST + " "),
            "atlas-work@" + DIGEST,
        )
        for invalid in (
            "atlas-work:local",
            "atlas-work",
            "sha256:abc",
            "a b@" + DIGEST,
        ):
            with self.assertRaisesRegex(RuntimeError, "ATLAS_WORK_IMAGE"):
                terminal_stack.work_image(invalid)

    def test_environment_is_read_when_no_value_is_given(self) -> None:
        with patch.dict(os.environ, {"ATLAS_WORK_IMAGE": DIGEST}):
            self.assertEqual(terminal_stack.work_image(), DIGEST)

    def test_go_is_announced_only_for_the_work_image(self) -> None:
        upstream = terminal_stack.toolchains(terminal_stack.UPSTREAM_IMAGE)
        self.assertNotIn("Go", upstream)
        self.assertIn("Node.js", upstream)
        self.assertTrue(terminal_stack.toolchains(DIGEST).endswith(" and Go"))

    def test_run_command_states_the_toolchains(self) -> None:
        description = DESCRIPTIONS["run_command"]
        self.assertIn(terminal_stack.TOOLCHAINS, description)
        self.assertIn("node --test", description)
        self.assertIn("timeout_seconds 120", description)

    def test_default_command_memory_fits_a_cold_go_build(self) -> None:
        self.assertEqual(Command(command="go test ./...").memory_mb, 512)


class WorkImageBuildTests(unittest.TestCase):
    def test_images_share_the_pinned_base_and_go_installer(self) -> None:
        for name in ("Dockerfile.work", "Dockerfile.opencode-engine"):
            text = (DOCKER / name).read_text()
            self.assertEqual(
                re.findall(r"^FROM (\S+)", text, re.M),
                [terminal_stack.UPSTREAM_IMAGE],
                name,
            )
            self.assertIn("RUN sh /usr/local/share/atlas/install-go.sh", text, name)

    def test_go_archive_is_pinned_and_verified(self) -> None:
        script = (DOCKER / "install-go.sh").read_text()
        self.assertRegex(script, r"(?m)^GO_VERSION=1\.\d+\.\d+$")
        self.assertEqual(
            len(re.findall(r"GO_SHA256=[0-9a-f]{64} ;;", script)), 2
        )  # amd64 and arm64
        self.assertIn("sha256sum -c -", script)
        self.assertIn("https://dl.google.com/go/", script)

    def test_go_defaults_are_offline(self) -> None:
        script = (DOCKER / "install-go.sh").read_text()
        for line in ("GOTOOLCHAIN=local", "GOPROXY=off", "GOSUMDB=off"):
            self.assertRegex(script, "(?m)^" + re.escape(line) + "$")
        # go.env already defines these keys and the first occurrence wins.
        self.assertIn("sed -i -E '/^(GOTOOLCHAIN|GOPROXY|GOSUMDB|GOFLAGS)=/d'", script)
        self.assertIn("ln -sf /usr/local/go/bin/go /usr/local/bin/go", script)


def option(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


class SandboxFlagsTests(unittest.TestCase):
    def test_worker_tmp_allows_compiled_binaries_and_home_is_set(self) -> None:
        for egress in ("", "registries"):
            with (
                tempfile.TemporaryDirectory() as state,
                patch.dict(
                    os.environ,
                    {"ATLAS_WORK_EGRESS": egress, "ATLAS_CODE_STATE_DIR": state},
                ),
            ):
                args = Workspace(str(uuid4())).docker_arguments([], "pass")
            tmpfs = option(args, "--tmpfs")
            self.assertTrue(tmpfs.startswith("/tmp:"), tmpfs)
            self.assertIn("exec", tmpfs.split(":", 1)[1].split(","))
            self.assertNotIn("noexec", tmpfs)
            self.assertIn("HOME=/home/atlas", args)
            self.assertIn("--read-only", args)
            self.assertEqual(option(args, "--cap-drop"), "ALL")
            self.assertEqual(
                "npm_config_offline=true" in args, egress == "", egress or "off"
            )

    def test_engine_tmp_allows_compiled_binaries(self) -> None:
        source = Path(container.__file__).read_text()
        self.assertIn('"/tmp:rw,exec,nosuid,nodev,size=256m,uid=1000,gid=1000"', source)

    def test_home_volume_is_mounted_without_egress(self) -> None:
        source = (ROOT / "services/orchestrator/code_workspace.py").read_text()
        self.assertIn(
            "\"os.makedirs(p + '.home', exist_ok=True); os.chown(p + '.home',1000,1000)\\n\"\n",
            source,
        )
        self.assertNotIn('if self.egress\n                else ""', source)


class CommandMemoryLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_address_space_reservations_are_not_counted(self) -> None:
        # Go and Node.js reserve gigabytes of address space without writing to it.
        reserve = (
            "python3 -c 'import mmap; "
            "m = mmap.mmap(-1, 4 << 30, prot=mmap.PROT_READ); print(len(m))'"
        )
        result = code_worker.command(self.root, reserve, 10, 64)
        self.assertEqual(result["exit_code"], 0, result["output"])
        self.assertEqual(str(result["output"]).strip(), str(4 << 30))

    def test_written_memory_is_still_bounded(self) -> None:
        result = code_worker.command(
            self.root, "python3 -c 'a = bytearray(256 * 1024 * 1024)'", 10, 64
        )
        self.assertNotEqual(result["exit_code"], 0)
        self.assertIn("MemoryError", str(result["output"]))


if __name__ == "__main__":
    unittest.main()
