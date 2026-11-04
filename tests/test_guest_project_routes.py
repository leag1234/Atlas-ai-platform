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


if __name__ == "__main__":
    unittest.main()
