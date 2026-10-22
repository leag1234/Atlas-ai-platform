"""Lifecycle tests: provider doubles never enter runtime code."""

import contextlib
import importlib.util
import os
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch


def load_gpu() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gpu", "infra/gpu.py")
    assert spec and spec.loader
    gpu = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gpu)
    return gpu


class GPUContract(unittest.TestCase):
    def test_lifecycle(self) -> None:
        gpu = load_gpu()
        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
            Path("BRAIN").mkdir()
            owned = {"id": "server", "tags": ["atlas-m1"], "name": "atlas-gpu"}
            with patch.dict(
                os.environ,
                {"SCW_DEFAULT_PROJECT_ID": "project", "SCW_DEFAULT_ZONE": "fr-par-2"},
            ):
                with patch.object(
                    gpu, "call", return_value=[owned, {"id": "foreign", "tags": []}]
                ):
                    self.assertEqual(gpu.owned("instance", "server"), [])
                    with patch.dict(os.environ, {"ATLAS_GPU_OWNER": "demo"}):
                        self.assertEqual(gpu.owned("instance", "server"), [owned])
                with patch.object(gpu, "call", side_effect=RuntimeError("provider")):
                    self.assertRaises(RuntimeError, gpu.down)
                with patch.object(gpu, "owned", return_value=[owned, owned]):
                    self.assertRaises(RuntimeError, gpu.down)
                with (
                    patch.object(gpu, "owned", return_value=[]),
                    patch.object(gpu, "call") as call,
                ):
                    gpu.down()
                    call.assert_not_called()
            cloud = gpu.bootstrap("weights", "org/model", True)
            self.assertIn("mkfs.ext4", cloud)
            self.assertNotIn("mkfs.ext4", gpu.bootstrap("weights", "org/model", False))
            self.assertIn("enable-prefix-caching", cloud)
            self.assertIn("/root/.cache/huggingface", cloud)
            self.assertIn("scsi-0SCW_sbs_volume-weights", cloud)

    def test_create_and_destroy(self) -> None:
        from typing import Any

        gpu = load_gpu()
        requests: list[tuple[str, ...]] = []
        servers: list[dict[str, Any]] = []
        volumes: list[dict[str, Any]] = []
        ips: list[dict[str, Any]] = []
        groups: list[dict[str, Any]] = []

        def provider(*args: str) -> Any:
            requests.append(args)
            if args[2] == "list":
                return {
                    "server": servers,
                    "volume": volumes,
                    "ip": ips,
                    "security-group": groups,
                    "server-type": [
                        {
                            "name": "L40S-1-48G",
                            "availability": "shortage",
                            "hourly_price": {"units": 1, "nanos": 0},
                        },
                        {
                            "name": "H100-1-80G",
                            "availability": "available",
                            "hourly_price": {"units": 3, "nanos": 0},
                        },
                    ],
                }[args[1]]
            if args[2] == "create":
                self.assertIn("project-id=project", args)
                obj: dict[str, Any] = {
                    "id": args[1],
                    "name": next(
                        (
                            arg.removeprefix("name=")
                            for arg in args
                            if arg.startswith("name=")
                        ),
                        "",
                    ),
                    "tags": [
                        arg.split("=", 1)[1] for arg in args if arg.startswith("tags.")
                    ],
                    "volumes": {"0": {"id": "root"}},
                }
                if args[1] == "server":
                    self.assertIn("additional-volumes.0=volume", args)
                    self.assertIn("type=L40S-1-48G", args)
                    self.assertTrue(any(a.startswith("cloud-init=@") for a in args))
                    root = obj["volumes"]["0"]
                    root["tags"] = list(obj["tags"])
                    volumes.append(root)
                    public_ip = next(ip for ip in ips if ip["id"] == "ip")
                    obj["public_ips"] = [public_ip]
                    obj["security_group"] = {"id": "security-group"}
                    servers.append(obj)
                    return {
                        **obj,
                    }
                if args[1] == "volume":
                    volumes.append(obj)
                elif args[1] == "ip":
                    obj.update({"address": "192.0.2.3", "family": "inet"})
                    ips.append(obj)
                else:
                    groups.append(obj)
                return {"security_group": obj} if args[1] == "security-group" else obj
            if args[2] == "update":
                records = {"volume": volumes, "ip": ips, "security-group": groups}[
                    args[1]
                ]
                record = next(record for record in records if record["id"] == args[3])
                record["tags"] = [
                    arg.split("=", 1)[1] for arg in args if arg.startswith("tags.")
                ]
                return record
            if args[2] == "terminate":
                self.assertIn("with-block=false", args)
                self.assertIn("with-ip=false", args)
                servers.clear()
            if args[2] == "delete":
                records = {"volume": volumes, "ip": ips, "security-group": groups}[
                    args[1]
                ]
                records[:] = [record for record in records if record["id"] != args[3]]
            return None

        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
            Path("BRAIN").mkdir()
            with (
                patch.dict(
                    os.environ,
                    {
                        "SCW_DEFAULT_PROJECT_ID": "project",
                        "SCW_DEFAULT_ZONE": "fr-par-2",
                        "LOCAL_MODEL": "org/model",
                        "GPU_CLIENT_IP": "192.0.2.2",
                        "GPU_MAX_EUR_H": "1.47",
                        "ATLAS_DEV_GPU": "1",
                    },
                ),
                patch.object(gpu, "call", side_effect=provider),
            ):
                gpu.up()
                self.assertEqual(
                    Path("BRAIN/gpu_ip.txt").read_text().strip(), "192.0.2.3"
                )
                self.assertRaises(RuntimeError, gpu.up)
                gpu.down()
                self.assertFalse(Path("BRAIN/gpu_ip.txt").exists())
                self.assertEqual(len(volumes), 1)
                self.assertTrue(
                    any(
                        a[:4] == ("block", "volume", "delete", "root") for a in requests
                    )
                )
                with patch.dict(os.environ, {"GPU_MAX_EUR_H": "0"}):
                    self.assertRaises(RuntimeError, gpu.up)
                with patch.dict(os.environ, {"LOCAL_MODEL": "$(unsafe)"}):
                    self.assertRaises(ValueError, gpu.up)

    def test_provider_boundary(self) -> None:
        import subprocess

        gpu = load_gpu()
        with patch.dict(os.environ, {"SCW_DEFAULT_ZONE": "fr-par-2"}):
            with patch.object(
                gpu.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "[]"),
            ):
                self.assertEqual(gpu.call("instance", "server", "list"), [])
            with patch.object(
                gpu.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1, "sensitive data"),
            ):
                with self.assertRaisesRegex(RuntimeError, "exit 1"):
                    gpu.call("instance", "server", "list")
            with patch.dict(os.environ, {"GPU_SSH_PUBLIC_KEY": "invalid\nkey"}):
                self.assertRaises(
                    ValueError, gpu.bootstrap, "volume", "org/model", True
                )
