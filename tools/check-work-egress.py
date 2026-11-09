#!/usr/bin/env python3
"""Staging-only end-to-end check for filtered Work registry egress."""

from __future__ import annotations

import argparse
import asyncio
import os
from uuid import uuid4

from services.orchestrator.code_workspace import Workspace


async def run(workspace: Workspace, command: str) -> tuple[bool, str]:
    result = await workspace.invoke(
        "run_command", dict(command=command, timeout_seconds=120, memory_mb=512), 150
    )
    return int(result.get("exit_code", 1)) == 0, str(result.get("output", ""))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--org", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--generation", type=int, default=1)
    args = parser.parse_args()
    if os.environ.get("ATLAS_WORK_EGRESS") != "registries":
        raise SystemExit("ATLAS_WORK_EGRESS must be set to registries")
    if os.environ.get("ATLAS_PROJECT_MODE") != "1":
        raise SystemExit("ATLAS_PROJECT_MODE must be set to 1")
    workspace = Workspace(
        str(uuid4()),
        org=args.org,
        project=args.project,
        workspace=args.workspace,
        generation=args.generation,
    )
    await workspace.prepare()
    try:
        checks = (
            ("pip", "python3 -m pip install --user requests==2.32.3"),
            ("npm", "npm install --prefix /workspace/.atlas-home/npm left-pad"),
            ("https denial", "curl -fsS --max-time 20 https://example.com"),
            ("metadata denial", "curl -fsS --max-time 20 http://169.254.169.254"),
        )
        failed = False
        for label, command in checks:
            passed, output = await run(workspace, command)
            expected_denial = label.endswith("denial")
            success = passed if expected_denial is False else not passed
            print(f"{label}: {'PASS' if success else 'FAIL'}")
            if not success:
                failed = True
                print(output[-1000:])
        return 1 if failed else 0
    finally:
        await workspace.cleanup()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
