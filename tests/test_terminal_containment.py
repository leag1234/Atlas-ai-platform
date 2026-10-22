"""Fail closed before making the terminal API available to model calls."""

import unittest
from unittest.mock import patch

from services.orchestrator import terminal_stack


class ContainmentTests(unittest.TestCase):
    def test_both_families_must_be_closed_before_latch(self) -> None:
        events: list[tuple[str, ...]] = []

        def command(*args: str) -> str:
            events.append(args)
            return "42"

        def firewall(pid: str, family: str, *args: str) -> str:
            events.append((family, *args))
            return "-P OUTPUT DROP\n-A OUTPUT -j REJECT"

        with (
            patch.object(terminal_stack, "command", command),
            patch.object(terminal_stack, "firewall", firewall),
        ):
            terminal_stack.contain("test-terminal")
        self.assertEqual(
            events[-1],
            ("docker", "exec", "test-terminal", "touch", "/tmp/atlas-network-ready"),
        )
        for family in ("iptables", "ip6tables"):
            self.assertIn((family, "-P", "OUTPUT", "DROP"), events)
            self.assertIn((family, "-A", "OUTPUT", "-j", "REJECT"), events)
            self.assertIn((family, "-S", "OUTPUT"), events)

    def test_failed_ipv6_verification_never_opens_latch(self) -> None:
        def firewall(pid: str, family: str, *args: str) -> str:
            return (
                "" if family == "ip6tables" else "-P OUTPUT DROP\n-A OUTPUT -j REJECT"
            )

        with (
            patch.object(terminal_stack, "command", return_value="42") as command,
            patch.object(terminal_stack, "firewall", firewall),
        ):
            with self.assertRaisesRegex(RuntimeError, "containment_unverified"):
                terminal_stack.contain("test-terminal")
        self.assertFalse(any("touch" in call.args for call in command.call_args_list))
