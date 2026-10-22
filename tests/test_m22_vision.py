"""M22 public selectors share the configured vision primary and compatible fallback."""

from collections.abc import AsyncGenerator
import os
import copy
import unittest
from unittest.mock import patch
from agent_provider import AgentProvider, AgentRequest
from packages.profiles import PROFILES
from quality import stream_quality
from serverless import ServerlessPolicy
from serverless_support import environment
from test_quality_gateway import request, result
from test_vision_schema import payload, picture


class VisionRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_selectors_and_two_images_recover_on_previous_vision(
        self,
    ) -> None:
        for profile in PROFILES:
            calls = []

            async def upstream(
                req: AgentRequest, model: str, timeout: float
            ) -> AsyncGenerator[dict[str, object], None]:
                calls.append(model)
                self.assertEqual(req.reasoning_effort, "none")
                self.assertEqual(req.messages, original.messages)
                if len(calls) == 1:
                    raise RuntimeError("provider_error")
                yield result("Both pictures contain visible evidence.", 20)

            with (
                patch.dict(os.environ, environment()),
                patch("stream_transport.attempt", upstream),
            ):
                policy = ServerlessPolicy()
                images = payload(picture())["messages"]
                assert isinstance(images, list)
                images[0]["content"].append(copy.deepcopy(images[0]["content"][-1]))
                original = request(profile=profile, messages=images, max_tokens=500)
                events = [e async for e in stream_quality(AgentProvider(), original)]
                self.assertEqual(
                    calls, [policy.models["vision"], policy.models["visionprevious"]]
                )
                self.assertIn("Both pictures", str(events))
                self.assertIn({"phase": "provider_fallback"}, events)
