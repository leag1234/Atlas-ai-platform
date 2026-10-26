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
