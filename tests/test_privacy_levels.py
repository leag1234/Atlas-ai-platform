"""Privacy levels: Standard, Enhanced and Strict, from the edge to every egress.

Enhanced has no pseudonymiser yet, so it must never pretend: content that would leave in
clear is refused by name, and the level is offered only where the operator enables it.
"""

import asyncio
import contextvars
from collections.abc import Callable
import os
from pathlib import Path
import tempfile
from typing import Any, TypeVar
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from packages import privacy
from services.orchestrator import conversation_privacy, dictation
from services.orchestrator.api_keys import Store
from services.orchestrator.chat_api import app
from services.retrieval.project_policy import ProjectCreate, ProjectPatch

T = TypeVar("T")


def at(level: str, call: Callable[[], T]) -> T:
    """Run ``call`` in a fresh request context raised to ``level``."""

    def run() -> T:
        privacy.elevate(level)
        return call()

    return contextvars.copy_context().run(run)


class LevelModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(
            patch.dict(
                os.environ,
                {"ATLAS_PRIVACY_MINIMUM": "", "ATLAS_PRIVACY_ENHANCED": ""},
            )
        )

    def test_parse_and_highest(self) -> None:
        self.assertIsNone(privacy.parse(None))
        self.assertIsNone(privacy.parse(""))
        self.assertEqual(privacy.parse(" Strict "), "strict")
        self.assertEqual(privacy.parse("enhanced"), "enhanced")
        for wrong in ("secret", 3, "STRICTEST"):
            with self.assertRaises(privacy.InvalidPrivacyLevel) as refused:
                privacy.parse(wrong)
            self.assertEqual(refused.exception.code, "invalid_privacy_level")
        self.assertEqual(privacy.highest(), "standard")
        self.assertEqual(privacy.highest("standard", None, "enhanced"), "enhanced")
        self.assertEqual(privacy.highest("strict", "enhanced"), "strict")

    def test_elevate_never_lowers(self) -> None:
        def run() -> list[str]:
            seen: list[str] = [privacy.current()]
            privacy.elevate("strict")
            seen.append(privacy.current())
            privacy.elevate("standard")
            privacy.elevate("enhanced")
            seen.append(privacy.current())
            return seen

        self.assertEqual(
            contextvars.copy_context().run(run), ["standard", "strict", "strict"]
        )

    def test_organisation_minimum_is_always_in_force(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PRIVACY_MINIMUM": "strict"}):
            self.assertEqual(privacy.current(), "strict")
            self.assertEqual(at("standard", privacy.current), "strict")
            self.assertEqual(privacy.headers(), {"X-Atlas-Privacy": "strict"})
        with patch.dict(os.environ, {"ATLAS_PRIVACY_MINIMUM": "nonsense"}):
            # A minimum that cannot be read fails closed.
            self.assertEqual(privacy.minimum(), "strict")
        with patch.dict(os.environ, {"ATLAS_PRIVACY_MINIMUM": "enhanced"}):
            self.assertTrue(privacy.enhanced_offered())

    def test_standard_changes_nothing_on_the_wire(self) -> None:
        # Recorded provider replays stay valid: no header, every egress allowed.
        self.assertEqual(privacy.headers(), {})
        for capability in ("inference", "vision", "ocr", "web_search", "embeddings"):
            self.assertTrue(privacy.allows(capability))
            privacy.require_shared(capability)
        tools = [{"function": {"name": n}} for n in ("web_search", "mcp_x", "calc")]
        self.assertEqual(privacy.allowed_tools(tools), tools)
        self.assertFalse(privacy.owned_only())

    def test_enhanced_without_pseudonymiser_sends_nothing_in_clear(self) -> None:
        self.assertFalse(privacy.pseudonymiser_available())

        def check() -> dict[str, Any]:
            refused = {}
            for capability in (
                "inference",
                "vision",
                "ocr",
                "web_search",
                "web_fetch",
                "embeddings",
                "transcription",
                "places_search",
            ):
                with self.assertRaises(privacy.PrivacyRefusal) as caught:
                    privacy.require_shared(capability)
                refused[capability] = caught.exception
            privacy.require_shared("connectors")
            privacy.require_shared("currency_rates")
            tools = [
                {"function": {"name": n}}
                for n in ("web_search", "web_fetch", "web_images", "mcp_notes")
            ] + [{"function": {"name": "currency_convert"}}, {"name": "calc"}]
            return {
                "refused": refused,
                "tools": [privacy.tool_name(t) for t in privacy.allowed_tools(tools)],
                "owned_only": privacy.owned_only(),
                "headers": privacy.headers(),
            }

        result = at("enhanced", check)
        self.assertTrue(result["owned_only"])
        self.assertEqual(result["headers"], {"X-Atlas-Privacy": "enhanced"})
        self.assertEqual(result["tools"], ["mcp_notes", "currency_convert", "calc"])
        inference = result["refused"]["inference"]
        self.assertEqual(inference.code, "enhanced_privacy_unavailable")
        self.assertEqual(inference.status, 403)
        self.assertIn("pseudonymisation", inference.message)
        for feature in ("vision", "ocr", "web_search", "embeddings", "transcription"):
            refused = result["refused"][feature]
            self.assertEqual(refused.code, "privacy_feature_refused")
            self.assertEqual(refused.body()["feature"], feature)
            self.assertEqual(refused.body()["level"], "enhanced")

    def test_strict_keeps_its_code_with_403(self) -> None:
        def check() -> privacy.PrivacyRefusal:
            with self.assertRaises(privacy.PrivacyRefusal) as caught:
                privacy.require_shared("connectors")
            return caught.exception

        refused = at("strict", check)
        self.assertEqual(refused.code, "strict_privacy_unavailable")
        self.assertEqual(refused.status, 403)
        # The former name still catches the whole family.
        self.assertIs(privacy.StrictPrivacyUnavailable, privacy.PrivacyRefusal)

    def test_internal_level_is_read_from_header_and_body(self) -> None:
        self.assertEqual(privacy.requested({"X-Atlas-Privacy": "enhanced"}), "enhanced")
        self.assertEqual(
            privacy.requested({}, {"privacy": "strict"}),
            "strict",
        )
        self.assertEqual(privacy.requested({"X-Atlas-Privacy": "bogus"}), "standard")


class GatewayEgressTests(unittest.TestCase):
    def test_shared_provider_refused_at_enhanced_and_strict(self) -> None:
        import provider_settings

        def require() -> str:
            try:
                provider_settings.require("scaleway")
            except privacy.PrivacyRefusal as exc:
                return exc.code
            return "sent"

        self.assertEqual(at("standard", require), "sent")
        self.assertEqual(at("enhanced", require), "enhanced_privacy_unavailable")
        self.assertEqual(at("strict", require), "strict_privacy_unavailable")

    def test_vision_needs_an_owned_engine_above_standard(self) -> None:
        import vision

        def owned() -> object:
            try:
                return vision.owned_vision_model()
            except privacy.PrivacyRefusal as exc:
                return exc.code

        self.assertIsNone(at("standard", owned))
        with patch("provider_settings.preset_value", return_value=None):
            self.assertEqual(at("enhanced", owned), "privacy_feature_refused")
            self.assertEqual(at("strict", owned), "strict_privacy_unavailable")


class ApiEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(
            root / "keys.sqlite", "fictional-pepper-" * 4, lambda: 1800000000.0
        )
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_DATA_DIR": str(root),
                    "ATLAS_INTERACTION_DIR": str(root / "chat-log"),
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                    "ATLAS_USER_DAILY_EUR": "10",
                    "ATLAS_GLOBAL_DAILY_EUR": "20",
                    "ATLAS_PRIVACY_MINIMUM": "",
                    "ATLAS_PRIVACY_ENHANCED": "",
                    "ATLAS_OWNED_VLLM_BASE_URL": "",
                },
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=self.store,
            )
        )
        self.calls: list[list[str]] = []

        async def fake_embed(texts: list[str], **options: Any) -> dict[str, Any]:
            self.calls.append(texts)
            return {"vectors": [[1.0] * 4 for _ in texts], "tokens": 1, "cost_micro": 1}

        self.enterContext(patch("embeddings.embed", fake_embed))
        self.client = self.enterContext(TestClient(app))
        _, secret = self.store.create(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="privacy",
            scopes=["model"],
            created_by="operator",
            daily_requests=100,
            daily_micro_eur=1000000,
        )
        self.key = {"Authorization": "Bearer " + secret}

    def chat(self, headers: dict[str, str] | None = None, **body: Any) -> Any:
        return self.client.post(
            "/api/atlas/v1/chat/completions",
            headers=self.key | (headers or {}),
            json={
                "model": "atlas-code",
                "messages": [{"role": "user", "content": "Bonjour"}],
            }
            | body,
        )

    def test_standard_is_served_and_reported(self) -> None:
        reply = self.client.post(
            "/api/atlas/v1/embeddings", headers=self.key, json={"input": "un"}
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.headers["X-Atlas-Privacy-Applied"], "standard")
        self.assertEqual(self.calls, [["un"]])

    def test_body_field_raises_the_level_and_is_never_forwarded(self) -> None:
        reply = self.client.post(
            "/api/atlas/v1/embeddings",
            headers=self.key,
            json={"input": "un", "privacy": "strict"},
        )
        self.assertEqual(reply.status_code, 403, reply.text)
        self.assertEqual(reply.json()["error"]["code"], "strict_privacy_unavailable")
        self.assertEqual(reply.headers["X-Atlas-Privacy-Applied"], "strict")
        self.assertEqual(self.calls, [])
        reply = self.chat(privacy="strict")
        self.assertEqual(reply.status_code, 403, reply.text)
        self.assertEqual(reply.json()["error"]["type"], "strict_privacy_unavailable")

    def test_a_request_cannot_lower_the_key_or_header_level(self) -> None:
        reply = self.client.post(
            "/api/atlas/v1/embeddings",
            headers=self.key | {"X-Atlas-Privacy": "strict"},
            json={"input": "un", "privacy": "standard"},
        )
        self.assertEqual(reply.status_code, 403, reply.text)
        self.assertEqual(reply.headers["X-Atlas-Privacy-Applied"], "strict")

    def test_unknown_level_is_a_400(self) -> None:
        reply = self.chat({"X-Atlas-Privacy": "private"})
        self.assertEqual(reply.status_code, 400, reply.text)
        self.assertEqual(reply.json()["error"]["code"], "invalid_privacy_level")
        reply = self.chat(privacy="private")
        self.assertEqual(reply.status_code, 400, reply.text)

    def test_enhanced_is_refused_until_offered_then_never_sent_in_clear(self) -> None:
        reply = self.chat({"X-Atlas-Privacy": "enhanced"})
        self.assertEqual(reply.status_code, 403, reply.text)
        error = reply.json()["error"]
        self.assertEqual(error["code"], "enhanced_privacy_unavailable")
        self.assertEqual(error["feature"], "pseudonymisation")
        with patch.dict(os.environ, {"ATLAS_PRIVACY_ENHANCED": "1"}):
            reply = self.chat({"X-Atlas-Privacy": "enhanced"})
            self.assertEqual(reply.status_code, 403, reply.text)
            self.assertEqual(
                reply.json()["error"]["type"], "enhanced_privacy_unavailable"
            )
            self.assertEqual(reply.headers["X-Atlas-Privacy-Applied"], "enhanced")
            reply = self.client.post(
                "/api/atlas/v1/embeddings",
                headers=self.key | {"X-Atlas-Privacy": "enhanced"},
                json={"input": "un"},
            )
            self.assertEqual(reply.status_code, 403, reply.text)
            self.assertEqual(reply.json()["error"]["code"], "privacy_feature_refused")
            self.assertEqual(reply.json()["error"]["feature"], "embeddings")
        self.assertEqual(self.calls, [])

    def test_organisation_minimum_applies_to_every_key(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PRIVACY_MINIMUM": "strict"}):
            reply = self.client.post(
                "/api/atlas/v1/embeddings", headers=self.key, json={"input": "un"}
            )
        self.assertEqual(reply.status_code, 403, reply.text)
        self.assertEqual(reply.headers["X-Atlas-Privacy-Applied"], "strict")
        self.assertEqual(self.calls, [])


class ConversationLevelTests(unittest.TestCase):
    user = "fictional-user-1"

    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_CONVERSATION_PRIVACY_DB": str(root / "levels.sqlite"),
                    "ATLAS_PRIVACY_MINIMUM": "",
                    "ATLAS_PRIVACY_ENHANCED": "",
                    "ATLAS_OWNED_VLLM_BASE_URL": "http://127.0.0.1:9/v1",
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                },
            )
        )

        async def signed_in(request: Any) -> str:
            return self.user

        self.enterContext(patch.object(conversation_privacy, "browser_user", signed_in))
        self.client = self.enterContext(TestClient(app))

    def choose(self, level: str, chat: str = "") -> Any:
        return self.client.put(
            "/api/atlas/privacy",
            json={"chat": chat, "level": level},
            headers={"Origin": "http://testserver"},
        )

    def test_new_conversation_takes_the_pending_choice(self) -> None:
        self.assertEqual(self.choose("strict").status_code, 200)
        self.assertEqual(conversation_privacy.bind(self.user, "chat-a"), "strict")
        # The pending choice was consumed: the next new conversation is Standard.
        self.assertEqual(conversation_privacy.bind(self.user, "chat-b"), "standard")
        self.assertEqual(conversation_privacy.bind(self.user, "chat-a"), "strict")
        self.assertEqual(
            conversation_privacy.bind("someone-else", "chat-a"), "standard"
        )

    def test_a_started_conversation_is_never_lowered(self) -> None:
        conversation_privacy.bind(self.user, "chat-c")
        self.assertEqual(self.choose("strict", "chat-c").status_code, 200)
        reply = self.choose("standard", "chat-c")
        self.assertEqual(reply.status_code, 409, reply.text)
        self.assertEqual(reply.json()["error"], "privacy_level_locked")
        state = self.client.get("/api/atlas/privacy", params={"chat": "chat-c"}).json()
        self.assertEqual(state["level"], "strict")
        self.assertTrue(state["locked"])
        self.assertEqual(
            {row["id"]: row["reason"] for row in state["levels"]},
            {"standard": "conversation_started", "strict": None},
        )
        # Before its first message, a conversation's choice can still go down.
        self.assertEqual(self.choose("strict", "chat-d").status_code, 200)
        self.assertEqual(self.choose("standard", "chat-d").status_code, 200)

    def test_unavailable_and_hidden_levels(self) -> None:
        state = self.client.get("/api/atlas/privacy").json()
        self.assertEqual([row["id"] for row in state["levels"]], ["standard", "strict"])
        reply = self.choose("enhanced")
        self.assertEqual(reply.status_code, 403, reply.text)
        self.assertEqual(reply.json()["error"], "enhanced_privacy_unavailable")
        self.assertEqual(self.choose("private").status_code, 400)
        with patch.dict(
            os.environ,
            {"ATLAS_PRIVACY_ENHANCED": "1", "ATLAS_OWNED_VLLM_BASE_URL": ""},
        ):
            levels = {
                row["id"]: row["reason"]
                for row in self.client.get("/api/atlas/privacy").json()["levels"]
            }
            self.assertEqual(
                levels,
                {
                    "standard": None,
                    "enhanced": "no_pseudonymiser",
                    "strict": "no_owned_server",
                },
            )
            self.assertEqual(self.choose("strict").status_code, 403)
        with patch.dict(os.environ, {"ATLAS_PRIVACY_MINIMUM": "strict"}):
            state = self.client.get("/api/atlas/privacy").json()
            self.assertEqual(state["level"], "strict")
            self.assertEqual(state["levels"][0]["reason"], "organisation_minimum")
            self.assertEqual(self.choose("standard").status_code, 403)

    def test_the_store_holds_no_content(self) -> None:
        conversation_privacy.bind(self.user, "chat-e")
        with conversation_privacy.connection() as db:
            columns = [
                row[1] for row in db.execute("PRAGMA table_info(conversation_privacy)")
            ]
        self.assertEqual(columns, ["user", "chat", "level", "used", "updated"])


class EnhancedFeatureTests(unittest.TestCase):
    def test_dictation_stays_local_at_enhanced(self) -> None:
        limits = dictation.Limits.current()
        with (
            patch.dict(os.environ, {"ATLAS_DICTATION_ENGINES": "local,cloud"}),
            patch.object(dictation, "local_installed", lambda: True),
        ):
            with self.assertRaises(dictation.Refusal) as refused:
                dictation.choose_engine("cloud", "enhanced", limits)
            self.assertEqual(refused.exception.body["code"], "privacy_feature_refused")
            self.assertEqual(refused.exception.body["feature"], "dictation_cloud")
            # A language the local engine lacks does not go to the cloud engine either.
            self.assertEqual(
                dictation.choose_engine("auto", "enhanced", limits, "ja"),
                ("local", False),
            )

    def test_dictation_follows_the_conversation_level(self) -> None:
        user = dictation.SocketUser("fictional-user-2", "")
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {"ATLAS_CONVERSATION_PRIVACY_DB": str(Path(root) / "levels.sqlite")},
            ),
        ):
            conversation_privacy.bind(user.subject, "chat-f")
            with conversation_privacy.connection() as db:
                db.execute(
                    "UPDATE conversation_privacy SET level='strict' WHERE chat='chat-f'"
                )
            start = {"type": "start", "chat": "chat-f", "privacy": "standard"}
            self.assertEqual(
                asyncio.run(dictation.session_privacy(start, user)), "strict"
            )
            self.assertEqual(
                asyncio.run(dictation.session_privacy({"type": "start"}, user)),
                "standard",
            )

    def test_projects_accept_enhanced_only_where_offered(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PRIVACY_ENHANCED": ""}):
            with self.assertRaises(ValidationError):
                ProjectPatch.model_validate({"privacy": "enhanced"})
            self.assertEqual(
                ProjectCreate.model_validate(
                    {"name": "P", "privacy": "strict"}
                ).privacy,
                "strict",
            )
        with patch.dict(os.environ, {"ATLAS_PRIVACY_ENHANCED": "1"}):
            self.assertEqual(
                ProjectPatch.model_validate({"privacy": "enhanced"}).privacy,
                "enhanced",
            )


if __name__ == "__main__":
    unittest.main()
