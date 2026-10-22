"""M24 stack wiring and least-privilege UI connection contract."""

import json
import os
import unittest
from unittest.mock import patch

from packages.ports import DEFAULT_PORTS
from services.orchestrator import serving


class TerminalLifecycleTests(unittest.TestCase):
    def test_ui_uses_shared_network_and_runtime_key(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"OPEN_TERMINAL_API_KEY": "test-only", "ATLAS_PORT_BASE": "0"},
            ),
            patch(
                "services.orchestrator.terminal_stack.network_gateway",
                return_value="172.30.0.1",
            ),
            patch.object(serving, "docker_run") as run,
        ):
            serving.webui("test-ui", False)
            connection = json.loads(os.environ["TERMINAL_SERVER_CONNECTIONS"])[0]
        options = run.call_args.args[2]
        self.assertEqual(options[options.index("--network") + 1], "atlas-files")
        self.assertNotIn("-p", options)
        self.assertIn("HOST=0.0.0.0", options)
        self.assertIn(
            "OPENAI_API_BASE_URL=http://172.30.0.1:"
            + str(DEFAULT_PORTS["adapter"])
            + "/v1",
            options,
        )
        self.assertIn("TERMINAL_SERVER_CONNECTIONS", options)
        self.assertEqual(connection["url"], "http://open-terminal:8000")
        self.assertEqual(connection["config"]["chat_uploads"], "filesystem")
        self.assertEqual(connection["key"], "test-only")

    def test_offset_stack_suffixes_the_ui_container_once(self) -> None:
        from contextlib import nullcontext

        from services.orchestrator.terminal_stack import user_interface

        inspect = '[{"NetworkSettings":{"Networks":{"atlas-files-300":{"IPAddress":"172.30.0.2"}}}}]'
        with (
            patch.dict(os.environ, {"ATLAS_PORT_BASE": "300"}),
            patch("services.orchestrator.terminal_stack.adapter_socket"),
            patch("services.orchestrator.terminal_stack.uvicorn.Server"),
            patch("services.orchestrator.terminal_stack.threading.Thread"),
            patch(
                "services.orchestrator.terminal_stack.loopback_html_injector",
                return_value=nullcontext(),
            ),
            patch.object(serving, "wait_http"),
            patch.object(serving, "webui") as webui,
            patch.object(
                serving,
                "docker",
                side_effect=lambda *args: inspect if args[0] == "inspect" else "",
            ) as docker,
        ):
            with user_interface("atlas-chat-ui", True):
                pass
        self.assertEqual(webui.call_args.args[0], "atlas-chat-ui")
        docker.assert_any_call("inspect", "atlas-chat-ui-300")
        docker.assert_called_with("stop", "atlas-chat-ui-300")
