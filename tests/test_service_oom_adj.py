"""Long-lived service containers outlive memory pressure; per-turn containers do not."""

import json
import os
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from services.orchestrator import serving


def run_arguments(env: dict[str, str]) -> list[str]:
    """The ``docker run`` arguments docker_run sends for a fresh container."""
    with (
        patch.dict(os.environ, env, clear=True),
        patch.object(serving, "docker", return_value="") as docker,
    ):
        serving.docker_run("atlas-chat-db", "pgvector/pgvector:pg16", ["-e", "X=1"])
    run = [c.args for c in docker.call_args_list if c.args[0] == "run"]
    assert len(run) == 1
    return list(run[0])


def oom_value(arguments: list[str]) -> str | None:
    if "--oom-score-adj" not in arguments:
        return None
    return arguments[arguments.index("--oom-score-adj") + 1]


class ServiceOomScoreTests(unittest.TestCase):
    def test_hosted_instance_protects_its_service_containers_by_default(self) -> None:
        arguments = run_arguments({"ATLAS_STABLE": "1"})
        self.assertEqual(oom_value(arguments), "-500")
        # Docker options precede the image name.
        self.assertLess(
            arguments.index("--oom-score-adj"),
            arguments.index("pgvector/pgvector:pg16"),
        )

    def test_local_and_test_stacks_keep_docker_default(self) -> None:
        self.assertIsNone(oom_value(run_arguments({})))
        self.assertIsNone(oom_value(run_arguments({"ATLAS_STABLE": "0"})))

    def test_setting_overrides_and_zero_disables(self) -> None:
        env = {"ATLAS_STABLE": "1", "ATLAS_SERVICE_OOM_SCORE_ADJ": "-300"}
        self.assertEqual(oom_value(run_arguments(env)), "-300")
        env["ATLAS_SERVICE_OOM_SCORE_ADJ"] = "0"
        self.assertIsNone(oom_value(run_arguments(env)))
        self.assertEqual(
            oom_value(run_arguments({"ATLAS_SERVICE_OOM_SCORE_ADJ": "-200"})), "-200"
        )

    def test_invalid_setting_fails_before_docker(self) -> None:
        for value in ("abc", "-1001", "1001", "1.5", "- 5"):
            with (
                self.subTest(value=value),
                patch.dict(
                    os.environ,
                    {"ATLAS_STABLE": "1", "ATLAS_SERVICE_OOM_SCORE_ADJ": value},
                    clear=True,
                ),
                patch.object(serving, "docker") as docker,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "invalid_configuration: ATLAS_SERVICE_OOM_SCORE_ADJ"
                ):
                    serving.require_stable_configuration()
                with self.assertRaisesRegex(
                    RuntimeError, "ATLAS_SERVICE_OOM_SCORE_ADJ"
                ):
                    serving.docker_run("atlas-chat-db", "image", [])
                docker.assert_not_called()

    def test_files_terminal_service_is_protected(self) -> None:
        from services.orchestrator import terminal_stack

        calls: list[tuple[str, ...]] = []

        def command(*args: str) -> str:
            calls.append(args)
            if args[1:3] == ("network", "ls"):
                return "atlas-files-300"
            if args[1] == "inspect":
                return json.dumps(
                    [
                        {
                            "NetworkSettings": {
                                "Networks": {
                                    "atlas-files-300": {"IPAddress": "172.30.0.2"}
                                }
                            }
                        }
                    ]
                )
            return ""

        env = {
            "ATLAS_STABLE": "1",
            "ATLAS_PORT_BASE": "300",
            "OPEN_TERMINAL_API_KEY": "test-only",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(terminal_stack, "command", side_effect=command),
            patch.object(terminal_stack, "network_gateway"),
            patch.object(terminal_stack, "contain"),
            patch.object(
                terminal_stack, "loopback_forward", return_value=nullcontext()
            ),
            patch.object(serving, "wait_http"),
        ):
            with terminal_stack.terminal("atlas-open-terminal", "127.0.0.1:0"):
                pass
        run = [c for c in calls if c[1] == "run"]
        self.assertEqual(len(run), 1)
        self.assertEqual(oom_value(list(run[0])), "-500")

    def test_per_turn_work_containers_stay_killable_first(self) -> None:
        from services.orchestrator import work_opencode_container as container

        class Workspace:
            key = "org/project/session/ws/g1"
            scope_key = "org/project/session/ws"
            scoped = True
            proxy_host = "egress-abc"

        with patch.dict(os.environ, {"ATLAS_STABLE": "1"}):
            arguments = container.docker_arguments(
                "engine-1",
                Workspace(),  # type: ignore[arg-type]
                "atlas-work-egress",
                "{}",
            )
        self.assertEqual(arguments[:3], ["docker", "run", "-d"])
        self.assertNotIn("--oom-score-adj", arguments)


if __name__ == "__main__":
    unittest.main()
