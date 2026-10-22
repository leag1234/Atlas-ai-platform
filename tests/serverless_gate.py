"""M8: synthetic live capability probes and exact replay, never interaction logs."""

import asyncio
import base64
import gzip
import json
import os
from pathlib import Path
import struct
import time
from typing import Any, cast
from unittest.mock import patch
from urllib.request import Request, urlopen
import zlib

from agent_provider import AgentProvider, AgentRequest
from eval_provider import deadline
from serverless import ServerlessPolicy
from serverless_support import environment

ROOT = Path("tests/cassettes/serverless.json.gz")
TOOL: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "calculator",
        "description": "Evaluate arithmetic.",
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
    },
}


def image_url() -> str:
    def chunk(kind: bytes, value: bytes) -> bytes:
        return (
            struct.pack("!I", len(value))
            + kind
            + value
            + struct.pack("!I", zlib.crc32(kind + value))
        )

    raw = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 256, 256, 8, 2, 0, 0, 0))
        + chunk(
            b"IDAT",
            zlib.compress(
                b"".join(
                    b"\x00"
                    + b"".join(
                        b"\xff\x00\x00"
                        if 48 <= x < 208 and 48 <= y < 208
                        else b"\xff\xff\xff"
                        for x in range(256)
                    )
                    for y in range(256)
                )
            ),
        )
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(raw).decode()


async def main() -> None:
    record = os.environ.get("ATLAS_M8_MODE", "replay") == "record"
    archive: dict[str, Any] = (
        {"calls": [], "raw": []}
        if record
        else json.loads(gzip.decompress(ROOT.read_bytes()))
    )
    report: dict[str, Any] = {"mode": "record" if record else "replay", "calls": []}
    original = AgentProvider._complete
    started = time.monotonic()
    reserved = 0.0
    served_models: list[str] = []

    def save() -> None:
        if record:
            ROOT.parent.mkdir(exist_ok=True)
            ROOT.write_bytes(
                gzip.compress(json.dumps(archive, ensure_ascii=False).encode(), mtime=0)
            )

    async def transport(
        self: AgentProvider,
        request: AgentRequest,
        endpoint: str,
        model: str,
        key: str,
        timeout: float,
    ) -> dict[str, object]:
        served_models.append(model)
        signature = {
            "messages": request.messages,
            "tools": request.tools,
            "model": model,
        }
        if record:
            result = await original(self, request, endpoint, model, key, timeout)
            archive["calls"].append(
                {
                    "request": signature,
                    "response": json.loads(json.dumps(result)),
                    "time": time.time(),
                }
            )
            save()
            return result
        matches = [r for r in archive["calls"] if r["request"] == signature]
        assert len(matches) == 1, "unrecorded_request"
        return cast(dict[str, object], json.loads(json.dumps(matches[0]["response"])))

    def raw(path: str, body: dict[str, Any]) -> str:
        nonlocal reserved
        policy = ServerlessPolicy()
        bound = float(
            policy.cost(
                body["model"],
                len(json.dumps(body).encode()) + 256,
                int(body.get("max_tokens", 0)),
            )
        )
        assert bound <= 0.05 and reserved + bound <= 0.30
        reserved += bound
        if record:
            req = Request(
                os.environ["SCW_GENERATIVE_BASE_URL"].rstrip("/") + path,
                data=json.dumps(body).encode(),
                headers={
                    "Authorization": "Bearer " + os.environ["SCW_GENERATIVE_API_KEY"],
                    "Content-Type": "application/json",
                },
            )
            with deadline(60), urlopen(req, timeout=60) as response:
                result = response.read(800001)
                assert len(result) <= 800000
            archive["raw"].append(
                {
                    "path": path,
                    "request": body,
                    "response": result.decode(),
                    "time": time.time(),
                }
            )
            save()
            return str(result.decode())
        matches = [
            r for r in archive["raw"] if r["path"] == path and r["request"] == body
        ]
        assert len(matches) == 1, "unrecorded_raw_request"
        return str(matches[0]["response"])

    with (
        patch.dict(os.environ, {} if record else environment()),
        patch.object(AgentProvider, "_complete", transport),
    ):
        policy = ServerlessPolicy()
        report["capabilities"] = {
            role: policy.config[role]["capabilities"][model]
            for role, model in policy.models.items()
        }
        provider = AgentProvider()
        cases: dict[str, object] = {
            "text": "Réponds en une courte phrase : pourquoi le ciel paraît-il bleu ?",
            "code": "Write a Python function add(a, b) returning their sum. Output only code.",
            "function": "Use the calculator tool to calculate 19 * 23.",
            "vision": [
                {
                    "type": "text",
                    "text": "What color is the square in the center of this image? Answer in English.",
                },
                {"type": "image_url", "image_url": {"url": image_url()}},
            ],
        }
        for label, content in cases.items():
            messages: list[dict[str, object]] = [{"role": "user", "content": content}]
            plan = policy.reserve(messages, [TOOL])
            reserved += float(plan.reserved_eur)
            assert reserved <= 0.30
            answer = cast(
                dict[str, Any],
                await provider.complete(
                    {
                        "messages": messages,
                        "tools": [TOOL],
                        "timeout": 60.0,
                        "local_enabled": False,
                        "observe": True,
                    }
                ),
            )
            assert served_models[-1] == plan.primary
            report["calls"].append(
                {
                    "case": label,
                    "observation": answer["observation"],
                    "usage": answer["usage"],
                    "cost_eur": str(
                        policy.cost(
                            plan.primary,
                            answer["usage"]["prompt_tokens"],
                            answer["usage"]["completion_tokens"],
                        )
                    ),
                }
            )
            assert answer["observation"]["task_type"] == (
                "text" if label == "function" else label
            )
            assert not answer["observation"]["fallback"], (
                "primary capability not proven"
            )
            if label == "function":
                assert (
                    len(answer["calls"]) == 1
                    and answer["calls"][0]["name"] == "calculator"
                )
                assert (
                    json.loads(answer["calls"][0]["arguments"])["expression"].replace(
                        " ", ""
                    )
                    == "19*23"
                )
                report["function_calling_ok"] = True
            elif label == "vision":
                assert "red" in answer["text"].lower()
                report["vision_ok"] = True
            else:
                assert answer["text"].strip()
                if label == "code":
                    assert "return" in answer["text"] and "+" in answer["text"]
                report[label + "_task_served"] = True
        payload: dict[str, Any] = {
            "messages": [
                {
                    "role": "user",
                    "content": "Write a Python function multiply(a, b) returning their product.",
                }
            ],
            "tools": [TOOL],
            "timeout": 60.0,
            "local_enabled": False,
            "observe": True,
        }
        reserved += float(policy.reserve(payload["messages"], [TOOL]).reserved_eur)
        attempts: list[str] = []

        async def fault(
            request: AgentRequest, endpoint: str, model: str, key: str, timeout: float
        ) -> dict[str, object]:
            attempts.append(model)
            if len(attempts) == 1:
                raise RuntimeError("injected_primary_failure")
            return await transport(provider, request, endpoint, model, key, timeout)

        with patch.object(provider, "_complete", side_effect=fault):
            answer = cast(dict[str, Any], await provider.complete(payload))
        assert attempts == [policy.models["code"], policy.models["text"]]
        assert answer["observation"]["fallback"] and answer["text"].strip()
        report["fallback_ok"] = True
        report["calls"].append(
            {
                "case": "fallback",
                "usage": answer["usage"],
                "cost_eur": str(
                    policy.cost(
                        policy.models["text"],
                        answer["usage"]["prompt_tokens"],
                        answer["usage"]["completion_tokens"],
                    )
                ),
            }
        )
        stream = raw(
            "/chat/completions",
            {
                "model": policy.models["text"],
                "messages": [
                    {
                        "role": "user",
                        "content": "Compte de un à dix, un nombre par ligne, sans commentaire.",
                    }
                ],
                "max_tokens": 256,
                "temperature": 0,
                "reasoning_effort": "none",
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
        events = [line[6:] for line in stream.splitlines() if line.startswith("data: ")]
        assert events[-1] == "[DONE]"
        decoded = [json.loads(e) for e in events[:-1]]
        pieces = [
            c["delta"].get("content", "") for e in decoded for c in e.get("choices", [])
        ]
        assert sum(bool(p) for p in pieces) > 1
        usage = next(e["usage"] for e in reversed(decoded) if e.get("usage"))
        report["streaming_ok"] = True
        report["calls"].append(
            {
                "case": "stream",
                "usage": usage,
                "cost_eur": str(
                    policy.cost(
                        policy.models["text"],
                        usage["prompt_tokens"],
                        usage["completion_tokens"],
                    )
                ),
            }
        )
        embedding = json.loads(
            raw(
                "/embeddings",
                {
                    "model": policy.models["embedding"],
                    "input": ["A synthetic red square."],
                    "dimensions": 1024,
                },
            )
        )
        vector = embedding["data"][0]["embedding"]
        assert len(vector) == 1024 and any(v != 0 for v in vector)
        report["embeddings_ok"] = True
        report["calls"].append(
            {
                "case": "embedding",
                "usage": embedding["usage"],
                "cost_eur": str(
                    policy.cost(
                        policy.models["embedding"],
                        embedding["usage"]["prompt_tokens"],
                        0,
                    )
                ),
            }
        )
        report["elapsed_seconds"] = time.monotonic() - started
        report["cost_eur"] = sum(float(r["cost_eur"]) for r in report["calls"])
        report["reserved_eur"] = reserved
        assert (
            report["elapsed_seconds"] < 120 and report["cost_eur"] <= reserved <= 0.30
        )
        path = Path("BRAIN/eval/serverless.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report))


if __name__ == "__main__":
    asyncio.run(main())
