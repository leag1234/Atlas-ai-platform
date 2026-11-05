"""Phase-0 code benchmark runner.

For each task: copy the public repository snapshot into a throwaway git working copy, give the statement to
the configured agent, then copy the result into a fresh directory, add the hidden check and run it in a
container without network. Records pass/fail, wall time, cost, steps and false success claims.

Usage:
  python3 evals/code/run.py --config oracle|null|codex-atlas|codex-atlas-high|atlas-work \
      [--tasks 01-...,02-...] [--out results.json] [--budget-eur 10]

The agent never sees `hidden/` or `solution/`: Codex runs in a container that only mounts the working copy.
Secrets are read from files and passed to containers through the environment; they are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
TASKS = ROOT / "tasks"
IMAGE = os.environ.get("BENCH_IMAGE", "atlas-code-bench:local")
CHECK_TIMEOUT = 300
LOG_ROOT = Path(os.environ.get("BENCH_LOG_DIR", "/opt/atlas-src/bench-logs"))
ATLAS_API = os.environ.get("ATLAS_API_BASE", "https://atlas.eu.com/api/atlas/v1")
KEY_FILE = os.environ.get("ATLAS_WORKER_KEY_FILE", "/root/atlas-keys/atlas-workers-flash.key")
CODEX_TEMPLATE = Path(os.environ.get("CODEX_TEMPLATE_HOME", "/root/codex-atlas-workers"))
CODEX_JS = Path("/usr/lib/node_modules/@openai/codex")
NODE = Path("/usr/bin/node")

# Provider prices (EUR per million tokens) of the first route of each Codex profile, from
# services/model-gateway/pricing.yaml and api-routing.json. This estimate is the per-task cost of B; the
# shared worker key usage delta is only an upper bound (other agents spend on the same key).
PRICES = {
    "atlas": {"model": "atlas-code effort none (zai-glm-5-3-flash)", "input": 0.1275, "cached": 0.0255, "output": 0.425},
    "atlas-high": {"model": "atlas-code effort high (zai-glm-5-3)", "input": 1.19, "cached": 0.119, "output": 3.74},
}

PREAMBLE = """You are working on a software task in the repository at {where}.
There is no human in the loop: do not ask questions, make reasonable decisions and finish the task.
Use Python 3.12 and its standard library only; do not install packages. Hidden automated checks will verify
your work after you finish; you cannot see them, so verify your own work (run the tests or the program).
When you are done, end your final message with exactly one line: `RESULT: SUCCESS` if you completed the task
and verified it, or `RESULT: FAILURE` if you could not complete it.

TASK:
"""


def load_tasks(selected: list[str] | None) -> list[dict[str, Any]]:
    tasks = []
    for meta in sorted(TASKS.glob("*/task.json")):
        data = json.loads(meta.read_text())
        if selected and data["id"] not in selected:
            continue
        data["dir"] = meta.parent
        data["statement"] = (meta.parent / "statement.md").read_text()
        tasks.append(data)
    if selected and len(tasks) != len(selected):
        raise SystemExit(f"unknown task in {selected}")
    return tasks


def sh(*args: str, timeout: float | None = None, **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, **kwargs)


def prepare(task: dict[str, Any], tmp: Path) -> Path:
    work = tmp / "work"
    shutil.copytree(task["dir"] / "repo", work)
    env = dict(os.environ, GIT_AUTHOR_NAME="bench", GIT_AUTHOR_EMAIL="bench@example.invalid",
               GIT_COMMITTER_NAME="bench", GIT_COMMITTER_EMAIL="bench@example.invalid")
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "task snapshot"]):
        subprocess.run(cmd, cwd=work, check=True, env=env, capture_output=True)
    return work


def run_container(work: Path, hidden: Path | None, command: str, timeout: int, network: bool) -> dict[str, Any]:
    name = "atlas-bench-check-" + os.urandom(4).hex()
    args = ["docker", "run", "--rm", "--name", name, "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{work}:/work", "-w", "/work"]
    if not network:
        args += ["--network", "none"]
    if hidden is not None:
        args += ["-v", f"{hidden}:/check:ro"]
    args += [IMAGE, "sh", "-c", command]
    try:
        done = sh(*args, timeout=timeout)
        out, code = (done.stdout + done.stderr), done.returncode
    except subprocess.TimeoutExpired:
        sh("docker", "kill", name)
        out, code = "check timed out", 124
    return {"code": code, "output": out[-3000:]}


def check(task: dict[str, Any], work: Path, tmp: Path) -> dict[str, Any]:
    target = tmp / "check"
    shutil.copytree(work, target, ignore=shutil.ignore_patterns(".git", "__pycache__"), symlinks=True)
    result = run_container(target, task["dir"] / "hidden", "sh /check/check.sh", CHECK_TIMEOUT, network=False)
    return {"passed": result["code"] == 0, "check_exit": result["code"], "check_tail": result["output"][-1200:]}


def claim_of(text: str) -> str:
    found = re.findall(r"RESULT:\s*(SUCCESS|FAILURE)", text or "")
    if found:
        return found[-1].lower()
    if re.search(r"\b(done|fixed|implemented|completed|all tests pass|tests pass|passes)\b", text or "", re.I):
        return "success_implied"
    return "none"


def atlas_usage(key: str) -> int | None:
    request = urllib.request.Request(ATLAS_API + "/usage", headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
        return int(data["charged_micro_eur"]) + int(data.get("unknown_micro_eur") or 0)
    except Exception:
        return None


# ---------------------------------------------------------------- agents

def agent_null(task, work, tmp, prompt, log_dir):
    return {"final": "RESULT: FAILURE"}


def agent_oracle(task, work, tmp, prompt, log_dir):
    solution = task["dir"] / "solution"
    for path in solution.rglob("*"):
        if path.is_file() and path.name != ".delete":
            dest = work / path.relative_to(solution)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
    deletions = solution / ".delete"
    if deletions.exists():
        for line in deletions.read_text().split():
            (work / line).unlink()
    return {"final": "Applied the reference solution.\nRESULT: SUCCESS"}


def codex_agent(profile: str):
    def run(task, work, tmp, prompt, log_dir):
        home = tmp / "codex-home"
        home.mkdir()
        for name in ("config.toml", "atlas.config.toml", "atlas-high.config.toml"):
            text = (CODEX_TEMPLATE / name).read_text()
            # Drop host trust entries; the benchmark trusts only its own working copy.
            text = re.sub(r'\n\[projects\."[^"]*"\]\ntrust_level = "trusted"\n', "\n", text)
            if name == "config.toml":
                text += '\n[projects."/workspace"]\ntrust_level = "trusted"\n'
            (home / name).write_text(text)
        name = "atlas-bench-agent-" + os.urandom(4).hex()
        env = dict(os.environ, ATLAS_API_KEY=Path(KEY_FILE).read_text().strip())
        args = ["docker", "run", "--rm", "--name", name, "-e", "ATLAS_API_KEY", "-e", "CODEX_HOME=/codex",
                "-e", "HOME=/codex", "-v", f"{work}:/workspace", "-v", f"{home}:/codex",
                "-v", f"{NODE}:/usr/local/bin/node:ro", "-v", f"{CODEX_JS}:/opt/codex:ro", "-w", "/workspace",
                IMAGE, "node", "/opt/codex/bin/codex.js", "exec", "-p", profile,
                "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "--json",
                "--output-last-message", "/codex/last.txt", prompt]
        events_path = log_dir / "events.jsonl"
        timed_out = False
        with events_path.open("w") as events, open(log_dir / "stderr.txt", "w") as err:
            proc = subprocess.Popen(args, stdout=events, stderr=err, stdin=subprocess.DEVNULL, env=env)
            try:
                proc.wait(timeout=task["time_budget_s"])
            except subprocess.TimeoutExpired:
                timed_out = True
                sh("docker", "kill", name)
                proc.wait(timeout=60)
        del env
        final = (home / "last.txt").read_text() if (home / "last.txt").exists() else ""
        stats = codex_stats(events_path)
        rollout = rollout_usage(home)
        if rollout and rollout["input_tokens"] >= stats["input_tokens"]:
            # The rollout records cumulative usage after every model call, including a run killed at the
            # time budget (turn.completed is then missing from the JSON events).
            stats.update(rollout)
        price = PRICES[profile]
        cached = stats["cached_input_tokens"]
        stats["cost_estimate_eur"] = round(
            ((stats["input_tokens"] - cached) * price["input"] + cached * price["cached"]
             + stats["output_tokens"] * price["output"]) / 1e6, 4)
        stats.update(final=final, timed_out=timed_out, agent_exit=proc.returncode)
        return stats

    return run


def rollout_usage(home: Path) -> dict[str, int] | None:
    last = None
    for path in sorted(home.glob("sessions/**/*.jsonl")):
        for line in path.read_text(errors="replace").splitlines():
            if '"token_count"' in line:
                try:
                    info = json.loads(line)["payload"]["info"]
                except (ValueError, KeyError, TypeError):
                    continue
                if info and info.get("total_token_usage"):
                    last = info["total_token_usage"]
    if last is None:
        return None
    return {k: int(last.get(k) or 0) for k in ("input_tokens", "cached_input_tokens", "output_tokens")}


def codex_stats(path: Path) -> dict[str, Any]:
    stats = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "turns": 0,
             "commands": 0, "file_changes": 0, "other_tools": 0, "messages": 0, "errors": []}
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "turn.completed":
            usage = event.get("usage") or {}
            stats["turns"] += 1
            for k in ("input_tokens", "cached_input_tokens", "output_tokens"):
                stats[k] += int(usage.get(k) or 0)
        elif kind == "item.completed":
            item = (event.get("item") or {}).get("type", "")
            if item == "command_execution":
                stats["commands"] += 1
            elif item == "file_change":
                stats["file_changes"] += 1
            elif item == "agent_message":
                stats["messages"] += 1
            elif item not in ("reasoning",):
                stats["other_tools"] += 1
        elif kind in ("error", "turn.failed"):
            stats["errors"].append(str(event.get("message") or event.get("error"))[:300])
    stats["steps"] = stats["commands"] + stats["file_changes"] + stats["other_tools"]
    return stats


AGENTS = {"null": agent_null, "oracle": agent_oracle,
          "codex-atlas": codex_agent("atlas"), "codex-atlas-high": codex_agent("atlas-high")}


def atlas_work_agent():
    from atlas_work import run_work  # local module, adapter for the Work mode of the guest instance

    return run_work


# ---------------------------------------------------------------- main

def run_task(task, config, agent, key, log_root):
    tmp = Path(tempfile.mkdtemp(prefix="atlas-bench-"))
    log_dir = log_root / task["id"]
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        work = prepare(task, tmp)
        where = "/workspace" if config.startswith("codex") else "of this project"
        prompt = PREAMBLE.format(where=where) + task["statement"]
        before = atlas_usage(key) if key else None
        started = time.monotonic()
        try:
            outcome = agent(task, work, tmp, prompt, log_dir)
        except Exception as error:  # harness or transport failure: recorded, the check still runs
            outcome = {"final": "", "agent_error": f"{type(error).__name__}: {str(error)[:300]}"}
        wall = round(time.monotonic() - started, 1)
        after = atlas_usage(key) if key else None
        diff = sh("git", "diff", "HEAD", "--stat", cwd=work).stdout
        (log_dir / "diff.patch").write_text(sh("git", "-c", "core.quotepath=off", "diff", "HEAD", cwd=work).stdout
                                            + "\n# untracked\n" + sh("git", "status", "--short", cwd=work).stdout)
        result = check(task, work, tmp)
        claim = claim_of(outcome.get("final", ""))
        record = {
            "id": task["id"], "category": task["category"], "config": config,
            "time_budget_s": task["time_budget_s"], "wall_s": wall, **result,
            "claim": claim, "false_claim": claim in ("success", "success_implied") and not result["passed"],
            # Shared worker key: other agents may spend on it at the same time, so this is an upper bound.
            "key_usage_delta_eur": None if before is None or after is None else round((after - before) / 1e6, 4),
            "diff_stat": diff.strip().splitlines()[-1] if diff.strip() else "no change",
        }
        for k, v in outcome.items():
            if k != "final":
                record[k] = v
        # Cost of this task: token estimate for Codex runs, Work session usage for Atlas Work runs.
        record["cost_eur"] = record.get("charged_eur", record.get("cost_estimate_eur"))
        record["final_message"] = outcome.get("final", "")[-1500:]
        (log_dir / "record.json").write_text(json.dumps(record, indent=2))
        return record
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(records)
    passed = sum(r["passed"] for r in records)
    walls = [r["wall_s"] for r in records]
    costs = [r["cost_eur"] for r in records if r.get("cost_eur") is not None]
    return {
        "tasks": n, "passed": passed, "pass_rate": round(passed / n, 3) if n else 0,
        "median_wall_s": statistics.median(walls) if walls else None,
        "cost_eur_total": round(sum(costs), 4) if costs else None,
        "median_cost_eur": round(statistics.median(costs), 4) if costs else None,
        "false_claims": sum(r["false_claim"] for r in records),
        "timeouts": sum(bool(r.get("timed_out")) for r in records),
        "median_steps": statistics.median([r["steps"] for r in records if "steps" in r]) if any("steps" in r for r in records) else None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, choices=[*AGENTS, "atlas-work"])
    parser.add_argument("--tasks")
    parser.add_argument("--out")
    parser.add_argument("--budget-eur", type=float, default=10.0)
    args = parser.parse_args()
    tasks = load_tasks(args.tasks.split(",") if args.tasks else None)
    agent = atlas_work_agent() if args.config == "atlas-work" else AGENTS[args.config]
    paid = args.config.startswith(("codex", "atlas-work"))
    key = Path(KEY_FILE).read_text().strip() if paid and args.config.startswith("codex") else None
    stamp = time.strftime("%Y%m%dT%H%M%S")
    log_root = LOG_ROOT / f"{stamp}-{args.config}"
    records, spent = [], 0.0
    for task in tasks:
        if paid and spent >= args.budget_eur:
            print(f"budget reached ({spent:.2f} EUR), stopping before {task['id']}", flush=True)
            break
        record = run_task(task, args.config, agent, key, log_root)
        if record.get("agent_error") and not record.get("model_calls") and not record.get("turns"):
            # A failure of the harness before any model call is not an attempt: run the task once more.
            print(f"{task['id']}: harness error before any model call ({record['agent_error'][:80]}), retrying", flush=True)
            record = run_task(task, args.config, agent, key, log_root)
        spent += record.get("cost_eur") or 0.0
        records.append(record)
        print(f"{record['id']:<36} {'PASS' if record['passed'] else 'FAIL'}  {record['wall_s']:>7}s  "
              f"claim={record['claim']:<15} cost={record.get('cost_eur')}  steps={record.get('steps')}", flush=True)
    if args.out and Path(args.out).exists():
        previous = json.loads(Path(args.out).read_text())
        if previous.get("config") == args.config:
            done = {r["id"] for r in records}
            records = sorted([r for r in previous["records"] if r["id"] not in done] + records, key=lambda r: r["id"])
    pricing = None
    if args.config.startswith("codex"):
        pricing = PRICES["atlas-high" if args.config.endswith("high") else "atlas"]
    document = {"config": args.config, "started": stamp, "image": IMAGE, "pricing": pricing,
                "summary": summarize(records), "records": records}
    if args.config == "atlas-work":
        document["model"] = os.environ.get("ATLAS_WORK_MODEL", "atlas-glm")
    if args.out:
        Path(args.out).write_text(json.dumps(document, indent=2) + "\n")
    print(json.dumps(document["summary"]))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
