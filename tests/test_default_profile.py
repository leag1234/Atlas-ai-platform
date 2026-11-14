"""ATLAS_DEFAULT_PROFILE: the profile of new conversations and of model-less API calls."""

import json
import os
import unittest
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from infra.openwebui_images import ATLAS_PROFILES
from packages.profiles import (
    HIDDEN_PROFILES,
    PROFILES,
    chat_ui_profiles,
    default_profile,
)
from services.orchestrator import serving
from services.orchestrator.chat_api import app
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.project_ui import project_asset
from services.orchestrator.webui import configure as configure_webui, managed_model


HIDDEN = HIDDEN_PROFILES[0]
EVERYONE = [{"principal_type": "user", "principal_id": "*", "permission": "read"}]
QUESTION = {"messages": [{"role": "user", "content": "Hello"}]}


def unset() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in {"ATLAS_DEFAULT_PROFILE", "ATLAS_HIDDEN_PROFILES"}
    }


def project_profile() -> str:
    script = bytes(project_asset("client.js").body).decode()
    line = next(
        row
        for row in script.splitlines()
        if row.startswith("window.atlasProjectProfile")
    )
    return str(json.loads(line.split(" = ", 1)[1].rstrip(";")))


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


def configured_models() -> list[dict[str, Any]]:
    """The model bodies the chat UI receives when the trusted relay configures it."""
    bodies: list[dict[str, Any]] = []

    def call(path: str, body: dict[str, object] | None) -> dict[str, object]:
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
        if body is not None and path.startswith("/models/"):
            bodies.append(body)
        return {}

    with patch("services.orchestrator.webui.build_opener") as opener_factory:
        opener_factory.return_value.open.side_effect = lambda request, timeout: Context(
            Body(
                call(
                    request.full_url.removeprefix("http://ui.invalid/api/v1"),
                    json.loads(request.data) if request.data else None,
                )
            )
        )
        configure_webui("http://ui.invalid", {"X-Test": "1"})
    return bodies


class DefaultProfileTests(unittest.TestCase):
    def test_unset_keeps_the_first_public_profile_everywhere(self) -> None:
        for environment in ({}, {"ATLAS_HIDDEN_PROFILES": HIDDEN}):
            with (
                self.subTest(environment=environment),
                patch.dict(os.environ, unset() | environment, clear=True),
            ):
                self.assertEqual(default_profile(), PROFILES[0])
                self.assertEqual(chat_ui_profiles(), PROFILES)
                self.assertEqual(
                    ChatRequest.model_validate(QUESTION).model, PROFILES[0]
                )
                self.assertEqual(project_profile(), PROFILES[0])
                with TestClient(app) as client:
                    listed = client.get("/v1/models").json()["data"]
                self.assertEqual([model["id"] for model in listed], list(PROFILES))
                public = managed_model(PROFILES[0])
                self.assertEqual(public["name"], "Atlas")
                self.assertEqual(public["access_grants"], EVERYONE)
                self.assertNotIn("hidden", public["meta"])
                for profile in PROFILES[1:]:
                    self.assertEqual(managed_model(profile)["name"], profile)
                    self.assertEqual(managed_model(profile)["access_grants"], [])
                self.assertEqual(
                    [body["id"] for body in configured_models()], list(PROFILES)
                )

    def test_hidden_default_starts_new_conversations_and_keeps_the_list(self) -> None:
        environment = {"ATLAS_HIDDEN_PROFILES": HIDDEN, "ATLAS_DEFAULT_PROFILE": HIDDEN}
        with patch.dict(os.environ, unset() | environment, clear=True):
            self.assertEqual(default_profile(), HIDDEN)
            self.assertEqual(ChatRequest.model_validate(QUESTION).model, HIDDEN)
            # A conversation that names its profile keeps it.
            self.assertEqual(
                ChatRequest.model_validate(QUESTION | {"model": PROFILES[0]}).model,
                PROFILES[0],
            )
            self.assertEqual(project_profile(), HIDDEN)
            with TestClient(app) as client:
                listed = client.get("/v1/models").json()["data"]
            self.assertEqual([model["id"] for model in listed], [*PROFILES, HIDDEN])
            models = {body["id"]: body for body in configured_models()}
            self.assertEqual(list(models), [*PROFILES, HIDDEN])
            # Users still see one model, Atlas, which is now the default profile.
            shown = [
                body["name"]
                for body in models.values()
                if body["access_grants"] == EVERYONE and not body["meta"].get("hidden")
            ]
            self.assertEqual(shown, ["Atlas"])
            self.assertEqual(models[HIDDEN]["name"], "Atlas")
            self.assertEqual(models[HIDDEN]["access_grants"], EVERYONE)
            self.assertNotIn("hidden", models[HIDDEN]["meta"])
            # The previous default leaves the selector but stays readable, so a
            # conversation already on it goes on, and the API still accepts it.
            previous = models[PROFILES[0]]
            self.assertEqual(previous["name"], "Atlas")
            self.assertEqual(previous["access_grants"], EVERYONE)
            self.assertIs(previous["meta"]["hidden"], True)
            for profile in PROFILES[1:]:
                self.assertEqual(models[profile]["name"], profile)
                self.assertEqual(models[profile]["access_grants"], [])

    def test_public_default_moves_atlas_to_that_profile(self) -> None:
        with patch.dict(
            os.environ, unset() | {"ATLAS_DEFAULT_PROFILE": PROFILES[1]}, clear=True
        ):
            self.assertEqual(chat_ui_profiles(), PROFILES)
            self.assertEqual(ChatRequest.model_validate(QUESTION).model, PROFILES[1])
            self.assertEqual(managed_model(PROFILES[1])["name"], "Atlas")
            self.assertIs(managed_model(PROFILES[0])["meta"]["hidden"], True)

    def test_unknown_or_disabled_default_stops_the_start(self) -> None:
        for environment in (
            {"ATLAS_DEFAULT_PROFILE": "atlas-unknown"},
            {"ATLAS_DEFAULT_PROFILE": HIDDEN},
        ):
            with (
                self.subTest(environment=environment),
                patch.dict(os.environ, unset() | environment, clear=True),
                patch.object(serving, "require_stable_configuration"),
                patch.object(serving, "require_env") as require_env,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "invalid_configuration: ATLAS_DEFAULT_PROFILE="
                    + environment["ATLAS_DEFAULT_PROFILE"]
                    + " .*ATLAS_HIDDEN_PROFILES",
                ):
                    serving.run()
                require_env.assert_not_called()

    def test_chat_ui_filter_covers_every_profile(self) -> None:
        self.assertEqual(ATLAS_PROFILES, frozenset((*PROFILES, *HIDDEN_PROFILES)))


if __name__ == "__main__":
    unittest.main()
