"""Owner UI transport boundaries; no model calls."""

import os
import unittest
from unittest.mock import patch

from services.orchestrator.code_ui import relay_header
from services.orchestrator.ui_injection import inject_atlas_ui


class CodeUITransportTests(unittest.TestCase):
    def test_owner_marker_requires_flag_and_never_stable(self) -> None:
        shell = b"<html><head></head></html>"
        for stable in (True, False):
            for enabled in (True, False):
                result = inject_atlas_ui(shell, stable=stable, code_mode=enabled)
                self.assertEqual(b'content="1"' in result, enabled and not stable)

    def test_relay_scope_and_origin(self) -> None:
        header = b"POST /atlas-code/v1/chat/completions HTTP/1.1\r\nHost: localhost:9300\r\nOrigin: http://localhost:9300\r\nCookie: example\r\nAuthorization: example\r\nContent-Length: 3"
        with patch.dict(os.environ, {"ATLAS_CODE_MODE": "1", "ATLAS_STABLE": "0"}):
            result = relay_header(header)
            assert result is not None
            self.assertTrue(result.startswith(b"POST /v1/chat/completions"))
            self.assertNotIn(b"Origin:", result)
            self.assertNotIn(b"Cookie:", result)
            self.assertNotIn(b"Authorization:", result)
            self.assertIn(b"Content-Length: 3", result)
            self.assertIsNone(
                relay_header(
                    header.replace(
                        b"Origin: http://localhost:9300",
                        b"Origin: https://foreign.invalid",
                    )
                )
            )
            self.assertIsNone(
                relay_header(header.replace(b"/v1/chat/completions", b"/admin"))
            )
            for env in ({"ATLAS_STABLE": "1"}, {"ATLAS_CODE_MODE": "0"}):
                with patch.dict(os.environ, env):
                    self.assertIsNone(relay_header(header))
