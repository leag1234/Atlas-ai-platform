"""Container restart ownership regression."""

import unittest


class ContainerRestartTests(unittest.TestCase):
    def test_readiness_waits_after_connection_reset(self) -> None:
        from serve_idempotent import ready
        from unittest.mock import patch

        with patch("serve_idempotent.urlopen", side_effect=ConnectionResetError):
            self.assertFalse(ready())

    def test_replace_only_matching_container_image(self) -> None:
        from unittest.mock import patch, call
        from services.orchestrator.serving import docker_run

        with patch(
            "services.orchestrator.serving.docker",
            side_effect=["atlas-chat-db", "expected", "", ""],
        ) as docker:
            docker_run("atlas-chat-db", "expected", [])
        self.assertIn(call("rm", "-f", "atlas-chat-db"), docker.call_args_list)
        with patch(
            "services.orchestrator.serving.docker",
            side_effect=["atlas-chat-db", "foreign"],
        ) as docker:
            with self.assertRaisesRegex(RuntimeError, "container_owner_mismatch"):
                docker_run("atlas-chat-db", "expected", [])
        self.assertEqual(docker.call_count, 2)

    def test_legacy_launcher_without_lock_is_stopped(self) -> None:
        import signal
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from services.orchestrator.serve_owner import ownership

        with (
            tempfile.TemporaryDirectory() as root,
            patch(
                "services.orchestrator.serve_owner.Path.cwd", return_value=Path(root)
            ),
            patch(
                "services.orchestrator.serve_owner.previous_launchers",
                side_effect=[[1234567], []],
            ),
            patch("services.orchestrator.serve_owner.os.kill") as kill,
        ):
            with ownership():
                self.assertTrue((Path(root) / "BRAIN/serve.lock").exists())
        kill.assert_called_once_with(1234567, signal.SIGTERM)

    def test_stable_second_start_refuses_without_signalling_owner(self) -> None:
        import fcntl
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from services.orchestrator.serve_owner import ownership

        with tempfile.TemporaryDirectory() as root:
            brain = Path(root) / "BRAIN"
            brain.mkdir()
            lock_path = brain / "serve.lock"
            with lock_path.open("a+") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with (
                    patch.dict(os.environ, {"ATLAS_STABLE": "1"}, clear=False),
                    patch(
                        "services.orchestrator.serve_owner.Path.cwd",
                        return_value=Path(root),
                    ),
                    patch("services.orchestrator.serve_owner.os.kill") as kill,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "stable instance already running; use systemctl"
                    ):
                        with ownership():
                            pass
            kill.assert_not_called()

    def test_legacy_discovery_requires_matching_module_and_checkout(self) -> None:
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from services.orchestrator.serve_owner import previous_launchers

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc"
            proc.mkdir()
            for number, cwd, module in (
                (os.getpid() + 100000, root, "services.orchestrator.serving"),
                (os.getpid() + 100001, root.parent, "services.orchestrator.serving"),
                (os.getpid() + 100002, root, "unrelated"),
            ):
                entry = proc / str(number)
                entry.mkdir()
                (entry / "cwd").symlink_to(cwd, target_is_directory=True)
                (entry / "cmdline").write_bytes(f"python3\0-m\0{module}\0".encode())
            with patch("services.orchestrator.serve_owner.Path", return_value=proc):
                self.assertEqual(previous_launchers(root), [os.getpid() + 100000])

    def test_auto_removal_race_only_recovers_after_container_disappears(self) -> None:
        from unittest.mock import patch, call
        from services.orchestrator.serving import docker_run

        with patch(
            "services.orchestrator.serving.docker",
            side_effect=["atlas-chat-db", "expected", RuntimeError("removing"), "", ""],
        ) as docker:
            docker_run("atlas-chat-db", "expected", [])
        self.assertEqual(
            docker.call_args_list[-1],
            call("run", "--rm", "-d", "--name", "atlas-chat-db", "expected"),
        )
