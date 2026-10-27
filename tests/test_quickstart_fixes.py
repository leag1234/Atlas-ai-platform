"""Fresh-trial setup, managed inlet startup and conversation status regressions."""

import argparse
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from typing import Any

from packages.profiles import MODEL_PROFILE_IMAGE_URL
import unittest
from unittest.mock import AsyncMock, patch

from infra.docker.webui_bootstrap import main as install_inlet
from infra.openwebui_images import Filter
from provider_settings import presets, request_options
from scripts.atlas_setup import check_connection, http_port, main as setup
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context


class SetupRegressionTests(unittest.TestCase):
    def probe(self, message: dict[str, object], provider: str = "hetzner") -> str:
        bodies: list[dict[str, Any]] = []

        def open_request(request: Any, **kwargs: Any) -> io.BytesIO:
            data: dict[str, Any]
            if request.data:
                bodies.append(json.loads(request.data))
                frames = [
                    {
                        "id": "probe",
                        "choices": [
                            {"index": 0, "delta": message, "finish_reason": "stop"}
                        ],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                    },
                ]
                return io.BytesIO(
                    (
                        "".join(
                            "data: " + json.dumps(frame) + "\n\n" for frame in frames
                        )
                        + "data: [DONE]\n\n"
                    ).encode()
                )
            else:
                data = {"data": [{"id": "test-model"}]}
            return io.BytesIO(json.dumps(data).encode())

        output = io.StringIO()
        with (
            patch("scripts.atlas_setup.build_opener") as opener,
            redirect_stdout(output),
        ):
            opener.return_value.open.side_effect = open_request
            check_connection(
                "https://example.org/v1", "test-value", "test-model", provider
            )
        self.assertEqual(len(bodies), 1)
        self.assertGreaterEqual(bodies[0]["max_tokens"], 1024)
        if provider == "hetzner":
            self.assertEqual(bodies[0]["reasoning_effort"], "none")
            self.assertEqual(
                bodies[0]["chat_template_kwargs"], {"enable_thinking": False}
            )
        self.assertNotIn("test-value", output.getvalue())
        return output.getvalue()

    def test_hetzner_visible_completion(self) -> None:
        self.assertIn("verified", self.probe({"content": "OK"}))

    def test_reasoning_only_is_reachable_with_warning(self) -> None:
        for field in ("reasoning", "reasoning_content"):
            with self.subTest(field=field):
                output = self.probe({"content": None, field: "reasoning"}, "other")
                self.assertIn("WARNING: provider reachable", output)
                self.assertIn("no visible answer was verified", output)
                self.assertNotIn("tiny completion verified", output)

    def test_empty_response_is_still_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty completion"):
            self.probe({"content": "", "reasoning_content": " "})

    def test_provider_options_override_reasoning_without_changing_input(self) -> None:
        for stream in (False, True):
            body = {
                "model": "chosen",
                "stream": stream,
                "reasoning_effort": "high",
                "tools": [{"type": "function"}],
            }
            with patch.dict(os.environ, {"ATLAS_PROVIDER": "hetzner"}, clear=True):
                output = request_options(body)
            self.assertEqual(output["reasoning_effort"], "none")
            self.assertEqual(output["chat_template_kwargs"], {"enable_thinking": False})
            self.assertEqual(output["stream"], stream)
            self.assertEqual(output["tools"], body["tools"])
            self.assertEqual(body["reasoning_effort"], "high")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(request_options(body), body)
        self.assertIn("request_options", presets()["hetzner"])

    def test_port_validation(self) -> None:
        self.assertEqual(http_port("13700"), "13700")
        for value in ("0", "65536", "-1", "abc", "3000:80", " 3000", "٣٠٠٠"):
            with (
                self.subTest(value=value),
                self.assertRaises(argparse.ArgumentTypeError),
            ):
                http_port(value)

    def test_setup_default_environment_and_flag_port(self) -> None:
        for environment, flags, port in (
            ({}, [], "3000"),
            ({"ATLAS_HTTP_PORT": "13701"}, [], "13701"),
            ({"ATLAS_HTTP_PORT": "13701"}, ["--http-port", "13700"], "13700"),
        ):
            with self.subTest(port=port), tempfile.TemporaryDirectory() as root:
                key = Path(root) / "key"
                key.write_text("test-value")
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch("scripts.atlas_setup.ROOT", Path(root)),
                    patch("scripts.atlas_setup.os.chdir"),
                    patch("scripts.atlas_setup.subprocess.run"),
                    patch("scripts.atlas_setup.check_connection"),
                    patch(
                        "sys.argv",
                        [
                            "atlas",
                            "setup",
                            "--provider",
                            "hetzner",
                            "--api-key-file",
                            str(key),
                            "--yes",
                            "--no-start",
                            *flags,
                        ],
                    ),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    self.assertEqual(setup(), 0)
                self.assertIn(
                    "ATLAS_HTTP_PORT='" + port + "'", (Path(root) / ".env").read_text()
                )
                self.assertIn("http://localhost:" + port, output.getvalue())


class InletStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_install_before_users_and_update_on_restart(self) -> None:
        functions = SimpleNamespace(
            get_function_by_id=AsyncMock(return_value=None),
            insert_new_function=AsyncMock(return_value=object()),
            update_function_by_id=AsyncMock(return_value=object()),
        )
        models = SimpleNamespace(
            get_model_by_id=AsyncMock(return_value=None),
            insert_new_model=AsyncMock(return_value=object()),
        )
        module = SimpleNamespace(
            Functions=functions,
            FunctionForm=SimpleNamespace,
            FunctionMeta=SimpleNamespace,
        )
        with (
            patch.dict(
                "sys.modules",
                {
                    "open_webui.models.functions": module,
                    "open_webui.models.models": SimpleNamespace(
                        Models=models,
                        ModelForm=lambda **kwargs: kwargs,
                        ModelMeta=lambda **kwargs: kwargs,
                        ModelParams=lambda **kwargs: kwargs,
                    ),
                },
            ),
            patch(
                "infra.docker.webui_bootstrap.Path.read_text",
                return_value="managed-source",
            ),
            redirect_stdout(io.StringIO()),
        ):
            await install_inlet()
            functions.get_function_by_id.return_value = object()
            await install_inlet()
        functions.insert_new_function.assert_awaited_once()
        self.assertEqual(
            functions.insert_new_function.call_args.args[0], "atlas-managed"
        )
        self.assertEqual(functions.update_function_by_id.await_count, 2)
        self.assertEqual(
            functions.update_function_by_id.call_args.args[1],
            {"content": "managed-source", "is_active": True, "is_global": True},
        )
        self.assertEqual(models.insert_new_model.await_count, 6)
        public = models.insert_new_model.call_args_list[0].args[0]
        self.assertEqual(public["name"], "Atlas")
        self.assertEqual(public["meta"]["description"], "")
        self.assertEqual(public["meta"]["profile_image_url"], MODEL_PROFILE_IMAGE_URL)
        self.assertEqual(public["access_grants"][0]["principal_id"], "*")

    async def test_failed_install_prevents_readiness(self) -> None:
        functions = SimpleNamespace(
            get_function_by_id=AsyncMock(return_value=None),
            insert_new_function=AsyncMock(return_value=None),
        )
        module = SimpleNamespace(
            Functions=functions,
            FunctionForm=SimpleNamespace,
            FunctionMeta=SimpleNamespace,
        )
        with (
            patch.dict("sys.modules", {"open_webui.models.functions": module}),
            patch(
                "infra.docker.webui_bootstrap.Path.read_text",
                return_value="managed-source",
            ),
            self.assertRaisesRegex(RuntimeError, "install_failed"),
        ):
            await install_inlet()


class StatusLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def test_inlet_and_stream_use_conversation_language(self) -> None:
        messages = [
            {
                "role": "user",
                "content": "Please search the web and explain how solar panels generate electricity.",
            },
            {"role": "assistant", "content": "They convert sunlight into electricity."},
            {"role": "user", "content": "Find sources."},
        ]
        events = AsyncMock()
        await Filter().inlet(
            {"model": "atlas-qwen", "messages": messages},
            __event_emitter__=events,
        )
        self.assertIn(
            "Thinking", events.call_args_list[0].args[0]["data"]["description"]
        )

        async def work(payload: ChatRequest, item: Interaction) -> None:
            sink = sink_context.get()
            assert sink is not None
            for tool in ("web_search", "web_fetch"):
                await sink({"phase": "tool_started", "tool": tool})
                await sink(
                    {
                        "phase": "tool_finished",
                        "tool": tool,
                        "urls": ["https://example.org/"],
                    }
                )
            item.reponse, item.state = (
                "Solar panels convert sunlight to electricity.",
                "done",
            )
            await sink({"delta": {"content": item.reponse}})

        payload = ChatRequest.model_validate({"stream": True, "messages": messages})
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
        ):
            stream = response(payload, Interaction(), time.monotonic(), work)
            rows = [
                json.loads(str(row).removeprefix("data: "))
                async for row in stream.body_iterator
                if str(row).startswith("data: {")
            ]
        descriptions = [
            row["event"]["data"]["description"] for row in rows if "event" in row
        ]
        text = "".join(
            row.get("choices", [{}])[0].get("delta", {}).get("content", "")
            for row in rows
        )
        self.assertTrue(any("Web search" in value for value in descriptions))
        self.assertTrue(any("Reading" in value for value in descriptions))
        self.assertIn("<summary>Reading", text)
        self.assertNotIn("Recherche", text + str(descriptions))
        self.assertNotIn("Lecture de la page", text + str(descriptions))
