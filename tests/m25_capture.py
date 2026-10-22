"""Capture real M25 public HTTP exchanges; never synthesise provider responses."""

from contextlib import aclosing
import copy
import gzip
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from gateway_cpu import CPUModels
from http_gateway import serve
from provider_recording import capture_stream, capture_tool, replay_stream, ExactHistory
from services.orchestrator.chat_api import app
from services.orchestrator.tools import Runtime
import stream_transport


def main() -> None:
    archive = Path(os.environ.get("M25_ARCHIVE", "BRAIN/m25-capture.json.gz"))
    replay = bool(os.environ.get("M25_REPLAY"))
    saved = (
        json.loads(gzip.decompress(archive.read_bytes()))
        if archive.exists() and (replay or os.environ.get("M25_CASE") == "J52")
        else {}
    )
    history = ExactHistory(saved.get("rows", []))
    rows: list[dict[str, Any]] = [] if replay else saved.get("rows", [])
    answers: dict[str, Any] = {} if replay else saved.get("answers", {})
    original = stream_transport.attempt

    def record(kind: str, request: Any, response: Any) -> None:
        rows.append(copy.deepcopy(dict(kind=kind, request=request, response=response)))
        raw = json.dumps(
            {"rows": rows, "answers": answers}, ensure_ascii=False
        ).encode()
        assert not any(
            value.encode() in raw
            for key, value in os.environ.items()
            if len(value) >= 8
            and any(word in key for word in ("KEY", "SECRET", "TOKEN", "PASSWORD"))
        ), "secret_in_recording"
        if not replay:
            archive.write_bytes(gzip.compress(raw, mtime=0))

    async def stream(request: Any, model: str, timeout: float) -> Any:
        value = {**request.model_dump(exclude={"timeout"}), "provider_model": model}
        if replay:
            recorded = history.take("stream", value)
            assert recorded is not None, "unrecorded_m25_stream"
            async with aclosing(replay_stream(recorded)) as events:
                async for event in events:
                    yield event
            return
        async with aclosing(
            capture_stream(
                original(request, model, timeout),
                lambda events: record("stream", value, events),
            )
        ) as events:
            async for event in events:
                yield event

    def wrap(name: str) -> Any:
        method = getattr(Runtime, name)

        async def call(self: Runtime, request: Any, timeout: float) -> Any:
            if replay:
                recorded = history.take(name, request.model_dump())
                assert recorded is not None, "unrecorded_m25_tool"
                return recorded
            return await capture_tool(
                method(self, request, timeout),
                lambda result: record(name, request.model_dump(), result),
            )

        return patch.object(Runtime, name, call)

    with (
        tempfile.TemporaryDirectory() as directory,
        patch.dict(
            os.environ,
            {
                "GPU_LOCAL": "0",
                "ATLAS_SEARCH_PROVIDER": "tavily",
                "ATLAS_TERMINAL_ENABLED": "0",
                "ATLAS_INTERACTION_DIR": directory + "/logs",
                "ATLAS_WEB_CACHE": directory + "/cache.sqlite",
            },
        ),
    ):
        gateway = serve(CPUModels(), 0)
        thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        thread.start()
        try:
            with (
                patch.dict(
                    os.environ,
                    {"ATLAS_GATEWAY_URL": f"http://127.0.0.1:{gateway.server_port}"},
                ),
                patch.object(stream_transport, "attempt", stream),
                wrap("search"),
                wrap("fetch"),
                TestClient(app) as client,
            ):
                requests = {
                    "J52": {
                        "messages": [
                            {
                                "role": "user",
                                "content": "Archive: "
                                + "The workshop opens at nine. " * 1500,
                            },
                            {
                                "role": "assistant",
                                "content": "The opening time is nine.",
                            },
                            {
                                "role": "user",
                                "content": "What opening time did I give? Reply in English.",
                            },
                        ]
                    },
                    "J51": {
                        "model": os.environ.get("M25_PROFILE", "atlas-qwen"),
                        "messages": [
                            {
                                "role": "user",
                                "content": os.environ.get(
                                    "M25_DESIGN_QUESTION",
                                    "Conçois une monnaie locale pour une ville de 50 000 habitants.",
                                ),
                            }
                        ],
                    },
                }
                for name, payload in requests.items():
                    if os.environ.get("M25_CASE") and name != os.environ["M25_CASE"]:
                        continue
                    response = client.post("/v1/chat/completions", json=payload)
                    answers[name] = {
                        "request": payload,
                        "status": response.status_code,
                        "response": response.json(),
                    }
                    record("public_http", payload, answers[name])
                    if replay:
                        Path("BRAIN/m25-replay.json").write_text(
                            json.dumps(answers, ensure_ascii=False, indent=2)
                        )
                    print(name, response.status_code, flush=True)
                    if response.status_code != 200:
                        print(response.text[:1000], flush=True)
        finally:
            gateway.shutdown()
            gateway.server_close()
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
