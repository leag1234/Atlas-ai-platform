"""Launcher shutdown retains data and stops only the UI it started."""

from contextlib import nullcontext
import json
import os
import signal
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import Mock, patch

from services.orchestrator.serving import (
    main,
    require_stable_configuration,
    require_accepted_stop,
    searxng,
    stable_stop_allowed,
    stable_webui_options,
)


class ServingTests(unittest.TestCase):
    def test_stable_stop_decision_only_accepts_systemd_or_failing_stack(self) -> None:
        self.assertTrue(stable_stop_allowed(1, False))
        self.assertTrue(stable_stop_allowed(999, True))
        self.assertFalse(stable_stop_allowed(999, False))

    def test_stable_signal_from_test_process_is_ignored_and_attributed(self) -> None:
        code = textwrap.dedent(
            """
            import threading, time
            from services.orchestrator.serving import install_stop_handling
            stopping, failed = threading.Event(), threading.Event()
            install_stop_handling(stopping, failed)
            print('READY', flush=True)
            time.sleep(0.5)
            raise SystemExit(3 if stopping.is_set() else 0)
            """
        )
        environment = {**os.environ, "ATLAS_STABLE": "1"}
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=os.getcwd(),
            env=environment,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        self.assertEqual(process.stdout.readline().strip(), "READY")
        os.kill(process.pid, signal.SIGTERM)
        output = process.communicate(timeout=5)[0]
        self.assertEqual(process.returncode, 0)
        record = json.loads(output)
        self.assertEqual(record["event"], "stop_ignored")
        self.assertEqual(record["signal"], "SIGTERM")
        self.assertEqual(record["sender_pid"], os.getpid())
        self.assertEqual(
            set(record),
            {
                "event",
                "signal",
                "sender_pid",
                "sender_uid",
                "sender_command_line",
                "timestamp",
            },
        )

    def test_non_stable_signal_still_stops(self) -> None:
        code = textwrap.dedent(
            """
            import threading
            from services.orchestrator.serving import install_stop_handling
            stopping, failed = threading.Event(), threading.Event()
            install_stop_handling(stopping, failed)
            print('READY', flush=True)
            while not stopping.wait(0.1):
                pass
            """
        )
        environment = {
            key: value for key, value in os.environ.items() if key != "ATLAS_STABLE"
        }
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=os.getcwd(),
            env=environment,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        self.assertEqual(process.stdout.readline().strip(), "READY")
        os.kill(process.pid, signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_unexpected_stack_end_is_logged_and_nonzero(self) -> None:
        stopping, failed = Mock(), Mock()
        stopping.is_set.return_value = False
        with patch("services.orchestrator.serving.log_json") as log_json:
            with self.assertRaisesRegex(RuntimeError, "stack_context_ended"):
                require_accepted_stop(stopping, failed)
        failed.set.assert_called_once_with()
        log_json.assert_called_once_with("stack_stopped", reason="stack_context_ended")

    def test_searxng_is_loopback_only_and_offset_owned(self) -> None:
        with (
            patch.dict(os.environ, {"ATLAS_PORT_BASE": "1800"}, clear=True),
            patch("services.orchestrator.serving.docker_run") as docker_run,
            patch("services.orchestrator.serving.wait_http") as wait_http,
            patch("services.orchestrator.serving.docker") as docker,
        ):
            with searxng():
                pass
        name, image, options = docker_run.call_args.args
        self.assertEqual(name, "atlas-searxng-1800")
        self.assertIn("@sha256:", image)
        self.assertIn("127.0.0.1:10688:8080", options)
        self.assertIn("atlas-searxng-data-1800:/var/cache/searxng", options)
        self.assertIn("GRANIAN_LOG_ACCESS_ENABLED=false", options)
        self.assertEqual(wait_http.call_args.args[0], "http://127.0.0.1:10688/healthz")
        docker.assert_called_once_with("stop", name)

    def test_stable_options_are_off_by_default_and_complete_when_enabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(stable_webui_options(), [])
        with patch.dict(
            os.environ,
            {
                "ATLAS_STABLE": "1",
                "WEBUI_SECRET_KEY": "test-only",
                "ATLAS_PROXY_SECRET": "test-only",
                "ATLAS_TRUSTED_EMAIL_HEADER": "X-Atlas-User",
            },
            clear=True,
        ):
            require_stable_configuration()
            options = stable_webui_options()
        self.assertIn("WEBUI_AUTH=True", options)
        self.assertIn("WEBUI_AUTH_TRUSTED_EMAIL_HEADER=X-Atlas-User", options)
        self.assertIn("WEBUI_AUTH_TRUSTED_ROLE_HEADER=X-Atlas-User-Role", options)
        self.assertIn("ENABLE_SIGNUP=False", options)
        self.assertIn("ENABLE_FORWARD_USER_INFO_HEADERS=True", options)
        self.assertIn("ENABLE_API_KEYS=False", options)
        self.assertIn("ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS=True", options)
        self.assertIn(
            "API_KEYS_ALLOWED_ENDPOINTS=/api/models,/api/chat/completions,/api/v1/chat/completions",
            options,
        )
        with patch.dict(
            os.environ,
            {
                "ATLAS_STABLE": "1",
                "ATLAS_TRUSTED_EMAIL_HEADER": "X-Atlas-User",
                "ATLAS_OPENWEBUI_API_KEYS": "1",
            },
            clear=True,
        ):
            self.assertIn("ENABLE_API_KEYS=True", stable_webui_options())
        with patch.dict(os.environ, {"ATLAS_STABLE": "1"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "WEBUI_SECRET_KEY"):
                require_stable_configuration()
        with patch.dict(
            os.environ,
            {
                "ATLAS_STABLE": "1",
                "WEBUI_SECRET_KEY": "test-only",
                "ATLAS_PROXY_SECRET": "test-only",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "ATLAS_TRUSTED_EMAIL_HEADER"):
                require_stable_configuration()

    def test_main_cleanup_on_readiness_failure_and_normal_stop(self) -> None:
        event = Mock()
        event.wait.side_effect = [False, True]
        with (
            patch.dict(
                os.environ,
                {
                    "ESCALATION_MODEL": "test-only",
                    "SCW_GENERATIVE_BASE_URL": "https://example.invalid",
                    "SCW_GENERATIVE_API_KEY": "test-only",
                    "TAVILY_API_KEY": "test-only",
                    "ATLAS_SEARCH_PROVIDER": "tavily",
                    "ATLAS_PORT_BASE": "0",
                },
            ),
            patch(
                "services.orchestrator.serve_owner.ownership",
                return_value=nullcontext(),
            ),
            patch("services.orchestrator.serving.stack"),
            patch(
                "services.orchestrator.terminal_stack.terminal",
                return_value=nullcontext(),
            ),
            patch(
                "services.orchestrator.terminal_stack.loopback_html_injector",
                return_value=nullcontext(),
            ),
            patch("services.orchestrator.terminal_stack.adapter_socket"),
            patch("services.orchestrator.serving.uvicorn.Server"),
            patch("services.orchestrator.serving.threading.Thread"),
            patch("services.orchestrator.serving.webui"),
            patch("services.orchestrator.serving.configure"),
            patch("services.orchestrator.serving.threading.Event", return_value=event),
            patch("services.orchestrator.serving.docker") as docker,
        ):
            docker.side_effect = (
                lambda *args: '[{"NetworkSettings":{"Networks":{"atlas-files":{"IPAddress":"172.30.0.2"}}}}]'
                if args[0] == "inspect"
                else ""
            )
            for error in (None, RuntimeError("service_start_timeout")):
                with patch(
                    "services.orchestrator.serving.wait_http", side_effect=error
                ):
                    if error:
                        with self.assertRaises(RuntimeError):
                            main()
                    else:
                        main()
                docker.assert_called_with("stop", "atlas-chat-ui")
            with patch.dict(os.environ, {"ESCALATION_MODEL": ""}):
                with self.assertRaisesRegex(RuntimeError, "missing_configuration"):
                    main()
