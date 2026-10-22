import os
import unittest
from unittest.mock import patch

from packages.ports import DEFAULT_PORTS, atlas_ports, port_base


class PortConfigurationTests(unittest.TestCase):
    def test_defaults_preserve_current_ports_and_names(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            ports = atlas_ports()
            self.assertEqual(ports.port("chat_ui"), 3000)
            self.assertEqual(ports.port("terminal"), 8000)
            self.assertEqual(ports.port("gateway"), 8010)
            self.assertEqual(ports.port("adapter"), 8020)
            self.assertEqual(ports.port("postgres"), 5432)
            self.assertEqual(ports.name("atlas-chat-ui"), "atlas-chat-ui")

    def test_offset_changes_every_resolved_endpoint_and_resource_name(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PORT_BASE": "100"}, clear=True):
            ports = atlas_ports()
            resolved = [ports.url(service) for service in DEFAULT_PORTS]
            self.assertEqual(ports.port("adapter"), 8120)
            self.assertEqual(ports.name("atlas-chat-ui"), "atlas-chat-ui-100")
            self.assertEqual(ports.name("atlas-chat-index"), "atlas-chat-index-100")
            self.assertEqual(
                ports.name("atlas-terminal-files"), "atlas-terminal-files-100"
            )
            for default in DEFAULT_PORTS.values():
                self.assertNotIn(":" + str(default), " ".join(resolved))

    def test_invalid_offset_is_rejected(self) -> None:
        for value in ("-1", "nope", "70000"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "invalid_atlas_port_base"),
            ):
                port_base(value)
