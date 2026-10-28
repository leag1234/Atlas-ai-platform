"""Stable-only integration boundary regressions; no external services required."""

import os
from pathlib import Path
import unittest
import tempfile
from unittest.mock import AsyncMock, patch

from services.orchestrator.serving import stable_webui_options
from services.orchestrator.terminal_stack import stable_route_allowed
from services.orchestrator.ui_injection import inject_atlas_ui
from services.orchestrator.web import Web
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction


class StableIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_image_requests_are_localized_and_never_reach_the_model(
        self,
    ) -> None:
        cases = {
            "fr": "Génère une image d'un chat",
            "en": "Generate an image of a cat",
            "de": "Erzeuge ein Bild von einer Katze",
            "es": "Genera una imagen de un gato",
            "it": "Genera un'immagine di un gatto",
        }
        for language, question in cases.items():
            with self.subTest(language=language):
                request = ChatRequest.model_validate(
                    {
                        "lang": language,
                        "ui_locale": language,
                        "messages": [{"role": "user", "content": question}],
                    }
                )
                item = Interaction()
                with (
                    patch.dict(os.environ, {"ATLAS_STABLE": "1"}, clear=False),
                    patch(
                        "services.orchestrator.project_commands.select",
                        new=AsyncMock(return_value=False),
                    ),
                    patch(
                        "services.orchestrator.chat_pipeline.GatewayModel.connect",
                        new=AsyncMock(),
                    ) as connect,
                ):
                    await process(request, item)
                self.assertEqual(item.state, "done")
                self.assertIn("diagram", item.reponse.lower())
                self.assertNotIn("SCW_", item.reponse)
                connect.assert_not_awaited()

    async def test_private_fetches_fail_before_network_io(self) -> None:
        with patch.dict(os.environ, {"ATLAS_STABLE": "1"}):
            for host in (
                "127.0.0.1",
                "169.254.169.254",
                "10.1.2.3",
                "open-terminal",
                "atlas-chat-ui-1900",
                "atlas-chat-1900-db",
                "host.docker.internal",
            ):
                with (
                    self.subTest(host=host),
                    self.assertRaisesRegex(ValueError, "ssrf_denied"),
                ):
                    await Web().fetch("http://" + host + "/")

    def test_every_proxy_location_overwrites_credentials_and_role(self) -> None:
        for name in ("atlas.test.conf", "atlas.indidea.org.conf"):
            text = Path("infra/stable/nginx", name).read_text()
            blocks = [
                block for block in text.split("location ") if "proxy_pass " in block
            ]
            # The production front door has one more proxy location: the image route.
            # Both have the Stop cancel route.
            self.assertEqual(len(blocks), 6 if name == "atlas.indidea.org.conf" else 4)
            prefix = "atlas" if name == "atlas.indidea.org.conf" else "atlas_test"
            for block in blocks:
                if block.startswith("^~ /api/atlas/v1/"):
                    self.assertIn("auth_basic off;", block)
                    self.assertIn(
                        'if ($http_authorization !~ "^Bearer atl_[A-Za-z0-9_-]+$") { return 401; }',
                        block,
                    )
                    self.assertIn(
                        "proxy_set_header Authorization $http_authorization;", block
                    )
                    for header in (
                        "X-Atlas-User",
                        "X-Atlas-User-Role",
                        "X-Atlas-Proxy-Secret",
                        "X-OpenWebUI-User-Id",
                        "X-OpenWebUI-User-Email",
                        "X-Atlas-Project-Identity",
                        "X-Atlas-Identity-Secret",
                    ):
                        self.assertIn(f'proxy_set_header {header} "";', block)
                    self.assertNotIn(
                        "include /etc/nginx/atlas-proxy-secret.conf;", block
                    )
                    continue
                self.assertIn("include /etc/nginx/atlas-proxy-secret.conf;", block)
                self.assertIn('proxy_set_header X-Atlas-User "$remote_user@', block)
                self.assertIn("proxy_set_header X-Atlas-User-Role user;", block)
                if block.startswith(
                    "~ ^/api/(chat/completions|v1/chat/completions|models)$"
                ):
                    # Only an Open WebUI key (Bearer sk-) passes; basic credentials never do.
                    self.assertIn(f"auth_basic ${prefix}_key_realm;", block)
                    self.assertIn(
                        f"proxy_set_header Authorization ${prefix}_upstream_authorization;",
                        block,
                    )
                else:
                    self.assertIn('proxy_set_header Authorization "";', block)
            authorization_map = text.split(
                f"map $http_authorization ${prefix}_upstream_authorization {{", 1
            )[1].split("}", 1)[0]
            self.assertEqual(
                [line.strip() for line in authorization_map.strip().splitlines()],
                ['"~^Bearer (sk-[A-Za-z0-9_-]+)$" "Bearer $1";', 'default "";'],
            )
            self.assertIn('"~^Bearer (sk-[A-Za-z0-9_-]+)$" "Bearer $1";', text)
            self.assertIn('"~^Bearer sk-" off;', text)

    def test_stable_cookie_bridge_and_code_execution_disabled(self) -> None:
        shell = b"<html><head></head></html>"
        self.assertNotIn(b"stable-auth.js", inject_atlas_ui(shell))
        self.assertIn(b"stable-auth.js", inject_atlas_ui(shell, stable=True))
        with patch.dict(
            os.environ,
            {"ATLAS_STABLE": "1", "ATLAS_TRUSTED_EMAIL_HEADER": "X-Atlas-User"},
        ):
            options = stable_webui_options()
        self.assertIn("ENABLE_CODE_EXECUTION=False", options)
        self.assertIn("ENABLE_CODE_INTERPRETER=False", options)
        self.assertIn("ENABLE_INITIAL_ADMIN_SIGNUP=False", options)

    def test_instance_notice_is_absent_by_default_and_escaped_when_set(self) -> None:
        shell = b"<html><head></head></html>"
        with patch.dict(os.environ, {}, clear=True):
            injected = inject_atlas_ui(shell, stable=True)
        self.assertNotIn(b"data-instance-notice", injected)

        notice = 'Use <trusted> & "quoted" text'
        with patch.dict(os.environ, {"ATLAS_INSTANCE_NOTICE": notice}, clear=True):
            injected = inject_atlas_ui(shell, stable=True)
        self.assertIn(
            b'data-instance-notice="Use &lt;trusted&gt; &amp; &quot;quoted&quot; text"',
            injected,
        )
        self.assertNotIn(notice.encode(), injected)

    def test_instance_notice_rejects_values_over_200_characters(self) -> None:
        shell = b"<html><head></head></html>"
        with patch.dict(os.environ, {"ATLAS_INSTANCE_NOTICE": "x" * 201}, clear=True):
            injected = inject_atlas_ui(shell, stable=True)
        self.assertNotIn(b"data-instance-notice", injected)

    def test_instance_notice_accepts_localized_texts(self) -> None:
        shell = b"<html><head></head></html>"
        notice = '{"en": "Recorded.", "fr": "Enregistr\u00e9es."}'
        with patch.dict(os.environ, {"ATLAS_INSTANCE_NOTICE": notice}, clear=True):
            injected = inject_atlas_ui(shell, stable=True)
        self.assertIn(
            b'data-instance-notice="{&quot;en&quot;:&quot;Recorded.&quot;', injected
        )
        for invalid in (
            '{"en": ""}',
            '{"en": "' + "x" * 201 + '"}',
            "{not json",
            "{}",
            '{"english": "x"}',
        ):
            with patch.dict(os.environ, {"ATLAS_INSTANCE_NOTICE": invalid}, clear=True):
                self.assertNotIn(
                    b"data-instance-notice", inject_atlas_ui(shell, stable=True)
                )

    def test_stable_auth_picks_the_notice_language(self) -> None:
        script = Path("services/orchestrator/stable-auth.js").read_text()
        self.assertIn("JSON.parse(raw)", script)
        self.assertIn("texts[language] || texts.en", script)

    def test_stable_auth_keeps_cookie_fetch_bridge(self) -> None:
        script = Path("services/orchestrator/stable-auth.js").read_text()
        self.assertNotIn("Conversations are recorded to improve Atlas.", script)
        self.assertIn("headers.delete('Authorization')", script)
        self.assertIn("credentials: 'same-origin'", script)

    def test_stable_terminal_only_serves_generated_downloads(self) -> None:
        download = (
            "/api/v1/terminals/atlas-files/files/serve/home/user/requests/chatcmpl-"
            + "a" * 32
            + "/presentation.pptx"
        )
        self.assertTrue(
            stable_route_allowed(("GET " + download + " HTTP/1.1").encode())
        )
        for method, path in (
            ("POST", "/api/v1/terminals/atlas-files/execute"),
            ("GET", "/api/v1/terminals/atlas-files/files/list"),
            ("GET", "/api/v1/terminals/atlas-files/api/terminals/x"),
            ("POST", download),
            ("GET", download + "/../../.request"),
            ("POST", "/api/v1/auths/%73ignup"),
            ("POST", "/api/v1/auths/%2573ignup"),
        ):
            with self.subTest(path=path):
                self.assertFalse(
                    stable_route_allowed((method + " " + path + " HTTP/1.1").encode())
                )

    async def test_unsolicited_model_code_is_denied_but_trusted_rendering_works(
        self,
    ) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.loop import Call
        from services.orchestrator.terminal_client import TerminalClient

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_STABLE": "1",
                    "ATLAS_TERMINAL_ENABLED": "1",
                    "ATLAS_WEB_CACHE": root + "/cache.sqlite",
                },
            ),
            patch.object(TerminalClient, "execute", new_callable=AsyncMock) as execute,
        ):
            execute.return_value = {"exit_code": 0}
            tools = ChatTools(Interaction())
            command = Call("x", "terminal_command", '{"command":"true"}')
            publication = Call(
                "y", "publish_document", '{"path":"result.pptx","strategy":"created"}'
            )
            self.assertEqual(
                await tools.execute(command, 1), {"error": "tool_unavailable"}
            )
            self.assertEqual(
                await tools.execute(publication, 1), {"error": "tool_unavailable"}
            )
            execute.assert_not_awaited()
            self.assertEqual(
                (await tools.execute_trusted_render(command, 1))["data"],
                {"exit_code": 0},
            )
            execute.assert_awaited_once_with("true", 1)
