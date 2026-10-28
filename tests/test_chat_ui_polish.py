"""Owner review 2026-10-28: calm activity in the guest conversation language."""

from collections.abc import AsyncGenerator
from typing import cast
import json
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

from infra.openwebui_images import Filter
from packages.profiles import PROFILES
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction


class GuestActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_inlet_uses_conversation_without_initial_timer(self) -> None:
        emitter = AsyncMock()
        with patch.dict(os.environ, {"ATLAS_STABLE": "1"}):
            await Filter().inlet(
                {
                    "model": PROFILES[0],
                    "ui_locale": "en",
                    "messages": [
                        {
                            "role": "user",
                            "content": "Bonjour, explique le cycle de l'eau.",
                        }
                    ],
                },
                __event_emitter__=emitter,
            )
        self.assertEqual(
            emitter.call_args_list[0].args[0]["data"]["description"],
            "R\u00e9flexion\u2026",
        )

    async def test_guest_stream_hides_deadline_and_delays_elapsed_time(self) -> None:
        for elapsed in (0, 9, 25):
            with (
                self.subTest(elapsed=elapsed),
                patch.dict(os.environ, {"ATLAS_STABLE": "1"}),
            ):
                request = ChatRequest.model_validate(
                    {
                        "ui_locale": "en",
                        "messages": [
                            {
                                "role": "user",
                                "content": "Bonjour, explique le cycle de l'eau.",
                            }
                        ],
                    }
                )
                stream = response(
                    request, Interaction(), time.monotonic() - elapsed, AsyncMock()
                )
                iterator = cast(AsyncGenerator[str, None], stream.body_iterator)
                first = await iterator.__anext__()
                data = json.loads(str(first).removeprefix("data: "))["event"]["data"]
                await iterator.aclose()
                self.assertEqual(
                    data["description"],
                    "R\u00e9flexion\u2026" + (" 25 s" if elapsed == 25 else ""),
                )
                self.assertNotIn("deadline_seconds", data)
                self.assertNotIn("/", data["description"])


class GuestActivityLabelTests(unittest.TestCase):
    def test_every_activity_has_a_service_translation(self) -> None:
        from services.orchestrator.guest_activity import (
            ACTIVITIES,
            LABELS,
            activity_label,
        )

        for language in ("fr", "de", "es", "it", "en"):
            self.assertEqual(len(LABELS[language]), len(ACTIVITIES))
            for activity in ACTIVITIES:
                label = activity_label(language, activity)
                self.assertTrue(label)
                self.assertNotIn("_", label)
                if language != "en":
                    self.assertNotEqual(label, activity_label("en", activity))
        self.assertEqual(
            activity_label("fr", "document_read"), "Lecture du document\u2026"
        )
        self.assertEqual(
            activity_label("fr", "internal_tool_identifier"), "R\u00e9flexion\u2026"
        )
        self.assertEqual(
            activity_label("fr", "provider_fallback"), "R\u00e9flexion\u2026"
        )
