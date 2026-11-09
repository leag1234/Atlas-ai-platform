"""Policy and configuration for opt-in Work registry egress."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import hashlib
import os
from typing import Final
from urllib.parse import urlsplit


EGRESS_MODE = "registries"
# The digest is to be pinned by the supervisor after verifying the image.
PROXY_IMAGE = "ubuntu/squid:latest"
ALLOWED_HOSTS: Final[frozenset[str]] = frozenset(
    (
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "github.com",
        "codeload.github.com",
        "objects.githubusercontent.com",
    )
)
PROXY_PORT = 3128
NO_PROXY = "localhost,127.0.0.1,::1"


def enabled() -> bool:
    return os.environ.get("ATLAS_WORK_EGRESS") == EGRESS_MODE


def internal_network_name(port_base: int) -> str:
    return "atlas-work-egress" if port_base == 0 else f"atlas-work-egress-{port_base}"


def external_network_name(port_base: int) -> str:
    suffix = "" if port_base == 0 else f"-{port_base}"
    return f"atlas-work-egress-upstream{suffix}"


def proxy_name(session_key: str) -> str:
    return "atlas-work-egress-" + hashlib.sha256(session_key.encode()).hexdigest()[:24]


def proxy_host(session_key: str) -> str:
    return "egress-" + hashlib.sha256(session_key.encode()).hexdigest()[:20]


def normalize_host(value: str) -> str:
    value = value.strip()
    if "://" in value:
        value = urlsplit(value).netloc
    host = value.lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    elif host.startswith("[") and "]:" in host:
        host = host[1 : host.index("]:")]
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if host.count(":") == 1 and not host.startswith(":"):
        host = host.rsplit(":", 1)[0]
    return host


@dataclass(frozen=True)
class EgressDecision:
    allowed: bool
    reason: str


def decide(
    host: str, *, resolved_addresses: frozenset[str] = frozenset()
) -> EgressDecision:
    normalized = normalize_host(host)
    if not normalized:
        return EgressDecision(False, "invalid_host")
    try:
        ip_address(normalized)
    except ValueError:
        if normalized not in ALLOWED_HOSTS:
            return EgressDecision(False, "host_not_allowed")
    else:
        return EgressDecision(False, "ip_literal_denied")
    for value in resolved_addresses:
        try:
            resolved = ip_address(value)
        except ValueError:
            return EgressDecision(False, "invalid_resolution")
        if (
            resolved.is_private
            or resolved.is_loopback
            or resolved.is_link_local
            or resolved.is_multicast
            or resolved.is_reserved
            or resolved.is_unspecified
        ):
            return EgressDecision(False, "private_address")
    return EgressDecision(True, "allowed")


def squid_configuration(session_label: str) -> str:
    allowed = " ".join(sorted(ALLOWED_HOSTS))
    safe_label = session_label.replace('"', "'")
    return f"""http_port {PROXY_PORT}
acl allowed_hosts dstdomain {allowed}
acl connect_method method CONNECT
acl ipv4_destination dstdom_regex ^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$
acl ipv6_destination dstdom_regex ^\\[[0-9A-Fa-f:]+\\]$
acl private_destination dst 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.0/8 169.254.0.0/16 100.64.0.0/10 224.0.0.0/4 ::1 fc00::/7 fe80::/10 ff00::/8
acl connect_port_443 port 443
http_access deny ipv4_destination
http_access deny ipv6_destination
http_access deny private_destination
http_access deny connect_method !connect_port_443
http_access allow allowed_hosts
http_access deny all
visible_hostname atlas-work-egress
pid_filename /tmp/squid.pid
forwarded_for delete
via on
logformat atlas_egress %tl "{safe_label}" %>rd %<st %Ss
access_log /var/log/squid/access.log atlas_egress
cache deny all
"""
