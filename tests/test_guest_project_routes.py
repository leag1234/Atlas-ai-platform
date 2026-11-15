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


class FrontDoorIdentityTests(unittest.TestCase):
    def test_public_build_assets_need_no_user(self) -> None:
        from services.orchestrator.terminal_stack import identity_allowed

        empty = b"\r\nHost: guest\r\nX-Atlas-User: @guest"
        self.assertTrue(
            identity_allowed(b"GET /_app/immutable/chunks/Ab1-c_D.js HTTP/1.1" + empty)
        )
        self.assertTrue(
            identity_allowed(b"HEAD /_app/immutable/assets/0.Xy.css HTTP/1.1" + empty)
        )
        for line in (
            b"POST /_app/immutable/chunks/Ab1.js HTTP/1.1",
            b"GET /_app/immutable/../../api/v1/chats HTTP/1.1",
            b"GET /_app/immutable/%2e%2e/%2e%2e/api/config HTTP/1.1",
            b"GET /_app/immutable/%252e%252e/api/config HTTP/1.1",
            b"GET /_app/version.json HTTP/1.1",
            b"GET /api/v1/auths/ HTTP/1.1",
            b"GET /_app/immutablex/a.js HTTP/1.1",
        ):
            self.assertFalse(identity_allowed(line + empty), line)

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
