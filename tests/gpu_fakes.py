#!/usr/bin/env python3
"""Executable provider/clock/SSH/HTTP doubles, used only by ownership tests."""

import fcntl
import json
import os
from pathlib import Path
import sys
from typing import Any


def main() -> None:
    path = Path(os.environ["GPU_FAKE_STATE"])
    state = json.loads(path.read_text())
    tool, args = Path(sys.argv[0]).name, sys.argv[1:]
    state.setdefault("calls", []).append([tool, *args])
    result: Any = None
    code = 0
    if tool == "scw":
        service, kind, action = args[:3]
        key = service + "/" + kind
        rows = state.setdefault(key, [])
        fields = dict(arg.split("=", 1) for arg in args[3:] if "=" in arg)
        tags = [value for key, value in fields.items() if key.startswith("tags.")]
        if action == "list":
            result = rows
        elif action == "create":
            record: dict[str, Any] = {
                "id": kind + "-created",
                "name": fields.get("name", ""),
                "tags": tags,
            }
            if kind == "server":
                root = {"id": "root-created", "tags": []}
                state.setdefault("block/volume", []).append(root)
                ips = [ip for ip in state["instance/ip"] if ip["id"] == fields["ip"]]
                record.update(
                    {
                        "volumes": {
                            "0": root,
                            "1": {"id": fields["additional-volumes.0"]},
                        },
                        "public_ips": ips,
                        "security_group": {"id": fields["security-group-id"]},
                        "state": "stopped",
                        "commercial_type": fields["type"],
                    }
                )
            elif kind == "ip":
                record.update({"family": "inet", "address": "192.0.2.10"})
            rows.append(record)
            # Mirror the real CLI: some created objects come back wrapped.
            wrappers = {"security-group": "security_group", "ip": "ip"}
            result = {wrappers[kind]: record} if kind in wrappers else record
        elif action == "update":
            record = next(r for r in rows if r["id"] == args[3])
            record["tags"] = tags
            result = record
        elif action in {"terminate", "delete"}:
            state[key] = [r for r in rows if r["id"] != args[3]]
        elif action in {"start", "stop", "reboot"}:
            record = next(r for r in rows if r["id"] == args[3])
            record["state"] = "stopped" if action == "stop" else "running"
            if action == "reboot":
                state["rebooted"] = True
        elif action != "create-rule":
            raise RuntimeError("Unsupported fake provider operation")
    elif tool == "date":
        result = (
            str(state.get("time", 0)) if args == ["+%s"] else "2026-10-22T00:00:00Z"
        )
    elif tool == "sleep":
        state["time"] = state.get("time", 0) + int(args[0])
    elif tool == "curl":
        state["health_calls"] = state.get("health_calls", 0) + 1
        mode = state.get("health", "lost")
        code = (
            0
            if mode == "healthy"
            or (
                mode == "runtime-loss"
                and (state.get("time", 0) < 60 or state.get("time", 0) >= 160)
            )
            or (mode == "restart" and state.get("restarted"))
            or (mode == "reboot" and state.get("rebooted"))
            or (mode == "transient" and state.get("time", 0) >= 100)
            else 22
        )
    elif tool == "ssh":
        if "docker restart" in args[-1]:
            state["restarted"] = True
        if "-N" in args:
            code = 1
    elif tool not in {"scp", "flag"}:
        raise RuntimeError("Unsupported fake executable")
    path.write_text(json.dumps(state))
    if result is not None:
        print(json.dumps(result) if tool == "scw" else result)
    sys.exit(code)


if __name__ == "__main__":
    with Path(os.environ["GPU_FAKE_STATE"] + ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        main()
