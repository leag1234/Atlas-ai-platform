"""Bounded live HTTP code/tool roundtrip and complete 24 KiB context, no replay."""

import json
import os
from pathlib import Path
import tempfile
from time import monotonic
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from devapi_context import context_case, context_answer
from services.orchestrator.dev_auth import Store, provision
from services.orchestrator.devapi import app


def main() -> None:
    if not os.environ.get("SCW_GENERATIVE_API_KEY"):
        raise SystemExit("SCW_GENERATIVE_API_KEY required for live validation")
    report = Path("BRAIN/eval/devapi-live.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    proof: dict[str, Any] = {"mode": "live_provider_http", "complete": False}
    started = monotonic()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store = Store(root / "usage.sqlite")
        for dev, count in [("tool", 2), ("context", 1)]:
            provision(store, dev, root / (dev + ".key"), count, 50000)
        try:
            with (
                patch.dict(os.environ, {"ATLAS_DEVAPI_DB": str(store.path)}),
                TestClient(app) as client,
            ):

                def send(dev: str, payload: dict[str, Any]) -> dict[str, Any]:
                    response = client.post(
                        "/v1/chat/completions",
                        json=payload,
                        headers={
                            "Authorization": "Bearer "
                            + (root / (dev + ".key")).read_text().strip()
                        },
                    )
                    assert response.status_code == 200, response.status_code
                    value = response.json()
                    assert isinstance(value, dict)
                    return value

                payload: dict[str, Any] = {
                    "model": "atlas-code",
                    "max_tokens": 512,
                    "messages": [
                        {
                            "role": "user",
                            "content": Path("tests/journeys/dev-tool.txt").read_text(),
                        }
                    ],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "save_value",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"value": {"type": "integer"}},
                                    "required": ["value"],
                                    "additionalProperties": False,
                                },
                            },
                        }
                    ],
                    "tool_choice": {
                        "type": "function",
                        "function": {"name": "save_value"},
                    },
                    "parallel_tool_calls": False,
                }
                first = send("tool", payload)["choices"][0]
                assert first["finish_reason"] == "tool_calls"
                call = first["message"]["tool_calls"][0]
                assert call["function"]["name"] == "save_value"
                assert json.loads(call["function"]["arguments"]) == {"value": 7}
                payload["messages"] += [
                    first["message"],
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": '{"saved":7}',
                    },
                ]
                payload["tool_choice"] = "none"
                second = send("tool", payload)["choices"][0]
                assert (
                    second["finish_reason"] == "stop" and second["message"]["content"]
                )
                proof["tool_roundtrip"] = True
                context, expected, size = context_case()
                answer = send("context", context)["choices"][0]
                assert answer["finish_reason"] == "stop"
                assert context_answer(answer["message"]["content"]) == expected
                proof.update(large_context=True, code_bytes=size, complete=True)
        finally:
            proof["usage"] = {dev: store.usage(dev) for dev in ("tool", "context")}
            proof["duration_seconds"] = round(monotonic() - started, 3)
            report.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(proof, ensure_ascii=False))


if __name__ == "__main__":
    main()
