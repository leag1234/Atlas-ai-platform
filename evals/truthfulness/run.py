"""Run the frozen public set through serverless public chat, with durable caps."""

import argparse
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from packages.ports import atlas_ports
from evals.truthfulness.score import Case, PROFILES, aggregate, load_cases

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "evals/truthfulness/cases.json"
OUTPUT = ROOT / "BRAIN/truthfulness"
CAP = Decimal("3")
RESERVATION = Decimal("0.10")


def write_json(path: Path, value: Any) -> None:
    pending = path.with_suffix(".tmp")
    with pending.open("w") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    pending.replace(path)


class Ledger:
    """Persist before dispatch; unknown usage keeps its entire reservation."""

    def __init__(self, path: Path):
        self.path = path
        self.rows: list[dict[str, Any]] = (
            json.loads(path.read_text()) if path.exists() else []
        )

    @property
    def spent(self) -> Decimal:
        return sum((Decimal(r["charged_eur"]) for r in self.rows), Decimal(0))

    def reserve(self, case_id: str, profile: str, attempt: int) -> int:
        if self.repeated_failure():
            raise RuntimeError("three_consecutive_identical_failures")
        if self.spent + RESERVATION > CAP:
            raise RuntimeError("ledger_cap_3_EUR")
        prior = [r for r in self.rows if (r["id"], r["profile"]) == (case_id, profile)]
        if len(prior) >= 2 or attempt != len(prior) + 1:
            raise RuntimeError("attempt_cap")
        self.rows.append(
            {
                "id": case_id,
                "profile": profile,
                "attempt": attempt,
                "at": datetime.now(timezone.utc).isoformat(),
                "status": None,
                "charged_eur": str(RESERVATION),
                "cost_kind": "reserved_upper_bound",
            }
        )
        write_json(self.path, self.rows)
        return len(self.rows) - 1

    def repeated_failure(self) -> bool:
        last = self.rows[-3:]
        return (
            len(last) == 3
            and last[0]["status"] not in (None, 200)
            and all(r["status"] == last[0]["status"] for r in last)
        )

    def settle(self, index: int, status: int | None, data: dict[str, Any]) -> None:
        row = self.rows[index]
        row["status"] = status
        try:
            cost = Decimal(str(data.get("atlas", {}).get("cost_eur")))
        except (InvalidOperation, ValueError, TypeError, AttributeError):
            cost = RESERVATION
        if cost.is_finite() and cost >= 0:
            row["charged_eur"] = str(cost)
            row["cost_kind"] = (
                data.get("atlas", {}).get("cost_kind", "reported")
                if cost != RESERVATION
                else "reserved_upper_bound"
            )
        write_json(self.path, self.rows)
        if Decimal(row["charged_eur"]) > RESERVATION or self.spent > CAP:
            raise RuntimeError("reported_cost_exceeds_reservation")


def post(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = Request(
        atlas_ports().url("adapter") + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        response = urlopen(request, timeout=150)
    except HTTPError as exc:
        response = exc
    with response:
        status = response.status
        try:
            value = json.loads(response.read(8 * 1024 * 1024))
        except (ValueError, UnicodeError):
            value = {}
    return status, value if isinstance(value, dict) else {}


def ask(case: Case, profile: str, ledger: Ledger) -> dict[str, Any]:
    if any((r["id"], r["profile"]) == (case.id, profile) for r in ledger.rows):
        raise RuntimeError("already_attempted_in_session")
    started = time.monotonic()
    body = {
        "model": profile,
        "messages": [{"role": "user", "content": case.question}],
        "stream": False,
    }
    result: dict[str, Any] = {
        "id": case.id,
        "profile": profile,
        "answer": "",
        "error": None,
        "attempts": [],
    }
    for attempt in (1, 2):
        index = ledger.reserve(case.id, profile, attempt)
        result["attempts"].append(index)
        try:
            status, data = post(body)
        except (OSError, TimeoutError):
            ledger.settle(index, 0, {})
            result["error"] = "transport_unknown_no_retry"
            break
        ledger.settle(index, status, data)
        result["http_status"] = status
        if status == 200:
            try:
                answer = data["choices"][0]["message"]["content"]
                if not isinstance(answer, str) or not answer.strip():
                    raise ValueError("empty_answer")
                result.update(
                    answer=answer,
                    usage=data.get("usage", {}),
                    atlas=data.get("atlas", {}),
                    completion_id=data.get("id"),
                )
            except (KeyError, IndexError, TypeError, ValueError):
                result["error"] = "invalid_response"
            break
        error = data.get("error", {})
        code = error.get("code", "") if isinstance(error, dict) else ""
        if code in ("quota_exhausted", "search_quota_exhausted", "budget_exceeded"):
            result["error"] = code
            break
        result["error"] = f"http_{status}"
        if ledger.repeated_failure():
            result["error"] = "three_consecutive_identical_failures"
            break
        if attempt == 2 or not (status == 429 or 500 <= status <= 599):
            break
        result["error"] = None
        # Each attempted HTTP request is separately charged and persisted.
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


def busy_ports() -> list[int]:
    output = subprocess.run(
        ["ss", "-ltnH"], check=True, capture_output=True, text=True
    ).stdout
    ports = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            match = re.search(r":(\d+)$", fields[3])
            if match:
                ports.add(int(match[1]))
    offsets = atlas_ports()
    used = {offsets.port(s) for s in ("terminal", "gateway", "retrieval", "adapter")}
    return sorted(ports.intersection(used))


def audit_batch(output: Path, stage: str, provider: str, maximum_searches: int) -> None:
    allowed = True
    for name in ("tavily", "serpapi"):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/search_allowance.py"),
                name,
                "truthfulness-" + stage,
                str(maximum_searches if name == provider else 0),
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        try:
            row = json.loads(result.stdout)
        except ValueError:
            row = {
                "provider": name,
                "stage": stage,
                "allowed": False,
                "error": "audit_invalid_output",
            }
        with (output / "allowance.jsonl").open("a") as log:
            log.write(json.dumps(row) + "\n")
        if name == provider and (
            result.returncode != 0 or row.get("allowed") is not True
        ):
            allowed = False
    if not allowed:
        raise RuntimeError("search_allowance_refused")


@contextmanager
def public_stack(name: str, output: Path) -> Iterator[None]:
    from services.orchestrator.serving import stack
    from services.orchestrator.terminal_stack import terminal

    os.environ.update(
        GPU_LOCAL="0",
        ATLAS_IMAGE_ON_DEMAND="0",
        ATLAS_TERMINAL_ENABLED="1",
        ATLAS_INTERACTION_DIR=str(output / "telemetry"),
    )
    with (
        terminal(
            name + "-terminal", "127.0.0.1:" + str(atlas_ports().port("terminal"))
        ),
        stack(name, False),
    ):
        yield


def run(output: Path, session: str) -> int:
    cases = load_cases(CASES)
    dataset = CASES.read_bytes()
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "mode": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_sha256": hashlib.sha256(dataset).hexdigest(),
        "frozen_cases": json.loads(dataset),
        "answers": [],
        "prompt_hashes": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "prompts").iterdir())
            if p.is_file()
        },
        "profile_concurrency": 1,
        "global_concurrency": 1,
        "cap_eur": str(CAP),
        "session": session,
        "cost_eur": "0",
        "reason": None,
    }
    ledger = Ledger(OUTPUT / ("session-" + session + ".json"))
    ledger_start = len(ledger.rows)

    def save() -> None:
        report["session_charged_eur"] = str(ledger.spent)
        report["attempt_log"] = ledger.rows[ledger_start:]
        report["cost_eur"] = str(
            sum(
                (Decimal(row["charged_eur"]) for row in ledger.rows[ledger_start:]),
                Decimal(0),
            )
        )
        report["metrics"] = aggregate(cases, report["answers"])
        write_json(output / "results.json", report)

    try:
        occupied = busy_ports()
        write_json(output / "ports.json", {"busy_ports": occupied})
        if occupied:
            report["reason"] = "ports_busy:" + ",".join(map(str, occupied))
            return 2
        provider = os.environ.get("ATLAS_SEARCH_PROVIDER", "")
        if provider not in ("tavily", "serpapi"):
            raise RuntimeError("owner_search_provider_required")
        report["search_provider"] = provider
        if any(r["id"] in {c.id for c in cases} for r in ledger.rows):
            raise RuntimeError("already_attempted_in_session")
        from packages.configuration import CHAT_REQUIRED_ENV, require_env

        require_env(CHAT_REQUIRED_ENV)
        report["mode"] = "live"
        save()
        failures = 0
        previous_error = None
        with public_stack("atlas-truthfulness-" + session, output):
            for start in range(0, len(cases), 5):
                batch = cases[start : start + 5]
                try:
                    # Three searches/request, two HTTP attempts, three profiles.
                    audit_batch(
                        output,
                        f"before-{start // 5 + 1}",
                        provider,
                        len(batch) * 3 * 2 * len(PROFILES),
                    )
                    for case in batch:
                        for profile in PROFILES:
                            result = ask(case, profile, ledger)
                            report["answers"].append(result)
                            save()
                            print(
                                case.id,
                                profile,
                                result["error"] or "answered",
                                flush=True,
                            )
                            error = result["error"]
                            if error == "three_consecutive_identical_failures":
                                raise RuntimeError(error)
                            failures = (
                                failures + 1
                                if error and error == previous_error
                                else int(bool(error))
                            )
                            previous_error = error
                            if failures >= 3:
                                raise RuntimeError(
                                    "three_consecutive_identical_failures"
                                )
                finally:
                    audit_batch(output, f"after-{start // 5 + 1}", provider, 0)
        if hashlib.sha256(CASES.read_bytes()).hexdigest() != report["dataset_sha256"]:
            raise RuntimeError("dataset_changed_during_run")
        report["mode"] = "complete"
        return 0
    except (RuntimeError, OSError, ValueError) as exc:
        # Only controlled local error identifiers are persisted, never provider bodies.
        report["mode"] = "stopped"
        report["reason"] = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        if report["reason"] == "three_consecutive_identical_failures":
            with (ROOT / "BRAIN/BLOCKERS.md").open("a") as file:
                file.write(
                    f"\nTask V0 stopped after three identical failures. Evidence: {output / 'results.json'}, {ledger.path}. No further attempt.\n"
                )
        return 1
    finally:
        save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--session",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        help="One owner session; reusing it cannot repeat cases or reset spending.",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.session):
        parser.error("session must contain letters, numbers, underscores or hyphens")
    os.chdir(ROOT)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with (OUTPUT / "runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        output = OUTPUT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        try:
            status = run(output, args.session)
        finally:
            with (output / "gpu-down.log").open("w") as log:
                cleanup = subprocess.run(
                    ["bash", "infra/gpu-down.sh"],
                    cwd=ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
        print("Evidence:", output.relative_to(ROOT), flush=True)
        print(
            "Judge explicitly, after reviewing the run:",
            "python -m evals.truthfulness.judge"
            f" {output.relative_to(ROOT) / 'results.json'}"
            f" --out {output.relative_to(ROOT) / 'judgments.json'}",
            flush=True,
        )
        raise SystemExit(status if cleanup.returncode == 0 else 1)


if __name__ == "__main__":
    main()
