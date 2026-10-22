"""REQ-INF-012/013: project-scoped, disposable GPU lifecycle via Scaleway CLI."""

import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.configuration import GPU_REQUIRED_ENV, require_env
from packages.ports import atlas_ports
from packages.gpu_ownership import gpu_owner, gpu_name, require_gpu

TAG = "atlas-m1"


def owner_tag() -> str:
    return "atlas-owner=" + gpu_owner()


def state_path() -> Path:
    return Path("BRAIN") / ("gpu-root-" + gpu_owner() + ".json")


def exact_owner(record: dict[str, Any]) -> bool:
    tags = record.get("tags", [])
    return [tag for tag in tags if tag.startswith("atlas-owner=")] == [owner_tag()]


def legacy_demo(record: dict[str, Any]) -> bool:
    return (
        os.environ.get("ATLAS_GPU_OWNER") == "demo"
        and record.get("name") == "atlas-gpu"
        and not any(tag.startswith("atlas-owner=") for tag in record.get("tags", []))
    )


def require_owner(record: dict[str, Any]) -> None:
    if not exact_owner(record):
        raise RuntimeError("GPU ownership refusal: resource lacks exact " + owner_tag())


def tag_resource(service: str, kind: str, record: dict[str, Any], *extra: str) -> None:
    tags = list(dict.fromkeys([*record.get("tags", []), TAG, owner_tag(), *extra]))
    call(
        service,
        kind,
        "update",
        record["id"],
        *(f"tags.{i}={tag}" for i, tag in enumerate(tags)),
    )
    record["tags"] = tags


def server_owned() -> dict[str, Any] | None:
    return single(owned("instance", "server"))


def checked_server() -> dict[str, Any] | None:
    server = server_owned()
    if server is None:
        return None
    # Preflight every attached child before any mutation, including migration.
    legacy = legacy_demo(server)
    children: list[tuple[str, str, dict[str, Any]]] = []
    refs = [("block", "volume", v["id"]) for v in server.get("volumes", {}).values()]
    refs += [("instance", "ip", ip["id"]) for ip in server.get("public_ips", [])]
    if group := server.get("security_group"):
        refs.append(("instance", "security-group", group["id"]))
    for service, kind, identity in refs:
        records = call(
            service, kind, "list", "project-id=" + os.environ["SCW_DEFAULT_PROJECT_ID"]
        )
        record = next((r for r in records if r["id"] == identity), None)
        if record is None:
            raise RuntimeError(
                "GPU ownership refusal: attached resource missing from project inventory"
            )
        if not (
            legacy
            and not any(t.startswith("atlas-owner=") for t in record.get("tags", []))
        ):
            require_owner(record)
        children.append((service, kind, record))
    if legacy:
        for service, kind, record in children:
            tag_resource(service, kind, record)
        tag_resource("instance", "server", server)
        print("[gpu-owner] adopted legacy demo and attached resources", file=sys.stderr)
    return server


def call(*args: str) -> Any:
    if args[2] not in {"list", "get"}:
        require_gpu()
    result = subprocess.run(
        ["scw", *args, "zone=" + os.environ["SCW_DEFAULT_ZONE"], "-o", "json"],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    if result.returncode:
        # Provider output can contain request data; do not leak it into logs.
        reason = "provider_error"
        diagnostic = (result.stderr or "").lower()
        for category, words in (
            ("stock_unavailable", ("stock", "capacity", "sold out")),
            ("quota_exceeded", ("quota",)),
            ("permission_denied", ("forbidden", "unauthorized")),
        ):
            if any(word in diagnostic for word in words):
                reason = category
                break
        raise RuntimeError(
            f"Scaleway {' '.join(args[:3])}: {reason}, exit {result.returncode}"
        )
    return json.loads(result.stdout) if result.stdout.strip() else None


def owned(service: str, resource: str) -> list[dict[str, Any]]:
    records = call(
        service, resource, "list", "project-id=" + os.environ["SCW_DEFAULT_PROJECT_ID"]
    )
    expected = gpu_name() + "-weights" if resource == "volume" else gpu_name()
    for record in records:
        if record.get("name") == expected and not (
            resource == "server" and legacy_demo(record)
        ):
            require_owner(record)
    return [
        r
        for r in records
        if exact_owner(r) or (resource == "server" and legacy_demo(r))
    ]


def single(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(records) > 1:
        raise RuntimeError("Multiple owned resources: manual reconciliation required")
    return records[0] if records else None


def create(service: str, resource: str, *args: str) -> Any:
    result = call(
        service,
        resource,
        "create",
        "project-id=" + os.environ["SCW_DEFAULT_PROJECT_ID"],
        "tags.0=" + TAG,
        "tags.1=" + owner_tag(),
        *args,
    )
    return result["security_group"] if resource == "security-group" else result


def bootstrap(volume: str, model: str, fresh: bool) -> str:
    port = atlas_ports().port("terminal")
    device = shlex.quote("/dev/disk/by-id/scsi-0SCW_sbs_volume-" + volume)
    public_key = os.environ.get("GPU_SSH_PUBLIC_KEY", "")
    if public_key and not re.fullmatch(
        r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\n]*)?", public_key
    ):
        raise ValueError("Invalid diagnostic public key")
    revision = os.environ.get("LOCAL_MODEL_REVISION", "main")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", revision):
        raise ValueError("Invalid model revision")
    script = f"""#!/bin/bash
set -Eeuo pipefail
mkdir -p /root/.ssh
printf '%s\\n' {shlex.quote(public_key)} >> /root/.ssh/authorized_keys
udevadm settle
DEV={device}
for i in $(seq 1 60); do [[ -b "$DEV" ]] && break; sleep 2; done
[[ -b "$DEV" ]]
{'if ! blkid "$DEV"; then mkfs.ext4 "$DEV"; fi' if fresh else 'test "$(blkid -s TYPE -o value "$DEV")" = ext4'}
mkdir -p /mnt/weights
mount "$DEV" /mnt/weights
systemctl enable --now docker
nvidia-smi
docker run -d --restart unless-stopped --gpus all --ipc=host --name vllm \\
 -p {port}:{port} -v /mnt/weights:/root/.cache/huggingface \\
 vllm/vllm-openai:v0.10.2 --model {shlex.quote(model)} --revision {shlex.quote(revision)} --served-model-name local \\
 --enable-prefix-caching --max-model-len 8192 --port {port}
"""
    return (
        script.split("docker run -d", 1)[0]
        if os.environ.get("GPU_WORKLOAD") == "image"
        else script
    )


def down() -> None:
    server = checked_server()
    for service, kind in (
        ("block", "volume"),
        ("instance", "ip"),
        ("instance", "security-group"),
    ):
        owned(service, kind)
    state = state_path()
    if state.exists():
        saved = json.loads(state.read_text())
        if (
            saved["project"] != os.environ["SCW_DEFAULT_PROJECT_ID"]
            or saved["zone"] != os.environ["SCW_DEFAULT_ZONE"]
            or saved["owner"] != gpu_owner()
        ):
            raise RuntimeError("Cleanup state belongs to another owner/project/zone")
        for volume in call(
            "block",
            "volume",
            "list",
            "project-id=" + os.environ["SCW_DEFAULT_PROJECT_ID"],
        ):
            if volume["id"] == saved["id"]:
                require_owner(volume)
    if server:
        root = server["volumes"]["0"]["id"]
        state.write_text(
            json.dumps(
                {
                    "id": root,
                    "project": os.environ["SCW_DEFAULT_PROJECT_ID"],
                    "zone": os.environ["SCW_DEFAULT_ZONE"],
                    "owner": gpu_owner(),
                }
            )
        )
        # Never let terminate implicitly delete resources that were not checked.
        call(
            "instance",
            "server",
            "terminate",
            server["id"],
            "with-ip=false",
            "with-block=false",
            "--wait",
        )
    if state.exists():
        root = json.loads(state.read_text())["id"]
        volumes = call(
            "block",
            "volume",
            "list",
            "project-id=" + os.environ["SCW_DEFAULT_PROJECT_ID"],
        )
        for volume in volumes:
            if volume["id"] == root:
                require_owner(volume)
                call("block", "volume", "delete", root)
        state.unlink()
    # Also remove tagged roots left by an interrupted creation; retain weights.
    for volume in owned("block", "volume"):
        if "atlas-root" in volume.get("tags", []):
            call("block", "volume", "delete", volume["id"])
    for kind in ("ip", "security-group"):
        for resource in owned("instance", kind):
            call("instance", kind, "delete", resource["id"])
    Path("BRAIN/gpu_ip.txt").unlink(missing_ok=True)
    Path("BRAIN/gateway.env").unlink(missing_ok=True)
    print("[gpu-down] owned GPU, root disk and IP removed; weights retained")


def capacity_available() -> bool:
    server = server_owned()
    if server is None:
        return False
    return any(
        r["name"] == server["commercial_type"] and r["availability"] == "available"
        for r in call("instance", "server-type", "list")
    )


def action(name: str) -> None:
    require_gpu()
    server = checked_server()
    if server is None:
        raise RuntimeError("No owned GPU server")
    call("instance", "server", name, server["id"], "--wait")


def address_file(server: dict[str, Any]) -> None:
    address = str(
        ipaddress.IPv4Address(
            next(ip["address"] for ip in server["public_ips"] if ip["family"] == "inet")
        )
    )
    Path("BRAIN/gpu_ip.txt").write_text(address + "\n")
    Path("BRAIN/gateway.env").write_text(
        atlas_ports().url("terminal", address) + "/v1\n"
    )


def resume() -> None:
    require_gpu()
    server = checked_server()
    if server is None:
        # Only orphaned resources can exist here; keep failed attempts bounded.
        down()
        up()
        return
    print("[gpu-resume] retaining owned server", file=sys.stderr)
    if server["state"] == "stopped":
        action("start")
        server = checked_server()
        if server is None:
            raise RuntimeError("Owned GPU disappeared during resume")
    address_file(server)
    catalog = call("instance", "server-type", "list")
    entry = next(r for r in catalog if r["name"] == server["commercial_type"])
    price = entry["hourly_price"]["units"] + entry["hourly_price"]["nanos"] / 1e9
    Path("BRAIN/gpu-cost.json").write_text(json.dumps({"hourly_eur": price}))


def up() -> None:
    require_gpu()
    require_env(GPU_REQUIRED_ENV)
    model = os.environ["LOCAL_MODEL"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", model):
        raise ValueError("Invalid LOCAL_MODEL repository identifier")
    client = str(ipaddress.IPv4Address(os.environ["GPU_CLIENT_IP"]))
    if server_owned() or state_path().exists():
        raise RuntimeError("Run gpu-down before starting a fresh verification cycle")
    for service, kind in (
        ("block", "volume"),
        ("instance", "ip"),
        ("instance", "security-group"),
    ):
        owned(service, kind)
    catalog = call("instance", "server-type", "list")
    # The image worker uses CPU offload on L4; BF16 weights are not resident.
    # Multiple affordable families avoid dependence on one type's capacity.
    preferences = [
        "L4-1-24G",
        "L4-2-24G",
        "L40S-1-48G",
        "H100-1-80G",
        "L40S-2-48G",
        "H100-SXM-2-80G",
    ]
    affordable = [
        r
        for t in preferences
        for r in catalog
        if r["name"] == t
        and r["hourly_price"]["units"] + r["hourly_price"]["nanos"] / 1e9
        <= float(os.environ["GPU_MAX_EUR_H"])
    ]
    # Creation confirms capacity; never substitute a pricier GPU.
    chosen = next(
        (r for r in affordable if r["availability"] in ("available", "scarce")),
        next(iter(affordable), None),
    )
    if chosen is None:
        raise RuntimeError("No compatible GPU available")
    price = chosen["hourly_price"]["units"] + chosen["hourly_price"]["nanos"] / 1e9
    if price > float(os.environ["GPU_MAX_EUR_H"]):
        raise RuntimeError("Available GPU exceeds recorded hourly budget")
    Path("BRAIN/gpu-cost.json").write_text(json.dumps({"hourly_eur": price}))
    print(
        f"[gpu-up] {chosen['name']} at {price} EUR/h excluding storage/IP", flush=True
    )
    weight = single(
        [v for v in owned("block", "volume") if "atlas-root" not in v.get("tags", [])]
    )
    fresh = weight is None or "atlas-unformatted" in weight.get("tags", [])
    if weight is None:
        weight = create(
            "block",
            "volume",
            "name=" + gpu_name() + "-weights",
            "tags.2=atlas-unformatted",
            "from-empty.size=200GB",
            "perf-iops=5000",
            "--wait",
        )
    group = create(
        "instance",
        "security-group",
        "name=" + gpu_name(),
        "inbound-default-policy=drop",
    )
    for port in (22, atlas_ports().port("terminal")):
        call(
            "instance",
            "security-group",
            "create-rule",
            "security-group-id=" + group["id"],
            "protocol=TCP",
            "direction=inbound",
            "action=accept",
            "ip-range=" + client + "/32",
            "dest-port-from=" + str(port),
        )
    ip = create("instance", "ip", "type=routed_ipv4")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh") as init:
        init.write(bootstrap(weight["id"], model, fresh))
        init.flush()
        server = create(
            "instance",
            "server",
            "name=" + gpu_name(),
            "type=" + chosen["name"],
            "image="
            + os.environ.get("GPU_IMAGE", "8063548b-7ade-4faa-8be1-f48f8d3ba3ea"),
            "root-volume=sbs:80GB:5000",
            "additional-volumes.0=" + weight["id"],
            "security-group-id=" + group["id"],
            "cloud-init=@" + init.name,
            "ip=" + ip["id"],
            "dynamic-ip-required=false",
            "stopped=true",
            "--wait",
        )
    # The CLI creates the image-backed root. Tag it before starting the server.
    tag_resource("block", "volume", server["volumes"]["0"], "atlas-root")
    action("start")
    address_file(server)
    print(
        f"[gpu-up] instance {server['id']} created; worker readiness pending",
        flush=True,
    )


if __name__ == "__main__":
    for key in ("SCW_DEFAULT_PROJECT_ID", "SCW_DEFAULT_ZONE"):
        if not os.environ.get(key):
            raise RuntimeError(f"Missing {key}")
    Path("BRAIN").mkdir(exist_ok=True)
    # Serialize an owner across worktrees, including demo window and launcher.
    lock_name = (
        "atlas-gpu-"
        + os.environ["SCW_DEFAULT_PROJECT_ID"]
        + "-"
        + os.environ["SCW_DEFAULT_ZONE"]
        + "-"
        + gpu_owner()
        + ".lock"
    )
    with (Path(tempfile.gettempdir()) / lock_name).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        command = sys.argv[1]
        if command not in {"status", "capacity"}:
            require_gpu()
        if command == "up":
            require_env(GPU_REQUIRED_ENV)
            if server_owned() or state_path().exists():
                raise RuntimeError(
                    "Existing owned cycle: use resume or down explicitly"
                )
            up()
        elif command in {"down", "delete"}:
            down()
        elif command in {"stop", "reboot"}:
            action(command)
        elif command == "resume":
            resume()
        elif command == "replace":
            if not capacity_available():
                raise RuntimeError(
                    "Replacement blocked: capacity is not reported available"
                )
            down()
            up()
        elif command == "capacity":
            sys.exit(0 if capacity_available() else 1)
        elif command == "check":
            if checked_server() is None:
                raise RuntimeError("No owned GPU server")
        elif command == "status":
            print(json.dumps(server_owned()))
        else:
            raise RuntimeError("Unknown GPU lifecycle command")
