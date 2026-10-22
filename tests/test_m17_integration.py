"""Regression cases for the public chat integration defects."""

import unittest

from packages.imagegen import image_request


class IntegrationTests(unittest.TestCase):
    def test_natural_french_image_intent(self) -> None:
        self.assertTrue(image_request("crée moi une image d'un chien qui danse"))
        self.assertTrue(image_request("génère-moi une image de chat"))
        self.assertFalse(image_request("décris cette image de chat"))

    def test_auxiliary_settings_override_persisted_configuration(self) -> None:
        from pathlib import Path

        settings = Path("infra/chat-ui.env").read_text()
        for key in (
            "ENABLE_TITLE_GENERATION",
            "ENABLE_TAGS_GENERATION",
            "ENABLE_FOLLOW_UP_GENERATION",
            "ENABLE_AUTOCOMPLETE_GENERATION",
            "ENABLE_PERSISTENT_CONFIG",
        ):
            self.assertIn(key + "=False", settings)


class WebContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_context_retains_complete_source_for_m21(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.cache import Cache
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Call
        from services.orchestrator.tools import Runtime

        with (
            tempfile.TemporaryDirectory() as root,
            patch(
                "services.orchestrator.chat_pipeline.Cache",
                return_value=Cache(Path(root) / "web.sqlite"),
            ),
            patch.object(
                Runtime,
                "execute",
                return_value={
                    "trust": "untrusted",
                    "data": {
                        "url": "https://example.org",
                        "consulted_at": "2026-09-11",
                        "text": "The CERN release was in 1993. " * 500,
                    },
                },
            ),
        ):
            result = await ChatTools(Interaction(), "CERN release").execute(
                Call("c", "web_fetch", '{"url":"https://example.org/history"}'), 5
            )
        data = result["data"]
        assert isinstance(data, dict)
        self.assertEqual(data["text"], "The CERN release was in 1993. " * 500)
        self.assertIn("1993", str(data["text"]))
        self.assertEqual(data["url"], "https://example.org")
        self.assertEqual(data["consulted_at"], "2026-09-11")
        self.assertNotIn("truncated", data)


class ProjectSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_prose_cannot_change_project_scope(self) -> None:
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.project_commands import select

        receipt = "[atlas-project:00000000-0000-0000-0000-000000000001:00000000-0000-0000-0000-000000000002]"
        request = ChatRequest.model_validate(
            {
                "messages": [
                    {"role": "user", "content": "Tell me about this document"},
                    {"role": "assistant", "content": receipt},
                    {"role": "user", "content": "Continue"},
                ]
            }
        )
        self.assertFalse(await select(request, Interaction(), "http://unused.invalid"))
        self.assertIsNone(request.project_id)
        request.messages[0].content = "/project use Alpha"
        self.assertFalse(await select(request, Interaction(), "http://unused.invalid"))
        self.assertEqual(request.project_id, "00000000-0000-0000-0000-000000000001")

    async def test_leave_drops_previous_project_client_history(self) -> None:
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.project_commands import select

        request = ChatRequest.model_validate(
            {
                "messages": [
                    {"role": "user", "content": "ALPHA_CANARY"},
                    {"role": "user", "content": "/project leave"},
                    {
                        "role": "assistant",
                        "content": "Project context cleared. [atlas-project:none]",
                    },
                    {"role": "user", "content": "Bonjour"},
                ]
            }
        )
        self.assertFalse(await select(request, Interaction(), "http://unused.invalid"))
        self.assertIsNone(request.project_id)
        self.assertEqual([m.text for m in request.messages], ["Bonjour"])
