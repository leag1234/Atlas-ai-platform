"""Run the unsourced-details evaluation set through the real chat pipeline.

Live check, not a unit test (it calls real models through an instance's model
gateway and searches through its SearXNG): kept out of CI like the other live
checks under tools/. Each question is one fresh conversation handled in-process
by ``chat_pipeline.process`` (router, tools, specialized sources, grounding),
with the instance's non-secret settings (infra/instances/common.env and the
overlay) and private paths redirected to the output folder, so nothing is
written to the instance's data. Paid search fallbacks are not used (SearXNG
only).

    PYTHONPATH="services/model-gateway:services/edge-bff:." \\
    python tools/unsourced_details/run.py --profile atlas-qwen --out /tmp/ud/qwen-before \\
        [--instance staging] [--set ATLAS_UNSOURCED_DETAILS=revise] [--ids co-01,pe-02] [--parallel 3]

Writes OUT/records.jsonl: per question the answer, the texts every source tool
returned in the turn (the evidence the annotation reads), the grounding
evidence when the turn was checked, cost and time. The output holds model
answers and page texts: keep it outside the repository.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Tools whose results are sources of the answer (the ones grounding collects).
SOURCE_TOOLS = {
    "web_search",
    "web_fetch",
    "encyclopedia",
    "news_headlines",
    "eu_statistics",
    "french_commune",
}


def instance_env(instance: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in ("common.env", f"{instance}.env"):
        for line in (ROOT / "infra/instances" / name).read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, raw = line.split("=", 1)
                parts = shlex.split(raw)
                values[key.strip()] = parts[0] if parts else ""
    return values


def configure(instance: str, out: Path, overrides: list[str]) -> None:
    values = instance_env(instance)
    base = int(values.get("ATLAS_PORT_BASE", "0"))
    private = out / "instance"
    private.mkdir(parents=True, exist_ok=True)
    values.update(
        {
            "ATLAS_GATEWAY_URL": f"http://127.0.0.1:{8010 + base}",
            "ATLAS_SEARCH_PROVIDER": "searxng",
            "ATLAS_DATA_DIR": str(private),
            "ATLAS_DATA_ROOT": str(private / "project-data"),
            "ATLAS_INTERACTION_DIR": str(private / "logs"),
            "ATLAS_IMAGE_DIR": str(private / "images"),
            "ATLAS_PROJECT_DB": str(private / "projects.sqlite"),
            "ATLAS_API_KEYS_DB": str(private / "api-keys.sqlite"),
            "ATLAS_KILL_SWITCH_FILE": str(private / "KILL"),
            "ATLAS_TERMINAL_ENABLED": "0",
        }
    )
    # A shared search cache keeps the results of a question identical between two
    # runs (before / after) when the model sends the same queries.
    values.setdefault("ATLAS_WEB_CACHE", str(out.parent / "web-cache.sqlite"))
    values["ATLAS_WEB_CACHE"] = os.environ.get(
        "UD_WEB_CACHE", str(out.parent / "web-cache.sqlite")
    )
    for item in overrides:
        key, value = item.split("=", 1)
        values[key] = value
    for key in [k for k in os.environ if k.startswith("ATLAS_")]:
        if key not in values:
            del os.environ[key]
    os.environ.update(values)


def texts(value: Any) -> list[str]:
    """Every string of a tool result, decoded (titles, snippets, page texts, facts)."""
    found: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if item.strip():
                found.append(item)
        elif isinstance(item, dict):
            stack.extend(v for k, v in item.items() if k not in {"link", "url"})
        elif isinstance(item, list):
            stack.extend(item)
    return found


COLLECTOR: Any = None


def install_hooks() -> None:
    """Record, per conversation, the source tools' texts and the grounding inputs."""
    from contextvars import ContextVar

    from services.orchestrator import grounding, unsourced_details
    from services.orchestrator.chat_pipeline import ChatTools

    global COLLECTOR
    COLLECTOR = ContextVar("ud_collector", default=None)
    original_execute = ChatTools.execute
    original_check = grounding.Grounding.check

    async def execute(self: Any, call: Any, timeout: float) -> Any:
        message = await original_execute(self, call, timeout)
        sink = COLLECTOR.get()
        if sink is not None and call.name in SOURCE_TOOLS:
            if not (isinstance(message, dict) and message.get("error")):
                sink["tools"].append(
                    {
                        "tool": call.name,
                        "arguments": call.arguments,
                        "texts": texts(message),
                    }
                )
        return message

    def check(self: Any, answer: str, question: str, language: str) -> str:
        sink = COLLECTOR.get()
        result = original_check(self, answer, question, language)
        if sink is not None:
            sink["grounding"] = {
                "draft": answer,
                "evidence": list(self.evidence),
                "metadata": self.metadata(),
            }
        return result

    original_review = unsourced_details.review

    async def review(*args: Any, **kwargs: Any) -> Any:
        answer, report = await original_review(*args, **kwargs)
        sink = COLLECTOR.get()
        if sink is not None:
            sink["unsourced"] = {**report.metadata(), "removed": report.removed}
            sink["tools"].append(
                {
                    "tool": "reference_check",
                    "arguments": "",
                    "texts": report.reference_texts,
                }
            )
        return answer, report

    setattr(unsourced_details, "review", review)
    setattr(ChatTools, "execute", execute)
    setattr(grounding.Grounding, "check", check)


async def ask(case: dict[str, Any], profile: str, privacy: str) -> dict[str, Any]:
    from packages import privacy as privacy_level
    from services.orchestrator.chat_pipeline import process
    from services.orchestrator.chat_schema import ChatRequest
    from services.orchestrator.interactions import Interaction
    from services.orchestrator.stream_client import sink_context

    collected: dict[str, Any] = {"tools": [], "grounding": {}}

    async def sink(event: dict[str, Any]) -> None:
        return None

    item = Interaction()
    started = time.monotonic()
    token = sink_context.set(sink)
    level = privacy_level._level.set(cast(privacy_level.Level, privacy))  # noqa: SLF001
    collecting = COLLECTOR.set(collected)
    error = ""
    try:
        request = ChatRequest.model_validate(
            {
                "model": profile,
                "messages": [{"role": "user", "content": case["question"]}],
                "ui_locale": case["lang"],
                "lang": case["lang"],
                "atlas_ui": True,
                "stream": True,
            }
        )
        await process(request, item)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        COLLECTOR.reset(collecting)
        privacy_level._level.reset(level)  # noqa: SLF001
        sink_context.reset(token)
    return {
        "id": case["id"],
        "lang": case["lang"],
        "question": case["question"],
        "profile": profile,
        "settings": {
            k: v for k, v in os.environ.items() if k.startswith("ATLAS_UNSOURCED")
        },
        "answer": item.reponse,
        "state": item.state,
        "task_type": item.task_type,
        "errors": list(item.erreurs),
        "error": error,
        "cost_eur": float(item.cout_eur or 0),
        "seconds": round(time.monotonic() - started, 2),
        "provider_model": item.provider_model,
        "retrieved_urls": list(dict.fromkeys(item.retrieved_urls)),
        "tools": collected["tools"],
        "grounding": collected["grounding"],
        "unsourced": collected.get("unsourced"),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--instance", default="staging")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--ids", default="")
    parser.add_argument("--questions", type=Path, default=HERE / "questions.jsonl")
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument(
        "--gap",
        type=float,
        default=0.0,
        help="seconds to wait after each question (spares the shared search engines)",
    )
    parser.add_argument("--privacy", default="standard")
    parser.add_argument(
        "--budget", type=float, default=2.0, help="stop above this spend (EUR)"
    )
    args = parser.parse_args()
    if ROOT in args.out.resolve().parents:
        raise SystemExit(
            "the output holds answers and page texts: write it outside the repository"
        )
    args.out.mkdir(parents=True, exist_ok=True)
    configure(args.instance, args.out, args.set)
    os.chdir(ROOT)
    install_hooks()
    cases = [
        json.loads(line)
        for line in args.questions.read_text().splitlines()
        if line.strip()
    ]
    wanted = {i for i in args.ids.split(",") if i}
    cases = [c for c in cases if not wanted or c["id"] in wanted]
    done = set()
    records = args.out / "records.jsonl"
    if records.exists():
        done = {
            json.loads(line)["id"]
            for line in records.read_text().splitlines()
            if line.strip()
        }
    spent = (
        sum(
            json.loads(line)["cost_eur"]
            for line in records.read_text().splitlines()
            if line.strip()
        )
        if records.exists()
        else 0.0
    )
    gate = asyncio.Semaphore(args.parallel)
    lock = asyncio.Lock()

    async def one(case: dict[str, Any]) -> None:
        nonlocal spent
        async with gate:
            if spent > args.budget:
                print(
                    f"skip {case['id']}: budget reached ({spent:.3f} EUR)", flush=True
                )
                return
            record = await ask(case, args.profile, args.privacy)
            async with lock:
                spent += record["cost_eur"]
                with records.open("a") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(
                f"{case['id']} {record['seconds']:6.1f}s {record['cost_eur']:.4f} EUR "
                f"{record['state']} {record['error'][:80]}",
                flush=True,
            )
            await asyncio.sleep(args.gap)

    await asyncio.gather(*(one(c) for c in cases if c["id"] not in done))
    print(f"total spent {spent:.4f} EUR", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
