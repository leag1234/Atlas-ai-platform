"""Project mode beside the chat keeps the chat's file, download and image routes."""

import os
import unittest
from unittest.mock import patch

from services.orchestrator.code_ui import legacy_project_route
from services.orchestrator.terminal_stack import stable_route_allowed

CHAT_ROUTES = (
    "/api/v1/files/",
    "/api/v1/files/00000000-0000-4000-8000-000000000001/content",
    "/api/v1/terminals/atlas-files/files/serve/home/user/requests/chatcmpl-"
    + "a" * 32
    + "/report.pdf",
    "/images/" + "a" * 32,
)
LEGACY_ROUTES = ("/projects", "/code/ui", "/atlas-code/code/ui", "/%63ode/ui")


class GuestProjectRoutes(unittest.TestCase):
    def test_guest_instance_keeps_chat_routes(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PROJECT_MODE": "1"}):
            os.environ.pop("ATLAS_PROJECT_ONLY", None)
            for route in CHAT_ROUTES:
                self.assertFalse(legacy_project_route(route), route)
            self.assertTrue(
                stable_route_allowed(
                    b"POST /api/v1/files/?process=false HTTP/1.1\r\nHost: guest"
                )
            )
            for route in LEGACY_ROUTES:
                self.assertTrue(legacy_project_route(route), route)

    def test_project_only_instance_closes_chat_routes(self) -> None:
        with patch.dict(
            os.environ, {"ATLAS_PROJECT_MODE": "1", "ATLAS_PROJECT_ONLY": "1"}
        ):
            for route in CHAT_ROUTES + LEGACY_ROUTES:
                self.assertTrue(legacy_project_route(route), route)
            self.assertFalse(
                stable_route_allowed(
                    b"POST /api/v1/files/?process=false HTTP/1.1\r\nHost: project"
                )
            )

    def test_without_project_mode_nothing_is_closed(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PROJECT_ONLY": "1"}):
            os.environ.pop("ATLAS_PROJECT_MODE", None)
            os.environ.pop("ATLAS_PROJECT_DSN", None)
            for route in CHAT_ROUTES:
                self.assertFalse(legacy_project_route(route), route)


# Files a browser loads to start the interface, the same for every user.
PUBLIC_FILES = [
    b"GET /_app/immutable/chunks/Ab1-c_D.js HTTP/1.1",
    b"GET /_app/immutable/assets/0.Xy.css HTTP/1.1",
    b"GET /_app/immutable/entry/start.oikFGM3s.js HTTP/1.1",
    b"GET /_app/version.json HTTP/1.1",
    b"GET /static/loader.js HTTP/1.1",
    b"GET /static/favicon.png?v=2 HTTP/1.1",
    b"GET /static/atlas/chat-ui.js HTTP/1.1",
    b"GET /static/atlas/brand/svg/atlas-symbol.svg HTTP/1.1",
    b"GET /manifest.json HTTP/1.1",
    b"GET /favicon.png HTTP/1.1",
    b"GET /user.png HTTP/1.1",
    b"GET /api/atlas/keys/client.js HTTP/1.1",
    b"GET /api/atlas/keys/style.css HTTP/1.1",
    b"GET /api/atlas/user-memory/client.js HTTP/1.1",
    b"GET /api/atlas/voice/client.js HTTP/1.1",
    b"GET /project-mode/client.js HTTP/1.1",
    b"GET /project-mode/locale.js HTTP/1.1",
    b"GET /project-mode/shell.js HTTP/1.1",
    b"GET /project-mode/style.css HTTP/1.1",
]
# Everything else needs a named user, whatever the method or encoding.
PRIVATE_READS = [
    b"POST /_app/immutable/chunks/Ab1.js HTTP/1.1",
    b"PUT /static/loader.js HTTP/1.1",
    b"GET /_app/immutable/../../api/v1/chats HTTP/1.1",
    b"GET /_app/immutable/%2e%2e/%2e%2e/api/config HTTP/1.1",
    b"GET /_app/immutable/%252e%252e/api/config HTTP/1.1",
    b"GET /static/%2e%2e/api/v1/chats HTTP/1.1",
    b"GET /static/ HTTP/1.1",
    b"GET /_app/immutablex/a.js HTTP/1.1",
    b"GET /_app/env.js HTTP/1.1",
    b"GET / HTTP/1.1",
    b"GET /?connexion HTTP/1.1",
    b"GET /c/123 HTTP/1.1",
    b"GET /favicon.ico HTTP/1.1",
    b"GET /manifest.json/x HTTP/1.1",
    b"GET /robots.txt HTTP/1.1",
    b"GET /favicon.png/x HTTP/1.1",
    b"GET /api/config HTTP/1.1",
    b"GET /api/v1/auths/ HTTP/1.1",
    b"GET /api/v1/files/abc/content HTTP/1.1",
    b"GET /api/atlas/me HTTP/1.1",
    b"GET /api/atlas/keys HTTP/1.1",
    b"GET /api/atlas/voice/config HTTP/1.1",
    b"GET /project-mode HTTP/1.1",
    b"GET /project-mode/other.js HTTP/1.1",
    b"GET /ws/socket.io/?EIO=4 HTTP/1.1",
    b"GET /staticx/a.js HTTP/1.1",
]


class FrontDoorIdentityTests(unittest.TestCase):
    def test_public_interface_files_need_no_user(self) -> None:
        from services.orchestrator.terminal_stack import identity_allowed

        empty = b"\r\nHost: guest\r\nX-Atlas-User: @guest"
        for line in PUBLIC_FILES:
            self.assertTrue(identity_allowed(line + empty), line)
            head = b"HEAD" + line.removeprefix(b"GET")
            self.assertTrue(identity_allowed(head + empty), head)
        for line in PRIVATE_READS:
            self.assertFalse(identity_allowed(line + empty), line)

    def test_front_door_template_serves_exactly_the_public_files(self) -> None:
        """nginx lets anonymous reads reach the relay only where the relay accepts them."""
        from pathlib import Path
        import re

        from services.orchestrator.terminal_stack import public_interface_file

        for name in ("atlas.indidea.org.conf", "atlas.test.conf"):
            text = Path("infra/stable/nginx", name).read_text()
            found = re.findall(
                r"(?m)^    location ~ (\^/\(\?:_app/immutable/\S+) \{$", text
            )
            self.assertEqual(len(found), 1, name)
            edge = re.compile(found[0])
            for line in PUBLIC_FILES + PRIVATE_READS:
                method, target = line.decode().split(" ")[:2]
                if method != "GET" or "%" in target or "/../" in target:
                    # limit_except refuses other methods; nginx matches the decoded,
                    # normalized path, which the relay checks again.
                    continue
                path = target
                with self.subTest(name=name, path=path):
                    self.assertEqual(
                        edge.search(path.split("?")[0]) is not None,
                        public_interface_file(line + b"\r\nHost: guest"),
                    )

    def test_anonymous_interface_read_carries_no_session(self) -> None:
        from services.orchestrator.terminal_stack import anonymous_interface_read

        header = anonymous_interface_read(
            b"GET /static/loader.js HTTP/1.1\r\nHost: guest\r\nX-Atlas-User: @guest"
            b"\r\nCookie: token=abc\r\nAuthorization: Basic eDp5\r\nAccept: */*"
        )
        self.assertEqual(
            header, b"GET /static/loader.js HTTP/1.1\r\nHost: guest\r\nAccept: */*"
        )

    def test_empty_user_only_beside_an_interface_key(self) -> None:
        from services.orchestrator.terminal_stack import identity_allowed

        base = b"POST /api/chat/completions HTTP/1.1\r\nHost: guest"
        self.assertTrue(identity_allowed(base + b"\r\nX-Atlas-User: ana@guest"))
        self.assertFalse(identity_allowed(base + b"\r\nX-Atlas-User: @guest"))
        self.assertFalse(identity_allowed(base + b"\r\nX-Atlas-User: "))
        self.assertFalse(identity_allowed(base))
        self.assertTrue(
            identity_allowed(
                base + b"\r\nX-Atlas-User: @guest\r\nAuthorization: Bearer sk-abc123"
            )
        )
        self.assertFalse(
            identity_allowed(
                base + b"\r\nX-Atlas-User: @guest\r\nAuthorization: Bearer atl_x"
            )
        )


class RelayBacklogTests(unittest.TestCase):
    def test_front_relays_listen_with_a_deep_backlog(self) -> None:
        import inspect

        from services.orchestrator import terminal_stack

        source = inspect.getsource(terminal_stack)
        self.assertGreaterEqual(terminal_stack.LISTEN_BACKLOG, 512)
        self.assertEqual(
            source.count("request_queue_size = LISTEN_BACKLOG"),
            source.count("(socketserver.ThreadingTCPServer)"),
        )


if __name__ == "__main__":
    unittest.main()
