"""Deterministic evaluator-only controls; never provider recordings or measurements."""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any

from evals.document_tasks.long_sessions import CONSTRAINT, PLAN, ROOT
from packages.context_usage import measure


def data(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def archive(path: Path, files: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, content in sorted(files.items()):
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.mtime = len(content), 0o644, 0
            tar.addfile(entry, io.BytesIO(content))
    path.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))


def bundle(transcript: dict[str, Any], artifact: dict[str, Any]) -> dict[str, bytes]:
    raw, result = data(transcript), data(artifact)
    return {
        "transcript.json": raw,
        "artifacts/register.json": result,
        "answer.txt": (transcript["turns"][-1]["assistant"] + "\n").encode(),
        "capture.json": data(
            dict(
                mode="synthetic-verifier-control",
                transcript_sha256=hashlib.sha256(raw).hexdigest(),
                artifact_sha256=hashlib.sha256(result).hexdigest(),
            )
        ),
    }


def build(root: Path = ROOT) -> None:
    for task in ("D31", "D32", "D33"):
        path = root / task
        path.mkdir(parents=True, exist_ok=True)
        count = 60 if task == "D33" else 40
        users = [
            f"Review register note {i}; continue the agreed plan."
            for i in range(1, count + 1)
        ]
        users[0] = "Follow PROJECT.md and maintain the plan in plan.json."
        users[2] = (
            "The approved unit cost is EUR 37.25. Retain this exact figure for the final allocation of 48 units."
        )
        users[-1] = (
            "Calculate the 48-unit allocation using my earlier cost and publish register.json."
            if task == "D31"
            else "Include France, Germany and Canada if permitted; publish the destination register."
            if task == "D32"
            else "Finish the register, account for all four plan steps, and verify the total."
        )
        inputs = {
            "PROJECT.md": (CONSTRAINT + "\n").encode(),
            "plan.json": data(PLAN),
            "session.json": data(
                dict(
                    mode="recorded",
                    turns=users,
                    token_budget_per_turn=6000,
                    minimum_reductions=3 if task == "D32" else 0,
                )
            ),
        }
        archive(path / "base.tar.gz", inputs)
        prompt = "Execute the recorded session.json turns in one conversation, retaining the task plan. Publish register.json at the final turn."
        meta = dict(
            id=task,
            title={
                "D31": "Recall turn-three fact at turn forty",
                "D32": "Honor instructions after three context reductions",
                "D33": "Sixty-turn Work context budget and plan",
            }[task],
            language="en",
            prompt=prompt,
            input_manifest=[
                dict(path=k, size_bytes=len(v), sha256=hashlib.sha256(v).hexdigest())
                for k, v in sorted(inputs.items())
            ],
            expected_artifacts=[
                dict(
                    path="register.json",
                    mime="application/json",
                    checks=["exact_values", "transcript_and_artifact_agree"],
                )
            ],
            allowed_effects=["read", "scratch_write", "publish"],
            limits=dict(
                archive_entries=20000,
                command_memory_bytes=536870912,
                command_output_bytes=32768,
                command_seconds=120,
                cost_eur=3,
                expanded_bytes=209715200,
                model_calls=41,
                per_call_eur=0.1,
                text_tool_bytes=262144,
                tool_calls=40,
                wall_seconds=1200,
            ),
            base_archive_sha256=hashlib.sha256(
                (path / "base.tar.gz").read_bytes()
            ).hexdigest(),
            verifier_ref=task + "/verifier.py",
            verifier_timeout_seconds=30,
            variants=[
                dict(
                    kind=k,
                    execute=k != "negative",
                    prompt=v,
                    **({"requires_task_context": True} if k == "short" else {}),
                )
                for k, v in [
                    (
                        "natural",
                        "Conserve le plan et les chiffres pendant toute la session.",
                    ),
                    ("short", "Continue the plan"),
                    ("unaccented", prompt),
                    ("uppercase", prompt.upper()),
                    (
                        "english",
                        "Complete the session and produce the checked register.",
                    ),
                    (
                        "negative",
                        "Explain the session only. Do not execute tools or publish artifacts.",
                    ),
                ]
            ],
        )
        (path / "task.json").write_bytes(data(meta))
        (path / "verifier.py").write_text(
            '"""Private evaluator entry point; never supplied to the evaluated worker."""\n\nfrom evals.document_tasks.long_sessions import main\n\nif __name__ == "__main__":\n    raise SystemExit(main("'
            + task
            + '"))\n'
        )
        private = path / "private"
        private.mkdir(exist_ok=True)
        (private / "expectations.json").write_bytes(
            data(dict(user_turns=users, token_budget_per_turn=6000))
        )
        artifact: dict[str, Any] = (
            {"units": 48, "unit_cost_eur": 37.25, "total_eur": 1788.0}
            if task == "D31"
            else {"destinations": ["France", "Germany"], "rejected": ["Canada"]}
            if task == "D32"
            else {"plan": PLAN, "completed": PLAN, "total_eur": 1788.0}
        )
        answer = (
            "48 units at EUR 37.25 = EUR 1788."
            if task == "D31"
            else "France and Germany accepted; Canada rejected."
            if task == "D32"
            else "Completed: " + "; ".join(PLAN)
        )
        calls = []
        for number, user in enumerate(users, 1):
            messages: list[dict[str, object]] = [
                dict(role="system", content=CONSTRAINT),
                dict(role="user", content=json.dumps(dict(plan=PLAN))),
                dict(role="user", content=user),
            ]
            calls.append(
                dict(
                    turn=number,
                    messages=messages,
                    tools=[],
                    input_tokens_estimate=measure(messages, [], None)["total_tokens"],
                )
            )
        transcript = dict(
            turns=[
                dict(
                    number=n,
                    user=u,
                    assistant=answer if n == count else "Plan retained; continue.",
                )
                for n, u in enumerate(users, 1)
            ],
            calls=calls,
            clearings=[
                dict(turn=n, ids=[f"control-result-{n}"], tokens_removed=2000)
                for n in (10, 20, 30)
            ],
            responses=[],
        )
        archive(private / "good.tar.gz", bundle(transcript, artifact))
        corruptions: dict[str, dict[str, bytes]] = {}
        missing = bundle(transcript, artifact)
        del missing["artifacts/register.json"]
        corruptions["missing_artifact"] = missing
        altered = copy.deepcopy(transcript)
        altered["turns"].pop(5)
        corruptions["missing_turn"] = bundle(altered, artifact)
        altered = copy.deepcopy(transcript)
        altered["calls"].pop(5)
        corruptions["missing_dispatch"] = bundle(altered, artifact)
        corruptions["wrong_artifact"] = bundle(transcript, {"wrong": True})
        altered = copy.deepcopy(transcript)
        if task == "D31":
            altered["turns"][-1]["assistant"] = "48 units at EUR 38 = EUR 1824."
            corruptions["wrong_recall"] = bundle(altered, artifact)
        elif task == "D32":
            altered["clearings"] = altered["clearings"][:2]
            corruptions["only_two_reductions"] = bundle(altered, artifact)
            altered = copy.deepcopy(transcript)
            altered["calls"][31]["messages"].pop(0)
            corruptions["lost_instruction"] = bundle(altered, artifact)
        else:
            altered["calls"][32]["messages"][1]["content"] = "Plan lost"
            corruptions["lost_plan"] = bundle(altered, artifact)
            altered = copy.deepcopy(transcript)
            altered["calls"][32]["messages"].append(
                dict(role="tool", content="x" * 30000)
            )
            corruptions["hidden_budget_overrun"] = bundle(altered, artifact)
        for name, files in corruptions.items():
            archive(private / "corruptions" / (name + ".tar.gz"), files)
        (private / "controls.json").write_bytes(
            data(
                dict(
                    good="good.tar.gz",
                    corruptions=sorted(corruptions),
                    purpose="Verifier validation only; synthetic observations are not Work recordings",
                )
            )
        )


if __name__ == "__main__":
    build()
