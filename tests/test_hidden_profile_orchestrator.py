"""Hidden profiles are accepted by name without changing visible defaults."""

import os
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from evals.code_tasks.work_adapter import WorkAdapter
from packages.api_content import assistant_name
from packages.content_policy import extended_profiles
from packages.profiles import PROFILES, context_window
from services.orchestrator.chat_api import app
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.code_mode import code_profile, configure_work_model
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.project_ui import project_asset
from services.orchestrator.webui import configure as configure_webui, managed_model

from fastapi.testclient import TestClient


HIDDEN = "atlas-mistral"
HIDDEN_ENVIRONMENT = {"ATLAS_HIDDEN_PROFILES": HIDDEN}


class Context:
    def __init__(self, value: object) -> None:
        self.value = value

    def __enter__(self) -> object:
        return self.value

    def __exit__(self, *args: object) -> None:
        pass


class Body:
    def __init__(self, value: object) -> None:
        self.value = value

    def read(self) -> str:
        return json.dumps(self.value)


class HiddenProfileOrchestratorTests(unittest.TestCase):
    def test_model_lists_and_ui_defaults_stay_public(self) -> None:
        visible = list(PROFILES)
        for environment in ({}, HIDDEN_ENVIRONMENT):
            with (
                self.subTest(environment=environment),
                patch.dict(os.environ, environment),
            ):
                with TestClient(app) as client:
                    chat_models = client.get("/v1/models").json()["data"]
                self.assertEqual([model["id"] for model in chat_models], visible)
                self.assertEqual(
                    [managed_model(profile)["id"] for profile in PROFILES], visible
                )
                script = bytes(project_asset("client.js").body).decode()
                self.assertIn(
                    "window.atlasProjectProfile = " + f'"{visible[0]}"', script
                )
                self.assertIn(
                    "window.atlasProjectWorkProfile = " + f'"{code_profile()}"', script
                )
                self.assertEqual(code_profile(), visible[1])
                self.assertEqual(extended_profiles(), ())

    def test_webui_configure_only_touches_visible_profiles(self) -> None:
        calls: list[tuple[str, dict[str, object] | None]] = []

        def call(path: str, body: dict[str, object] | None = None) -> dict[str, object]:
            calls.append((path, body))
            if path == "/auths/signin":
                return {"token": "test-token"}
            if path == "/functions/list":
                return {"items": []}
            if path == "/functions/create":
                return {
                    "id": "atlas_image_references",
                    "is_active": True,
                    "is_global": True,
                }
            return {}

        with (
            patch.dict(os.environ, HIDDEN_ENVIRONMENT),
            patch("services.orchestrator.webui.build_opener") as opener_factory,
        ):
            opener = opener_factory.return_value
            opener.open.side_effect = lambda request, timeout: Context(
                Body(
                    call(
                        request.full_url.removeprefix("http://ui.invalid/api/v1"),
                        json.loads(request.data) if request.data else None,
                    )
                )
            )
            configure_webui("http://ui.invalid", {"X-Test": "1"})
        model_bodies = [body for _, body in calls if body is not None]
        self.assertEqual(
            [
                body["id"]
                for body in model_bodies
                if "id" in body and body["id"] in PROFILES
            ],
            list(PROFILES),
        )
        self.assertNotIn(HIDDEN, [body["id"] for body in model_bodies if "id" in body])
        opener_factory.assert_called_once()

    def test_chat_request_accepts_enabled_hidden_profile_only(self) -> None:
        payload = {"model": HIDDEN, "messages": [{"role": "user", "content": "Work"}]}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATLAS_HIDDEN_PROFILES", None)
            with self.assertRaisesRegex(ValueError, "invalid_profile"):
                ChatRequest.model_validate(payload)
        with patch.dict(os.environ, HIDDEN_ENVIRONMENT):
            request = ChatRequest.model_validate(payload)
            self.assertEqual(request.model, HIDDEN)
            self.assertEqual(context_window(HIDDEN), 1048576)
            self.assertTrue(assistant_name(HIDDEN))

    def test_work_model_and_reasoning_room_use_hidden_profile(self) -> None:
        configuration = Configuration(
            input_eur_per_mtok=Decimal("0.595"),
            output_eur_per_mtok=Decimal("1.785"),
            max_tokens=2048,
            profile_reasoning_output_tokens={HIDDEN: 2000},
        )
        with patch.dict(os.environ, HIDDEN_ENVIRONMENT):
            request = ChatRequest.model_validate(
                {
                    "model": HIDDEN,
                    "project_action": "work",
                    "project_id": "1f0d3a4c-5b06-4a8e-9c1d-2f3b4a5c6d7e",
                    "conversation_id": "2f0d3a4c-5b06-4a8e-9c1d-2f3b4a5c6d7e",
                    "work_session_id": "0f0d3a4c-5b06-4a8e-9c1d-2f3b4a5c6d7e",
                    "messages": [{"role": "user", "content": "Repair the fixture"}],
                }
            )
            model = MagicMock()
            configure_work_model(model, request)
            self.assertEqual(model.configure_quality.call_args.args[0], HIDDEN)

            gateway_model = GatewayModel("http://gateway", configuration)
            gateway_model.configure_quality(HIDDEN, request.max_tokens)
            self.assertEqual(gateway_model.profile, HIDDEN)
            self.assertEqual(gateway_model.reasoning_room, 2000)
            gateway_model.configure_quality("atlas-glm", request.max_tokens)
            self.assertEqual(gateway_model.reasoning_room, 0)

    def test_work_adapter_profile_is_configurable_and_default_is_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            default = WorkAdapter("fixture", "fixture", Path(directory))
            hidden = WorkAdapter("fixture", "fixture", Path(directory), profile=HIDDEN)
        self.assertEqual(default.profile, "atlas-qwen")
        self.assertEqual(hidden.profile, HIDDEN)
