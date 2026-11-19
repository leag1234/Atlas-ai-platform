"""Model calls through the Atlas public API, with a cost ledger and a hard budget.

Prices are those of services/model-gateway/pricing.yaml (EUR per million tokens).
The direct-model responses of the public API carry token usage but no cost, so
the cost is computed here from usage and the gateway price table.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import httpx

API = os.environ.get("LONGFORM_API", "https://staging.atlas.eu.com/api/atlas/v1")
KEY_FILE = os.environ.get("LONGFORM_KEY_FILE", "/root/atlas-keys/staging/defect-repro.key")
ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "ledger.jsonl"
TOTAL_BUDGET_EUR = float(os.environ.get("LONGFORM_TOTAL_BUDGET_EUR", "8"))

# v6 model roles, chosen by measurement (out/v6_writers.json, 2026-11-19): Mistral Large 4 plans and derives
# (reasoning helps, latency 1-9 min with 504s); GLM 5.3 writes and repairs (no reasoning tokens, 2-52 s, fewest
# check issues of the fast candidates); GLM 5.3 Flash extracts.
STRONG = "mistral/mistral-large-4"
WRITER = "mistral/zai-glm-5-3"
FAST = "mistral/zai-glm-5-3-flash"
# A failed call (5xx, 429, timeout) on a slow model is retried on this one.
FALLBACK = {STRONG: WRITER, WRITER: FAST}

# EUR per million tokens: input, cached input, output (pricing.yaml, 2026-11-19).
PRICES = {
    "mistral-large-4": (0.595, 0.0595, 1.785),
    "zai-glm-5-3-flash": (0.1275, 0.0255, 0.425),
    "zai-glm-5-3": (1.19, 0.119, 3.74),
    "deepseek-v4-flash-0731": (0.40, 0.40, 0.80),
}

_lock = threading.Lock()


class BudgetExceeded(RuntimeError):
    pass


def _key() -> str:
    return Path(KEY_FILE).read_text().strip()


def ledger_total() -> float:
    if not LEDGER.exists():
        return 0.0
    total = 0.0
    for line in LEDGER.read_text().splitlines():
        try:
            total += float(json.loads(line).get("eur", 0))
        except (ValueError, json.JSONDecodeError):
            continue
    return total


def record(entry: dict) -> None:
    with _lock:
        with LEDGER.open("a") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def cost(model: str, usage: dict) -> float:
    name = model.split("/", 1)[-1]
    price_in, price_cached, price_out = PRICES.get(name, (2.0, 2.0, 6.0))
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    return (
        (prompt - cached) * price_in + cached * price_cached + completion * price_out
    ) / 1e6


class Client:
    """One client per run: keeps the run's trace of calls and its cost."""

    def __init__(self, case: str, run_budget_eur: float):
        self.case = case
        self.run_budget = run_budget_eur
        self.spent = 0.0
        self.calls: list[dict] = []
        self.http = httpx.Client(timeout=httpx.Timeout(300.0, connect=15.0))
        self.key = _key()

    def chat(
        self,
        step: str,
        messages: list[dict],
        model: str = STRONG,
        max_tokens: int = 2000,
        temperature: float = 0.3,
        timeout: float | None = None,
        reasoning: str | None = None,
    ) -> str:
        if ledger_total() >= TOTAL_BUDGET_EUR:
            raise BudgetExceeded(f"total ledger reached {TOTAL_BUDGET_EUR} EUR")
        if self.spent >= self.run_budget:
            raise BudgetExceeded(f"run budget reached {self.run_budget} EUR")
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if reasoning:
            body["reasoning_effort"] = reasoning
        if timeout is None:
            timeout = 240.0 if model == STRONG else 150.0
        last_error = ""
        for attempt in range(4):
            if attempt >= 1 and last_error and model in FALLBACK:
                # After one failure the call moves to the faster model instead of waiting again.
                model = FALLBACK[model]
                body["model"] = model
                body.pop("reasoning_effort", None)
            started = time.monotonic()
            try:
                response = self.http.post(
                    API + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.key},
                    json=body,
                    timeout=timeout,
                )
            except httpx.HTTPError as error:
                last_error = type(error).__name__
                self.calls.append({"step": step, "model": model, "failed": last_error, "seconds": round(time.monotonic() - started, 1), "eur": 0})
                time.sleep(5)
                continue
            elapsed = time.monotonic() - started
            if response.status_code >= 500 or response.status_code == 429:
                last_error = f"http_{response.status_code}"
                self.calls.append({"step": step, "model": model, "failed": last_error, "seconds": round(elapsed, 1), "eur": 0})
                time.sleep((3, 10, 20, 30)[attempt])
                continue
            data = response.json()
            if response.status_code != 200:
                raise RuntimeError(f"model call failed: {response.status_code} {str(data)[:300]}")
            usage = data.get("usage") or {}
            eur = data.get("atlas", {}).get("cost_eur") or cost(model, usage)
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "case": self.case,
                "step": step,
                "model": model,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "seconds": round(elapsed, 2),
                "eur": round(eur, 6),
                "attempt": attempt + 1,
            }
            self.calls.append(entry)
            self.spent += eur
            choice = data["choices"][0]
            entry["finish_reason"] = choice.get("finish_reason")
            entry["reasoning_tokens"] = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            record(entry)
            content = choice["message"].get("content") or ""
            if not content.strip() and choice.get("finish_reason") == "length":
                raise RuntimeError(f"{step}: max_tokens spent on reasoning, empty answer")
            return content
        raise RuntimeError(f"model call failed after retry: {last_error}")

    def json(self, step: str, messages: list[dict], **kwargs) -> dict | list:
        """A call whose answer must be JSON; one re-ask on a parse failure."""
        text = self.chat(step, messages, **kwargs)
        try:
            return parse_json(text)
        except ValueError:
            retry = messages + [
                {"role": "assistant", "content": text[:4000]},
                {
                    "role": "user",
                    "content": "That was not valid JSON. Reply with the JSON only, no prose, no code fence.",
                },
            ]
            return parse_json(self.chat(step + ":json_retry", retry, **kwargs))


def parse_json(text: str) -> dict | list:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        text = fenced.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start >= 0 and end > start:
            candidate = text[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON in model answer")
