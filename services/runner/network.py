"""Container egress permits only DNS and authenticated registered SSH endpoints."""

import ipaddress
import os
import socket
import subprocess
import threading

from services.runner.policy import RunnerError
from services.runner.ssh import environment

_lock = threading.Lock()


def rule(*args: str) -> None:
    reply = subprocess.run(
        ["/usr/sbin/iptables", "-w", "5", *args],
        env=environment(),
        capture_output=True,
        timeout=10,
    )
    if reply.returncode:
        raise RunnerError("runner_egress_policy_failed")


def initialize() -> None:
    if os.environ.get("ATLAS_RUNNER_FIREWALL") != "1":
        return
    rule("-P", "INPUT", "DROP")
    rule(
        "-A",
        "INPUT",
        "-m",
        "conntrack",
        "--ctstate",
        "ESTABLISHED,RELATED",
        "-j",
        "ACCEPT",
    )
    rule("-A", "INPUT", "-i", "eth0", "-p", "tcp", "--dport", "13712", "-j", "ACCEPT")
    # Docker DNATs its resolver to ephemeral loopback ports. These queries
    # traverse INPUT too; permitting OUTPUT alone silently drops every lookup.
    for protocol in ("udp", "tcp"):
        rule(
            "-A",
            "INPUT",
            "-i",
            "lo",
            "-d",
            "127.0.0.11",
            "-p",
            protocol,
            "-j",
            "ACCEPT",
        )
    rule("-P", "OUTPUT", "DROP")
    rule(
        "-A",
        "OUTPUT",
        "-m",
        "conntrack",
        "--ctstate",
        "ESTABLISHED,RELATED",
        "-j",
        "ACCEPT",
    )
    rule("-A", "OUTPUT", "-d", "127.0.0.11", "-p", "udp", "-j", "ACCEPT")
    rule("-A", "OUTPUT", "-d", "127.0.0.11", "-p", "tcp", "-j", "ACCEPT")


def vetted(host: str, port: int) -> str:
    """One IPv4 address for a member-declared host, or a refusal.

    A member chooses the host of a personal machine, so the name alone proves
    nothing: it may resolve to this deployment's own network. Every address
    must be public, or inside a network the operator listed in
    ATLAS_RUNNER_PERSONAL_NETWORKS (comma-separated CIDR). The caller connects
    to the returned address, never to the name again.
    """
    try:
        networks = [
            ipaddress.ip_network(value.strip())
            for value in os.environ.get("ATLAS_RUNNER_PERSONAL_NETWORKS", "").split(",")
            if value.strip()
        ]
    except ValueError:
        raise RunnerError("runner_egress_policy_failed") from None
    try:
        found = sorted(
            {
                str(item[4][0])
                for item in socket.getaddrinfo(
                    host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
                )
            }
        )
    except (OSError, UnicodeError):
        raise RunnerError("runner_target_unresolved") from None
    if not found:
        raise RunnerError("runner_target_unresolved")
    for value in found:
        address = ipaddress.IPv4Address(value)
        if not address.is_global and not any(address in net for net in networks):
            raise RunnerError("runner_target_address_denied")
    return found[0]


def allow(host: str, port: int) -> None:
    if os.environ.get("ATLAS_RUNNER_FIREWALL") != "1":
        return
    with _lock:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
                )
            }
        except OSError:
            raise RunnerError("runner_target_unresolved") from None
        for value in addresses:
            address = str(ipaddress.IPv4Address(value))
            args = [
                "OUTPUT",
                "-d",
                address,
                "-p",
                "tcp",
                "--dport",
                str(port),
                "-j",
                "ACCEPT",
            ]
            checked = subprocess.run(
                ["/usr/sbin/iptables", "-w", "5", "-C", *args],
                env=environment(),
                capture_output=True,
                timeout=10,
            )
            if checked.returncode:
                rule("-A", *args)
