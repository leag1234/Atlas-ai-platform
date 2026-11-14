"""Strict privacy: no request content reaches a shared provider or a third party.

The level is one context variable per request (packages.privacy). These tests check the
single gateway enforcement point (provider credentials), each third-party egress outside
the gateway, the boundary between the orchestrator and the gateway, and whole turns of a
strict project. Every network primitive is replaced by a trap: reaching one fails the test.
"""

import ast
import asyncio
import base64
import json
import os
import re
import tempfile
import threading
import unittest
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import aiohttp

from packages import privacy
from serverless_support import environment

# Modules that open an outbound HTTP connection, by destination. A new egress must be
# classified here or in INTERNAL. "internal": Atlas services only (gateway, retrieval,
# runner, terminal, image worker); "provider": endpoint and credential come from
# provider_settings (the gateway's strict check); "third_party": calls require_shared
# (or is_strict) before sending. Local MCP server processes (mcp_transport) are checked
# by the behaviour test below.
EGRESS: dict[str, str] = {
    "services/model-gateway/agent_provider.py": "provider",
    "services/model-gateway/dev_gateway.py": "provider",
    "services/model-gateway/embeddings.py": "provider",
    "services/model-gateway/eval_provider.py": "provider",
    "services/model-gateway/generation.py": "provider",
    "services/model-gateway/image_lifecycle.py": "internal",
    "services/model-gateway/imagegen.py": "internal",
    "services/model-gateway/realtime_transcription.py": "provider",
    "services/model-gateway/stream_transport.py": "provider",
    "services/model-gateway/transcription.py": "provider",
    "services/model-gateway/vision.py": "provider",
    "services/orchestrator/attachment_reading.py": "third_party",
    "services/orchestrator/commons.py": "third_party",
    "services/orchestrator/fx_rates.py": "third_party",
    "services/orchestrator/mcp_http.py": "third_party",
    "services/orchestrator/nearby.py": "third_party",
    "services/orchestrator/searxng.py": "third_party",
    "services/orchestrator/tools.py": "third_party",
    "services/orchestrator/visual_tools.py": "third_party",
    "services/orchestrator/web.py": "third_party",
}
# Modules that reach only Atlas services, or send nothing from the request (health,
# skill catalog, connector OAuth discovery through retrieval, forge publication the
# user asked for); the inventory test fails on any module missing from both lists.
INTERNAL = {
    "services/orchestrator/api_key_admin.py",
    "services/orchestrator/api_key_edge.py",
    "services/orchestrator/api_key_ui.py",
    "services/orchestrator/app_turn.py",
    "services/orchestrator/artifact_routes.py",
    "services/orchestrator/chat_pipeline.py",
    "services/orchestrator/chat_work.py",
    "services/orchestrator/code_api.py",
    "services/orchestrator/connectors.py",
    "services/orchestrator/dev_health.py",
    "services/orchestrator/dictation.py",
    "services/orchestrator/dictation_cleanup.py",
    "services/orchestrator/document_chat.py",
    "services/orchestrator/image_errors.py",
    "services/orchestrator/key_subject.py",
    "services/orchestrator/model.py",
    "services/orchestrator/project_commands.py",
    "services/orchestrator/project_forge.py",
    "services/orchestrator/project_scheduler.py",
    "services/orchestrator/project_transcript.py",
    "services/orchestrator/project_ui.py",
    # The CPU picture server is an Atlas-operated dedicated server (infra/pictures-cpu,
    # TLS and token): it receives the rewritten picture prompt only.
    "services/orchestrator/pictures/cpu_server.py",
    "services/orchestrator/serving.py",
    "services/orchestrator/skill_catalog.py",
    "services/orchestrator/stream_client.py",
    "services/orchestrator/terminal_client.py",
    "services/orchestrator/terminal_stack.py",
    "services/orchestrator/user_memory.py",
    "services/orchestrator/user_memory_ui.py",
    "services/orchestrator/voice_ui.py",
    "services/orchestrator/webui.py",
    "services/orchestrator/work_opencode.py",
    "services/orchestrator/work_opencode_container.py",
    "services/retrieval/connector_api.py",
    "services/retrieval/connector_oauth.py",
    "services/retrieval/search.py",
    "services/retrieval/skills_cli.py",
    "services/runner/channel.py",
}


def outbound_modules() -> set[str]:
    found: set[str] = set()
    for path in Path("services").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = (
                target.attr
                if isinstance(target, ast.Attribute)
                else target.id
                if isinstance(target, ast.Name)
                else ""
            )
            owner = (
                target.value.id
                if isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                else ""
            )
            if name in {"ClientSession", "urlopen", "build_opener"} or (
                owner == "httpx" and name in {"Client", "AsyncClient"}
            ):
                found.add(path.as_posix())
                break
    return found


@contextmanager
def strict() -> Iterator[None]:
    token = privacy.elevate(privacy.STRICT)
    try:
        yield
    finally:
        privacy.reset(token)


class Trap:
    """Stands for a network client: any use fails the test."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        raise AssertionError("outbound connection attempted in strict privacy")


def network(trap: Trap) -> Mock:
    """A module's aiohttp with its client replaced by the trap (that module only)."""
    return Mock(
        ClientSession=trap,
        ClientTimeout=aiohttp.ClientTimeout,
        ClientError=aiohttp.ClientError,
    )


@contextmanager
def no_network() -> Iterator[Trap]:
    trap = Trap()
    with (
        patch.object(aiohttp, "ClientSession", trap),
        patch("urllib.request.urlopen", trap),
    ):
        yield trap


SHARED_ENV = environment() | {
    "SCW_GENERATIVE_BASE_URL": "https://shared.example.invalid/v1",
    "SCW_GENERATIVE_API_KEY": "shared-test-only",
    "ATLAS_OCR_MODEL": "ocr-test-only",
    "SERPAPI_KEY": "search-test-only",
    "TAVILY_API_KEY": "search-test-only",
}


class EgressInventoryTests(unittest.TestCase):
    def test_every_outbound_module_is_classified(self) -> None:
        found = outbound_modules()
        unknown = found - set(EGRESS) - INTERNAL
        self.assertEqual(
            unknown, set(), "classify this egress for strict privacy (EGRESS)"
        )
        self.assertEqual(set(EGRESS) - found, set(), "stale EGRESS entry")

    def test_shared_egress_checks_the_level_before_sending(self) -> None:
        for module, kind in EGRESS.items():
            text = Path(module).read_text()
            with self.subTest(module=module):
                if kind == "provider":
                    # Credentials come from provider_settings (checked there); a module
                    # reading a key itself must call the check explicitly.
                    self.assertIn("provider_settings.", text)
                    if re.search(r"environ\[\"[A-Z_]*API_KEY\"\]", text):
                        self.assertIn("provider_settings.require(", text)
                elif kind == "third_party":
                    self.assertRegex(text, r"require_shared\(|is_strict\(")


class GatewayEnforcementTests(unittest.TestCase):
    def test_shared_credentials_are_never_handed_out_in_strict(self) -> None:
        import provider_settings

        with patch.dict(os.environ, SHARED_ENV, clear=False):
            os.environ.pop("ATLAS_PROVIDER", None)
            self.assertEqual(provider_settings.api_key(), "shared-test-only")
            with strict():
                for call in (
                    provider_settings.endpoint,
                    provider_settings.api_key,
                    lambda: provider_settings.api_key_for("mistral"),
                ):
                    with self.assertRaises(privacy.StrictPrivacyUnavailable) as raised:
                        call()
                    self.assertEqual(str(raised.exception), privacy.CODE)
                with patch.dict(
                    os.environ, {"ATLAS_OWNED_VLLM_API_KEY": "owned-test-only"}
                ):
                    self.assertEqual(
                        provider_settings.owned_api_key(), "owned-test-only"
                    )

    def test_only_presets_flagged_owned_pass(self) -> None:
        import provider_settings

        owned = [
            name
            for name in provider_settings.presets()
            if provider_settings.owned(name)
        ]
        self.assertEqual(owned, ["owned-vllm"])

    def test_agent_requests_inherit_the_level(self) -> None:
        from agent_provider import AgentRequest

        body = {
            "messages": [{"role": "user", "content": "Bonjour"}],
            "tools": [],
            "timeout": 5,
        }
        self.assertEqual(AgentRequest.model_validate(body).privacy, "standard")
        with strict():
            self.assertEqual(AgentRequest.model_validate(body).privacy, "strict")


class GatewayServer:
    def __init__(self) -> None:
        from gateway_cpu import CPUModels
        from http_gateway import serve

        backend = Mock(spec=CPUModels)
        backend.embed.return_value = [[0.0, 1.0]]
        self.backend = backend
        self.server = serve(backend, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def post(
        self, path: str, payload: dict[str, object], level: str = "strict"
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        request = Request(
            self.url + path,
            data=json.dumps(payload).encode(),
            headers={privacy.HEADER: level},
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), json.load(response)
        except HTTPError as error:
            with error:
                return error.code, dict(error.headers), json.load(error)


def picture() -> str:
    from test_vision_schema import picture as image

    return image()


class GatewayBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, SHARED_ENV)
        env.start()
        self.addCleanup(env.stop)
        for name in ("ATLAS_PROVIDER", "ATLAS_OWNED_VLLM_BASE_URL"):
            os.environ.pop(name, None)
        self.gateway = GatewayServer()
        self.addCleanup(self.gateway.close)

    def vision_payload(self) -> dict[str, object]:
        return {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Que montre cette image ?"},
                        {"type": "image_url", "image_url": {"url": picture()}},
                    ],
                }
            ]
        }

    def test_vision_on_an_attached_image_is_refused_by_name(self) -> None:
        trap = Trap()
        with patch("vision.aiohttp", network(trap)):
            status, headers, body = self.gateway.post(
                "/vision/complete", self.vision_payload()
            )
        self.assertEqual(status, privacy.STATUS)
        self.assertEqual(body["code"], privacy.CODE)
        self.assertEqual(headers.get(privacy.REFUSAL_HEADER), "vision")
        self.assertEqual(trap.calls, [])

    def test_strict_chat_without_owned_server_is_refused_by_name(self) -> None:
        trap = Trap()
        with patch("agent_provider.aiohttp", network(trap)):
            status, headers, body = self.gateway.post(
                "/agent/complete",
                {
                    "messages": [{"role": "user", "content": "Bonjour"}],
                    "tools": [],
                    "timeout": 5,
                },
            )
        self.assertEqual((status, body["code"]), (privacy.STATUS, privacy.CODE))
        self.assertEqual(headers.get(privacy.REFUSAL_HEADER), "inference")
        self.assertEqual(trap.calls, [])

    def test_project_file_embeddings_stay_on_the_local_models(self) -> None:
        status, _, body = self.gateway.post(
            "/embeddings", {"texts": ["Budget fictif 2026"], "kind": "document"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["vectors"], [[0.0, 1.0]])
        self.gateway.backend.embed.assert_called_once()

    def test_retrieval_client_carries_the_level(self) -> None:
        from services.retrieval.search import Gateway

        seen: list[str | None] = []

        def fake(request: Request, timeout: float) -> object:
            seen.append(request.get_header(privacy.HEADER.capitalize()))
            raise OSError("stop")

        with patch("services.retrieval.search.urlopen", fake), strict():
            with self.assertRaises(Exception):
                Gateway(self.gateway.url).post("embeddings", {"texts": [], "kind": "x"})
        self.assertEqual(seen, ["strict"])

    async def test_orchestrator_client_names_the_gateway_refusal(self) -> None:
        from services.orchestrator.loop import GatewayError, StrictPrivacyRefusal
        from services.orchestrator.model import GatewayModel

        trap = Trap()
        with patch("vision.aiohttp", network(trap)), strict():
            with self.assertRaises(StrictPrivacyRefusal) as raised:
                await GatewayModel.post(
                    self.gateway.url + "/vision/complete", self.vision_payload(), 10
                )
        self.assertIsInstance(raised.exception, GatewayError)
        self.assertEqual(raised.exception.code, privacy.CODE)
        self.assertEqual(raised.exception.status, privacy.STATUS)
        self.assertIn("vision", raised.exception.detail)
        self.assertEqual(trap.calls, [])


class FeatureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, SHARED_ENV)
        env.start()
        self.addCleanup(env.stop)
        for name in ("ATLAS_PROVIDER", "ATLAS_OWNED_VLLM_BASE_URL"):
            os.environ.pop(name, None)

    async def test_image_prompt_rewrite_goes_to_the_owned_endpoint_only(self) -> None:
        import image_prompt

        with no_network() as trap, strict():
            with self.assertRaises(privacy.StrictPrivacyUnavailable):
                await image_prompt.rewrite("Un phare au crépuscule", 10, "fr")
        self.assertEqual(trap.calls, [])
        sent: list[str] = []

        async def owned(
            self: object,
            request: object,
            endpoint: str,
            model: str,
            key: str,
            timeout: float,
        ) -> dict[str, object]:
            sent.append(endpoint)
            raise RuntimeError("stop")

        with (
            patch.dict(
                os.environ,
                {"ATLAS_OWNED_VLLM_BASE_URL": "https://owned.example.invalid/v1"},
            ),
            patch("agent_provider.AgentProvider._complete", owned),
            strict(),
        ):
            try:
                await image_prompt.rewrite("Un phare au crépuscule", 10, "fr")
            except Exception:  # noqa: BLE001 - the stop above; only the target counts
                pass
        self.assertTrue(sent)
        self.assertEqual(set(sent), {"https://owned.example.invalid/v1"})

    async def test_generated_image_check_never_reaches_the_shared_vision(self) -> None:
        from image_scene import anchored_scene
        from image_verify import verify

        trap = Trap()
        with patch("vision.aiohttp", network(trap)), strict():
            result = await verify(picture(), anchored_scene("Un phare"), 5)
        self.assertTrue(result.unknown)
        self.assertEqual(trap.calls, [])

    async def test_scanned_pdf_pages_are_not_sent_to_ocr(self) -> None:
        from test_attachment_ocr import scanned_pdf

        from services.orchestrator.attachment_reading import (
            extract_attachment_with_ocr,
        )
        from services.orchestrator.documents import Attachment

        attachment = Attachment(
            filename="scan.pdf", file_data=base64.b64encode(scanned_pdf(2)).decode()
        )
        with no_network() as trap, strict():
            document, cost, pages = await extract_attachment_with_ocr(
                attachment,
                deadline=asyncio.get_running_loop().time() + 10,
                remaining_cost=Decimal("0.30"),
            )
        self.assertEqual((cost, pages), (Decimal(0), []))
        self.assertEqual(trap.calls, [])
        self.assertTrue(any(privacy.CODE in error for error in document.errors))

    async def test_connectors_are_neither_offered_nor_called(self) -> None:
        from services.orchestrator import connectors, mcp_client
        from services.orchestrator.chat_schema import ChatMessage, ChatRequest

        request = ChatRequest(
            messages=[ChatMessage(role="user", content="Lis mes tickets")]
        )
        request._connector_scope = {"subject": "s", "org": "o"}
        with no_network() as trap, strict():
            self.assertIsNone(await connectors.prepare(request))
            for operation in ("discover", "call"):
                with self.assertRaises(privacy.StrictPrivacyUnavailable):
                    await connectors.Client("s", "o").call(operation, {})
            result = await mcp_client.execute('{"server": "x", "tool": "y"}', 5)
        self.assertEqual(result["error"], privacy.CODE)
        self.assertEqual(trap.calls, [])

    async def test_every_third_party_client_refuses_before_sending(self) -> None:
        from services.orchestrator import (
            commons,
            mcp_http,
            mcp_transport,
            nearby,
            searxng,
            tools,
            visual_tools,
            web,
        )

        calls: dict[str, Callable[[], Awaitable[object]]] = {
            "web_search": lambda: tools.Runtime.search(
                Mock(), tools.Search(query="météo Lyon", lang="fr"), 5
            ),
            "web_fetch": lambda: web.Web._request(Mock(), "https://example.org", 5),
            "searxng": lambda: searxng.engine_search(
                Mock(), "http://127.0.0.1:1", "e", {}, Mock(), 1.0
            ),
            "image_search": lambda: visual_tools.search_images("phare", "serpapi", 5),
            "commons": lambda: commons.find_drawing("phare", lambda url: None),
            "places": lambda: nearby.OSM.request(Mock(), "geocode", {"q": "Lyon"}),
            "mcp_http": lambda: mcp_http.HTTP.exchange(Mock(), "tools/list", {}),
            "mcp_local": lambda: mcp_transport.invoke(
                "/bin/false", [], {}, [], "tool", {}, 1
            ),
        }
        with no_network() as trap, strict():
            for name, call in calls.items():
                with self.subTest(egress=name):
                    with self.assertRaises(privacy.StrictPrivacyUnavailable):
                        await call()
        self.assertEqual(trap.calls, [])

    def test_strict_turns_never_offer_tools_that_leave_atlas(self) -> None:
        from services.orchestrator.model import Configuration, GatewayModel

        model = GatewayModel(
            "http://gateway",
            Configuration(
                input_eur_per_mtok=Decimal("0.5"),
                output_eur_per_mtok=Decimal("1"),
                max_tokens=2048,
            ),
        )
        model.tools.append(
            {"type": "function", "function": {"name": "mcp_ticket_list"}}
        )
        names = {privacy.tool_name(t) for t in model.available_tools([])}
        self.assertTrue({"web_search", "mcp_ticket_list"} <= names)
        with strict():
            names = {privacy.tool_name(t) for t in model.available_tools([])}
            self.assertTrue(model.strict)
        self.assertFalse(names & (privacy.STRICT_EXCLUDED_TOOLS | {"mcp_ticket_list"}))
        self.assertIn("calculator", names)

    def test_strict_work_turns_keep_atlas_loop(self) -> None:
        from services.orchestrator.work_opencode_turn import engine_turn_selected

        selection = Mock(group="code")
        with patch.dict(os.environ, {"ATLAS_WORK_ENGINE": "opencode"}):
            self.assertTrue(engine_turn_selected(selection))
            with strict():
                self.assertFalse(engine_turn_selected(selection))


class StrictProjectTurnTests(unittest.IsolatedAsyncioTestCase):
    """A strict project's chat turn with an image keeps the level end to end."""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from services.retrieval.api import app

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(
            os.environ,
            {
                **SHARED_ENV,
                "ATLAS_PROJECT_DB": str(Path(self.directory.name) / "projects.sqlite"),
                "ATLAS_WEB_CACHE": str(Path(self.directory.name) / "web.sqlite"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)
        self.calls: list[tuple[str, str]] = []

    async def post(
        self, url: str, payload: dict[str, object], timeout: float
    ) -> dict[str, object]:
        from services.orchestrator.loop import StrictPrivacyRefusal

        if "/projects/" in url:
            response = self.client.post(
                "/projects/" + url.split("/projects/", 1)[1], json=payload
            )
            value = response.json()
            assert isinstance(value, dict)
            return value
        # Every other call goes to the model gateway: record the level it would carry
        # (GatewayModel.post sends it as a header) and answer as a strict gateway does.
        self.calls.append((url.split("//", 1)[-1].split("/", 1)[-1], privacy.current()))
        if privacy.is_strict():
            raise StrictPrivacyRefusal("vision" if "vision" in url else "inference")
        raise AssertionError("standard call in a strict project")

    async def test_image_in_a_strict_project_is_refused_not_sent(self) -> None:
        from services.orchestrator.chat_pipeline import process
        from services.orchestrator.chat_schema import ChatMessage, ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.model import GatewayModel

        project = self.client.post(
            "/projects", json={"name": "Privé", "privacy": "strict"}
        ).json()["id"]
        conversation = self.client.post(
            f"/projects/{project}/conversations", json={"name": "Conversation"}
        ).json()["id"]
        request = ChatRequest(
            project_id=project,
            conversation_id=conversation,
            messages=[
                ChatMessage.model_validate(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Que montre cette photo ?"},
                            {"type": "image_url", "image_url": {"url": picture()}},
                        ],
                    }
                )
            ],
        )
        item = Interaction()
        with (
            no_network() as trap,
            patch.object(GatewayModel, "post", side_effect=self.post),
        ):
            # Its own task, like a request: the level ends with it.
            try:
                await asyncio.create_task(process(request, item))
            except privacy.StrictPrivacyUnavailable:
                pass
        self.assertEqual(trap.calls, [])
        self.assertIn(("vision/complete", "strict"), self.calls)
        self.assertTrue(all(level == "strict" for _, level in self.calls), self.calls)
        self.assertFalse(privacy.is_strict())


if __name__ == "__main__":
    unittest.main()
