"""Refresh M24 recordings through real public HTTP, tools and downloaded files."""

from contextlib import aclosing
from dataclasses import asdict
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from gateway_cpu import CPUModels
from http_gateway import serve
from m24_artifacts import validate
from m24_capture import capture
from provider_recording import capture_stream
from services.orchestrator.chat_api import app
from services.orchestrator.chat_pipeline import ChatTools
from services.orchestrator.loop import Call, Message
from services.orchestrator.model import GatewayModel
from services.orchestrator.terminal_client import TerminalClient
import stream_transport


def main() -> None:
    archive = Path("tests/cassettes/m24.json.gz")
    cases = json.loads(gzip.decompress(archive.read_bytes()))
    rows: list[dict[str, Any]] = []
    original_stream = stream_transport.attempt
    original_post = GatewayModel.post
    original_tool = ChatTools.execute
    original_upload = TerminalClient.upload_generated
    original_publish = TerminalClient.publish

    def record(request: Any, response: Any) -> None:
        rows.append(copy.deepcopy({"request": request, "response": response}))
        encoded = json.dumps(rows, ensure_ascii=False).encode()
        assert not any(
            value.encode() in encoded
            for key, value in os.environ.items()
            if len(value) >= 8
            and any(word in key for word in ("KEY", "SECRET", "TOKEN", "PASSWORD"))
        ), "secret_in_recording"
        Path("/tmp/m24-session-streams.json.gz").write_bytes(
            gzip.compress(encoded, mtime=0)
        )

    async def stream(request: Any, model: str, timeout: float) -> Any:
        value = {
            "kind": "stream",
            **request.model_dump(exclude={"timeout"}),
            "provider_model": model,
        }
        async with aclosing(
            capture_stream(
                original_stream(request, model, timeout),
                lambda events: record(value, events),
            )
        ) as events:
            async for event in events:
                yield event

    async def post(url: str, payload: Message, timeout: float) -> Message:
        response = await original_post(url, payload, timeout)
        if url.endswith("/agent/complete"):
            record(
                {
                    "kind": "complete",
                    "request": {
                        key: value for key, value in payload.items() if key != "timeout"
                    },
                },
                response,
            )
        return response

    async def upload_generated(
        client: TerminalClient, filename: str, content: bytes, timeout: float
    ) -> str:
        path = await original_upload(client, filename, content, timeout)
        record(
            {
                "kind": "terminal_upload",
                "filename": filename,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            },
            {"path": path},
        )
        return path

    async def publish(
        client: TerminalClient, path: str, strategy: str, timeout: float
    ) -> dict[str, object]:
        output = await original_publish(client, path, strategy, timeout)
        record(
            {"kind": "terminal_publish", "path": path, "strategy": strategy},
            {
                "output": output,
                "commands": client.commands,
                "failed_commands": client.failed_commands,
                "files": client.files,
                "strategies": client.strategies,
            },
        )
        return output

    async def execute(runtime: ChatTools, call: Call, timeout: float) -> Message:
        output = await original_tool(runtime, call, timeout)
        terminal = runtime.terminal
        assert terminal is not None
        record(
            {
                "kind": "tool",
                "call": asdict(call),
                "request_id": runtime.item.request_id,
            },
            {
                "output": output,
                "commands": terminal.commands,
                "failed_commands": terminal.failed_commands,
                "files": terminal.files,
                "strategies": terminal.strategies,
                "documents": runtime.item.documents,
                "errors": runtime.item.erreurs,
            },
        )
        return output

    with (
        tempfile.TemporaryDirectory() as root,
        patch.dict(
            os.environ,
            {
                "GPU_LOCAL": "0",
                "ATLAS_TERMINAL_ENABLED": "1",
                "ATLAS_SEARCH_PROVIDER": "tavily",
                "ATLAS_IMAGE_ON_DEMAND": "0",
                "ATLAS_INTERACTION_DIR": root + "/logs",
                "ATLAS_WEB_CACHE": root + "/cache.sqlite",
            },
        ),
    ):
        gateway = serve(CPUModels(), 0)
        worker = threading.Thread(target=gateway.serve_forever, daemon=True)
        worker.start()
        try:
            with (
                patch.dict(
                    os.environ,
                    {"ATLAS_GATEWAY_URL": f"http://127.0.0.1:{gateway.server_port}"},
                ),
                patch.object(stream_transport, "attempt", stream),
                patch.object(GatewayModel, "post", staticmethod(post)),
                patch.object(ChatTools, "execute", execute),
                patch.object(TerminalClient, "upload_generated", upload_generated),
                patch.object(TerminalClient, "publish", publish),
                TestClient(app) as client,
            ):
                for name, case in cases.items():
                    if os.environ.get("M24_CASE") and name not in os.environ[
                        "M24_CASE"
                    ].split(","):
                        continue
                    rows.clear()
                    started = time.monotonic()
                    response = client.post("/v1/chat/completions", json=case["request"])
                    print(name, response.status_code, flush=True)
                    assert response.status_code == 200, (name, response.json())
                    answer = response.json()
                    answer["seconds"] = time.monotonic() - started
                    request_path, response_path = (
                        Path(root) / "request.json",
                        Path(root) / "response.json",
                    )
                    request_path.write_text(json.dumps(case["request"]))
                    response_path.write_text(json.dumps(answer))
                    capture(name, request_path, response_path, rows)
                    updated = json.loads(gzip.decompress(archive.read_bytes()))[name]
                    validate(name, updated, answer)
                    print(name, "useful artifacts validated", flush=True)
        finally:
            gateway.shutdown()
            gateway.server_close()
            worker.join(timeout=10)


if __name__ == "__main__":
    main()
