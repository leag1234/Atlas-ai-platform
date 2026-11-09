import os
import tempfile
from pathlib import Path
import hashlib
import unittest
from unittest.mock import patch
from uuid import uuid4

from packages.ports import atlas_ports
from services.orchestrator import work_egress
from services.orchestrator.code_workspace import Workspace


class WorkEgressTests(unittest.TestCase):
    def test_allowlist_parser_accepts_only_registry_hosts(self) -> None:
        for host in work_egress.ALLOWED_HOSTS:
            self.assertTrue(work_egress.decide(host).allowed)
        self.assertFalse(work_egress.decide("example.com").allowed)
        self.assertFalse(work_egress.decide("pypi.org.example.com").allowed)
        self.assertFalse(work_egress.decide("").allowed)

    def test_allowlist_parser_normalizes_host_input(self) -> None:
        self.assertTrue(work_egress.decide(" PYPI.org. ").allowed)
        self.assertTrue(work_egress.decide("pypi.org:443").allowed)
        self.assertTrue(work_egress.decide("[pypi.org]:443").allowed)
        self.assertTrue(work_egress.decide("https://pypi.org/path").allowed)

    def test_ip_literals_and_private_resolutions_are_denied(self) -> None:
        for host in ("127.0.0.1", "169.254.169.254", "10.0.0.1", "::1"):
            decision = work_egress.decide(host)
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason, "ip_literal_denied")
        self.assertFalse(
            work_egress.decide(
                "pypi.org", resolved_addresses=frozenset({"10.0.0.1"})
            ).allowed
        )
        self.assertFalse(
            work_egress.decide(
                "pypi.org", resolved_addresses=frozenset({"not-an-ip"})
            ).allowed
        )
        self.assertTrue(
            work_egress.decide(
                "pypi.org", resolved_addresses=frozenset({"151.101.0.1"})
            ).allowed
        )

    def test_squid_configuration_is_host_scoped_and_path_free(self) -> None:
        configuration = work_egress.squid_configuration("work-session")
        self.assertIn("acl allowed_hosts dstdomain", configuration)
        self.assertIn("pypi.org registry.npmjs.org", configuration)
        self.assertIn(
            "acl ipv4_destination dstdom_regex ^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$",
            configuration,
        )
        self.assertIn(
            "acl ipv6_destination dstdom_regex ^\\[[0-9A-Fa-f:]+\\]$",
            configuration,
        )
        self.assertIn(
            "acl private_destination dst 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 "
            "127.0.0.0/8 169.254.0.0/16 100.64.0.0/10 224.0.0.0/4 ::1 fc00::/7 "
            "fe80::/10 ff00::/8",
            configuration,
        )
        self.assertIn("http_access deny ipv4_destination", configuration)
        self.assertIn("http_access deny ipv6_destination", configuration)
        self.assertIn("http_access deny private_destination", configuration)
        self.assertIn(
            "http_access deny connect_method !connect_port_443", configuration
        )
        self.assertIn("http_access deny all", configuration)
        self.assertIn(
            'logformat atlas_egress %tl "work-session" %>rd %<st %Ss', configuration
        )
        self.assertIn(
            "access_log /var/log/squid/access.log atlas_egress", configuration
        )
        self.assertNotIn("%ru", configuration)

    def test_docker_arguments_stay_off_by_default(self) -> None:
        with patch.dict(os.environ, {"ATLAS_WORK_EGRESS": ""}):
            workspace = Workspace(str(uuid4()))
            self.assertFalse(workspace.egress)
            args = workspace.docker_arguments(["--mount", "workspace"], "pass")
            self.assertIsInstance(args, list)
            self.assertIn("--network", args)
            self.assertEqual(args[args.index("--network") + 1], "none")
            self.assertEqual(args[args.index("--memory") + 1], "768m")
            self.assertEqual(args[args.index("--pids-limit") + 1], "64")
            self.assertNotIn("HTTP_PROXY=", args)

    def test_docker_arguments_enable_filtered_egress(self) -> None:
        with patch.dict(os.environ, {"ATLAS_WORK_EGRESS": "registries"}):
            workspace = Workspace(str(uuid4()))
            self.assertTrue(workspace.egress)
            args = workspace.docker_arguments(["--mount", "workspace"], "pass")
            self.assertEqual(
                args[args.index("--network") + 1],
                work_egress.internal_network_name(atlas_ports().base),
            )
            self.assertEqual(args[args.index("--memory") + 1], "2g")
            self.assertEqual(args[args.index("--pids-limit") + 1], "256")
            self.assertIn(f"HTTP_PROXY=http://{workspace.proxy_host}:3128", args)
            self.assertIn(f"HTTPS_PROXY=http://{workspace.proxy_host}:3128", args)
            self.assertIn("NO_PROXY=localhost,127.0.0.1,::1", args)
            self.assertIn("HOME=/home/atlas", args)
            self.assertIn("PIP_USER=1", args)
            self.assertIn("npm_config_prefix=/home/atlas/npm", args)

    def test_egress_proxy_identity_is_session_unique(self) -> None:
        first = Workspace(str(uuid4()))
        second = Workspace(str(uuid4()))
        self.assertNotEqual(first.proxy_host, second.proxy_host)
        self.assertNotEqual(
            work_egress.proxy_name(first.key), work_egress.proxy_name(second.key)
        )

    def test_access_log_name_is_flat_and_proxy_can_write_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"ATLAS_WORK_EGRESS": "registries"}):
                workspace = Workspace(str(uuid4()))
                log_dir = Path(temporary)
                with (
                    patch("os.chown") as chown,
                    patch("os.chmod") as chmod,
                ):
                    access_log = workspace.prepare_access_log(log_dir)
                expected = hashlib.sha256(workspace.key.encode()).hexdigest() + ".log"
                self.assertEqual(access_log.name, expected)
                self.assertNotIn("/", access_log.name)
                chown.assert_called_once_with(access_log, 13, 13)
                chmod.assert_called_once_with(access_log, 0o620)
                args = workspace.egress_proxy_arguments(access_log)
                mount = args[args.index("--mount") + 1]
                self.assertIn(f"src={access_log}", mount)
                self.assertIn("dst=/var/log/squid/access.log", mount)
                # Squid runs as its own user without capabilities, with a writable /tmp,
                # and starts a single process (verified live on staging, 2026-11-09).
                self.assertEqual(args[args.index("--user") + 1], "13:13")
                self.assertEqual(args[args.index("--cap-drop") + 1], "ALL")
                self.assertIn("/tmp:rw,nosuid,size=8m", args)
                self.assertNotIn("squid -z", args[-1])


if __name__ == "__main__":
    unittest.main()
