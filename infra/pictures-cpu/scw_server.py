"""The CPU picture server's Scaleway Instance, from the Atlas host.

Credentials come from the environment (SCW_SECRET_KEY, SCW_DEFAULT_PROJECT_ID,
loaded by the caller) and are never printed. Every server, IP and security
group made here carries the tag `pictures-cpu`; delete, stop and start act
only on a server whose name starts with `atlas-pictures-cpu` AND carries the
tag.

  scw_server.py create ZONE TYPE DISK_GB CLIENT_IP SSH_PUBLIC_KEY_FILE
      security group (inbound: SSH and 8790 from CLIENT_IP only), flexible
      IPv4, server `atlas-pictures-cpu`, cloud-init (docker, key), power on
  scw_server.py list               servers, IPs, groups with the tag
  scw_server.py stop ZONE ID       power off (compute no longer billed;
                                   volume and IP still are)
  scw_server.py start ZONE ID      power on (the container restarts by itself)
  scw_server.py delete ZONE ID     power off, delete server, volumes, IP, group
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

NAME, TAG = "atlas-pictures-cpu", "pictures-cpu"
API = "https://api.scaleway.com"
ZONES = ("fr-par-1", "fr-par-2", "fr-par-3")
PORT = 8790
# Ubuntu 24.04 (Noble) x86_64 images, read from the Instance API 2026-11-13.
IMAGES = {
    "fr-par-1": "a85e8bd8-26d9-4093-933e-d94b0125d60f",
    "fr-par-2": "65fe9f32-fcd0-4766-aa6e-33086a5becf2",
}


def call(
    method: str, path: str, body: Any = None, ctype: str = "application/json"
) -> tuple[int, Any]:
    data = (
        None
        if body is None
        else (body if isinstance(body, bytes) else json.dumps(body).encode())
    )
    request = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={"X-Auth-Token": os.environ["SCW_SECRET_KEY"], "Content-Type": ctype},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"raw": raw[:300].decode("utf-8", "replace")}


def ours(server: dict[str, Any]) -> bool:
    return str(server.get("name", "")).startswith(NAME) and TAG in (
        server.get("tags") or []
    )


def create(zone: str, ctype: str, disk_gb: str, client_ip: str, key_file: str) -> None:
    project = os.environ["SCW_DEFAULT_PROJECT_ID"]
    rules_base = f"/instance/v1/zones/{zone}/security_groups"
    code, group = call(
        "POST",
        rules_base,
        {
            "name": NAME,
            "project": project,
            "tags": [TAG],
            "stateful": True,
            "inbound_default_policy": "drop",
            "outbound_default_policy": "accept",
            "description": "CPU picture server: SSH and gate from the Atlas host only",
        },
    )
    if code >= 300:
        sys.exit(f"security group: {code} {json.dumps(group)[:300]}")
    group_id = group["security_group"]["id"]
    for port in (22, PORT):
        code, _ = call(
            "POST",
            f"{rules_base}/{group_id}/rules",
            {
                "protocol": "TCP",
                "direction": "inbound",
                "action": "accept",
                "ip_range": f"{client_ip}/32",
                "dest_port_from": port,
            },
        )
        if code >= 300:
            sys.exit(f"rule {port}: {code}")
    code, ip = call(
        "POST",
        f"/instance/v1/zones/{zone}/ips",
        {"project": project, "type": "routed_ipv4", "tags": [TAG]},
    )
    if code >= 300:
        sys.exit(f"ip: {code}")
    ip = ip["ip"]
    body = {
        "name": NAME,
        "commercial_type": ctype,
        "image": IMAGES[zone],
        "project": project,
        "tags": [TAG],
        "public_ips": [ip["id"]],
        "security_group": group_id,
        "volumes": {"0": {"size": int(disk_gb) * 10**9, "volume_type": "sbs_volume"}},
    }
    code, server = call("POST", f"/instance/v1/zones/{zone}/servers", body)
    if code >= 300:
        call("DELETE", f"/instance/v1/zones/{zone}/ips/{ip['id']}")
        call("DELETE", f"{rules_base}/{group_id}")
        sys.exit(f"server: {code} {json.dumps(server)[:300]}")
    server = server["server"]
    key = Path(key_file).read_text().strip()
    cloud = (
        "#cloud-config\nssh_authorized_keys:\n  - " + key + "\npackage_update: true\n"
        "packages:\n  - docker.io\n  - git\n  - curl\nruncmd:\n"
        "  - [systemctl, enable, --now, docker]\n  - [touch, /root/pictures-cpu-ready]\n"
    )
    call(
        "PATCH",
        f"/instance/v1/zones/{zone}/servers/{server['id']}/user_data/cloud-init",
        cloud.encode(),
        "text/plain",
    )
    call(
        "POST",
        f"/instance/v1/zones/{zone}/servers/{server['id']}/action",
        {"action": "poweron"},
    )
    print(
        json.dumps(
            {
                "id": server["id"],
                "zone": zone,
                "ip": ip["address"],
                "type": ctype,
                "security_group": group_id,
            }
        )
    )


def _server(zone: str, server_id: str) -> dict[str, Any]:
    code, reply = call("GET", f"/instance/v1/zones/{zone}/servers/{server_id}")
    if code != 200 or not ours(reply["server"]):
        sys.exit("refused: not the picture server")
    return reply["server"]


def _wait(zone: str, server_id: str, state: str) -> None:
    for _ in range(120):
        code, reply = call("GET", f"/instance/v1/zones/{zone}/servers/{server_id}")
        if code == 200 and reply["server"]["state"] == state:
            return
        time.sleep(5)
    sys.exit(f"timeout waiting for {state}")


def power(zone: str, server_id: str, action: str) -> None:
    server = _server(zone, server_id)
    target = "stopped" if action == "poweroff" else "running"
    if server["state"] != target:
        call(
            "POST",
            f"/instance/v1/zones/{zone}/servers/{server_id}/action",
            {"action": action},
        )
        _wait(zone, server_id, target)
    print(json.dumps({"id": server_id, "state": target}))


def delete(zone: str, server_id: str) -> None:
    server = _server(zone, server_id)
    volumes = [
        (v["id"], v.get("volume_type")) for v in server.get("volumes", {}).values()
    ]
    ips = [i["id"] for i in server.get("public_ips") or []]
    group = (server.get("security_group") or {}).get("id")
    if server["state"] != "stopped":
        call(
            "POST",
            f"/instance/v1/zones/{zone}/servers/{server_id}/action",
            {"action": "poweroff"},
        )
        _wait(zone, server_id, "stopped")
    result: dict[str, int] = {}
    result["server"], _ = call(
        "DELETE", f"/instance/v1/zones/{zone}/servers/{server_id}"
    )
    for volume, kind in volumes:
        path = (
            f"/block/v1alpha1/zones/{zone}/volumes/{volume}"
            if kind == "sbs_volume"
            else f"/instance/v1/zones/{zone}/volumes/{volume}"
        )
        for _ in range(30):
            code, _ = call("DELETE", path)
            if code in (200, 204, 404):
                break
            time.sleep(5)
        result["volume_" + volume[:8]] = code
    for ip in ips:
        code, reply = call("GET", f"/instance/v1/zones/{zone}/ips/{ip}")
        if code == 200 and TAG in (reply["ip"].get("tags") or []):
            result["ip_" + ip[:8]], _ = call(
                "DELETE", f"/instance/v1/zones/{zone}/ips/{ip}"
            )
    if group:
        code, reply = call("GET", f"/instance/v1/zones/{zone}/security_groups/{group}")
        if code == 200 and TAG in (reply["security_group"].get("tags") or []):
            result["group"], _ = call(
                "DELETE", f"/instance/v1/zones/{zone}/security_groups/{group}"
            )
    print(json.dumps(result))


def listing() -> None:
    out: dict[str, Any] = {}
    for zone in ZONES:
        _, servers = call(
            "GET", f"/instance/v1/zones/{zone}/servers?per_page=100&tags={TAG}"
        )
        _, ips = call("GET", f"/instance/v1/zones/{zone}/ips?per_page=100&tags={TAG}")
        _, groups = call(
            "GET", f"/instance/v1/zones/{zone}/security_groups?per_page=100&tags={TAG}"
        )
        out[zone] = {
            "servers": [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "state": s["state"],
                    "type": s["commercial_type"],
                    "ip": [i["address"] for i in s.get("public_ips") or []],
                }
                for s in servers.get("servers", [])
            ],
            "ips": [
                {"id": i["id"], "address": i["address"]} for i in ips.get("ips", [])
            ],
            "security_groups": [
                {"id": g["id"], "name": g["name"]}
                for g in groups.get("security_groups", [])
            ],
        }
    print(json.dumps(out, indent=1))


def main() -> None:
    if not os.environ.get("SCW_SECRET_KEY"):
        sys.exit("no Scaleway key in the environment")
    action = sys.argv[1]
    if action == "create":
        create(*sys.argv[2:7])
    elif action == "list":
        listing()
    elif action == "stop":
        power(sys.argv[2], sys.argv[3], "poweroff")
    elif action == "start":
        power(sys.argv[2], sys.argv[3], "poweron")
    elif action == "delete":
        delete(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
