"""M12 real adapter/gateway HTTP; only the provider is recorded or replayed."""

import base64
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from gateway_cpu import CPUModels
from http_gateway import serve
import quality
from agent_provider import AgentProvider, AgentRequest
from serverless import ServerlessPolicy
from services.orchestrator.chat_api import app

ROOT = Path("tests/cassettes/vision")
RECORDING = ROOT / "transport.json"
QUESTIONS = [
    "Describe the shapes in this image, their colors and relative positions. Use one line per shape.",
    "What color is the circle, and is it to the left or right of the square?",
]


def main() -> None:
    record = os.environ.get("ATLAS_M12_MODE", "replay") == "record"
    archive: list[dict[str, Any]] = [] if record else json.loads(RECORDING.read_text())
    original = quality.complete
    count = 0

    async def transport(self: AgentProvider, request: AgentRequest) -> object:
        nonlocal count
        count += 1
        body = request.model_dump(exclude={"timeout"})
        if record:
            response = await original(self, request)
            archive.append({"request": body, "response": response})
            RECORDING.write_text(json.dumps(archive, ensure_ascii=False) + "\n")
            return response
        rows = [r for r in archive if r["request"] == body]
        if len(rows) != 1:
            raise RuntimeError("unrecorded_vision_request")
        return rows[0]["response"]

    image = (ROOT / "shapes.png").read_bytes()
    encoded = base64.b64encode(image).decode()
    server = serve(Mock(spec=CPUModels), 0)  # CPU backend never used by vision.
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    report: dict[str, object] = {
        "described": False,
        "mode": "record" if record else "replay",
    }
    try:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "ATLAS_GATEWAY_URL": f"http://127.0.0.1:{server.server_port}",
                    "ATLAS_INTERACTION_DIR": directory,
                    # The standalone gateway needs the same configured role as
                    # the launcher, even when CI has no provider environment.
                    "ESCALATION_MODEL": os.environ.get(
                        "ESCALATION_MODEL", ServerlessPolicy().models["text"]
                    ),
                },
            ),
            patch.object(quality, "complete", transport),
            TestClient(app) as client,
        ):
            answers = []
            for question in QUESTIONS:
                value = {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": question},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/png;base64," + encoded
                                    },
                                },
                            ],
                        }
                    ]
                }
                response = client.post("/v1/chat/completions", json=value)
                assert response.status_code == 200, response.text
                answers.append(
                    response.json()["choices"][0]["message"]["content"].lower()
                )
            for color, shape, side in (
                ("red", "square", "left"),
                ("blue", "circle", "right"),
            ):
                assert any(
                    color in line and shape in line and side in line
                    for line in re.split(r"[\n.;]", answers[0])
                )
            assert (
                "blue" in answers[1]
                and "right" in answers[1]
                and "left" not in answers[1]
            )
            rows = [
                json.loads(line)
                for p in Path(directory).glob("*.jsonl")
                for line in p.read_text().splitlines()
            ]
            assert len(rows) == count == 2
            assert encoded not in json.dumps(rows)
            for row in rows:
                assert row["state"] == "done" and row["task_type"] == "vision"
                assert row["modele_utilise"] == "escalade"
                assert (
                    row["images"][0]["width"] == 512
                    and row["images"][0]["height"] == 256
                )
                assert 0 < row["cout_eur"] <= 0.05 and row["tokens"]["in"] > 0
                assert row["latence_ms"]["total"] / 1000 < 120
            report.update(
                described=True,
                requests=count,
                costs_eur=[r["cout_eur"] for r in rows],
                seconds=time.monotonic() - started,
                image_sha256=hashlib.sha256(image).hexdigest(),
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        target = Path("BRAIN/eval/vision.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
