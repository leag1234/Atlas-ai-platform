"""The gateway's "locate" vision role (labels of CPU diagrams) and the rewrite role."""

import base64
import io
import os
import unittest
from unittest.mock import patch

import yaml
from PIL import Image
from pydantic import ValidationError

from agent_provider import AgentRequest
from packages.profiles import PICTURE_ROLES, picture_profile
from serverless import ServerlessPolicy
from serverless_support import environment
from test_picture_sampling import _body

ROUTING = yaml.safe_load(open("services/model-gateway/routing.yaml").read())


def _image() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 220, 240)).save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _request(**extra: object) -> AgentRequest:
    return AgentRequest(
        messages=[
            {"role": "system", "content": "Locate the parts."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": 'Labels: ["stem"]'},
                    {"type": "image_url", "image_url": {"url": _image()}},
                ],
            },
        ],
        tools=[],
        timeout=10.0,
        profile="atlas-deepseek",
        **extra,  # type: ignore[arg-type]
    )


class LocateFieldTests(unittest.TestCase):
    def test_default_is_not_serialised(self) -> None:
        with patch.dict(os.environ, environment()):
            self.assertNotIn("vision_role", _request().model_dump())
            self.assertEqual(
                _request(vision_role="locate").model_dump()["vision_role"], "locate"
            )

    def test_unknown_role_and_legacy_calls_are_refused(self) -> None:
        with patch.dict(os.environ, environment()):
            with self.assertRaises(ValidationError):
                _request(vision_role="text")
            with self.assertRaisesRegex(ValidationError, "invalid_legacy_profile"):
                AgentRequest(
                    messages=[{"role": "user", "content": "hello"}],
                    tools=[],
                    timeout=5.0,
                    vision_role="locate",
                )


class LocateRoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_locate_runs_on_its_declared_model(self) -> None:
        locate = ROUTING["serverless"]["locate"]["model"]
        vision = ROUTING["serverless"]["vision"]["model"]
        self.assertNotEqual(locate, vision)
        body = await _body(_request(vision_role="locate"), environment())
        self.assertEqual(body["model"], locate)
        body = await _body(_request(), environment())
        self.assertEqual(body["model"], vision)

    def test_locate_model_is_priced_and_sees_images(self) -> None:
        with patch.dict(os.environ, environment()):
            policy = ServerlessPolicy()
        model = policy.models["locate"]
        self.assertTrue(policy.config["locate"]["capabilities"][model]["vision"])
        self.assertGreater(policy.cost(model, 1000, 100), 0)
        self.assertEqual(policy.config["locate"]["fallback"], "vision")


class RewriteRoleTests(unittest.TestCase):
    def test_rewrite_role_is_declared_on_a_public_profile(self) -> None:
        self.assertIn("picture_rewrite", PICTURE_ROLES)
        with patch.dict(os.environ, environment()):
            os.environ.pop("ATLAS_HIDDEN_PROFILES", None)
            self.assertEqual(picture_profile("picture_rewrite"), "atlas-deepseek")
