"""Run isolated Code mode tasks against a command or callable agent."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
Agent = Callable[[str, Path], str]


@dataclass(frozen=True)
class TaskSpec:
    identifier: str
    prompt: str
    verifier_command: str
    timeout_seconds: int
    archive: Path
    verifier: Path


def load_tasks(root: Path = ROOT) -> list[TaskSpec]:
    tasks: list[TaskSpec] = []
    for metadata_path in sorted(root.glob("*/task.json")):
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
        identifier = data["id"]
        tasks.append(
            TaskSpec(
                identifier=identifier,
                prompt=data["prompt"],
                verifier_command=data["verifier_command"],
                timeout_seconds=data["timeout_seconds"],
                archive=metadata_path.parent / "base.tar.gz",
                verifier=root / "verifiers" / identifier,
            )
        )
    return tasks


def null_agent(prompt: str, workspace: Path) -> str:
    """Reference agent which intentionally leaves the workspace untouched."""
    del prompt, workspace
    return "I did not make a change."


def oracle_agent(prompt: str, workspace: Path) -> str:
    """Reference agent which applies the fixture's private known-good solution."""
    del prompt
    shutil.copyfile(workspace / ".oracle_solution.py", workspace / "solution.py")
    return "The issue is fixed and the verifier should pass."


def command_agent(command: Sequence[str]) -> Agent:
    def run(prompt: str, workspace: Path) -> str:
        completed = subprocess.run(
            [*command, prompt],
            cwd=workspace,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        return (completed.stdout + completed.stderr).strip()

    return run


def _unpack(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError("task_archive_path_escape")
        source.extractall(destination, filter="data")


def run_task(
    task: TaskSpec, agent: Agent, python: str = sys.executable
) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix=f"atlas-code-{task.identifier}-"
    ) as temporary:
        workspace = Path(temporary)
        _unpack(task.archive, workspace)
        # The oracle is a harness-only reference, not an evaluated agent.  Its
        # private source is supplied immediately before its call and never to a
        # command/callable selected by an evaluator.
        if agent is oracle_agent:
            shutil.copyfile(
                task.verifier / "oracle_solution.py", workspace / ".oracle_solution.py"
            )
        try:
            claim = agent(task.prompt, workspace)
        except (
            Exception
        ) as error:  # Agent failures are evaluation results, not harness failures.
            claim = f"agent error: {type(error).__name__}: {error}"
        shutil.copytree(task.verifier, workspace, dirs_exist_ok=True)
        oracle = workspace / "oracle_solution.py"
        if oracle.exists():
            oracle.rename(workspace / ".oracle_solution.py")
        command = task.verifier_command.format(python=python)
        completed = subprocess.run(
            command,
            cwd=workspace,
            shell=True,
            text=True,
            capture_output=True,
            timeout=task.timeout_seconds,
            check=False,
        )
        output = completed.stdout + completed.stderr
        solved = completed.returncode == 0
        claimed_success = any(
            word in claim.lower() for word in ("pass", "fixed", "success")
        )
        return {
            "id": task.identifier,
            "solved": solved,
            "duration_seconds": round(time.monotonic() - started, 4),
            "agent_final_claim": claim,
            "verifier_exit_code": completed.returncode,
            "verifier_output": output,
            "false_success_claim": claimed_success and not solved,
        }


def run_all(
    agent: Agent, root: Path = ROOT, python: str = sys.executable
) -> dict[str, Any]:
    results = [run_task(task, agent, python) for task in load_tasks(root)]
    solved = sum(result["solved"] for result in results)
    false_claims = sum(result["false_success_claim"] for result in results)
    return {
        "task_count": len(results),
        "solved_count": solved,
        "solved_rate": solved / len(results) if results else 0.0,
        "false_claim_count": false_claims,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=("null", "oracle"), default="null")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    agent = oracle_agent if args.agent == "oracle" else null_agent
    report = run_all(agent)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
