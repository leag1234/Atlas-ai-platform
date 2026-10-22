"""Real HTTP/CPU/index/UI integration; provider replay exists only in tests."""

from collections.abc import AsyncIterator
from contextlib import ExitStack

import gzip
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from unittest.mock import patch

from agent_provider import AgentProvider, AgentRequest
from serverless_support import environment
from stream_transport import StreamRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.serving import stack
from native_ui import native_ui
from packages.ports import atlas_ports

RECORDING = Path("tests/cassettes/chat.json.gz")


def main() -> None:
    record = os.environ.get("ATLAS_M7_MODE", "replay") == "record"
    mode = "record" if record else "replay"
    archive: dict[str, Any] = (
        {"calls": []} if record else json.loads(gzip.decompress(RECORDING.read_bytes()))
    )
    original = AgentProvider.complete
    if record:
        archive["configuration"] = AgentProvider().configuration(False)

    async def transport(
        self: AgentProvider,
        payload: object,
    ) -> dict[str, object]:
        request = AgentRequest.model_validate(payload)
        assert request.local_enabled is False
        stable = request.model_dump(exclude={"timeout"})
        if record:
            response = await original(self, payload)
            archive["calls"].append({"request": stable, "response": response})
            return response
        rows = [r for r in archive["calls"] if r["request"] == stable]
        if len(rows) != 1:
            raise RuntimeError("unrecorded_chat_request")
        response = rows[0]["response"]
        if not isinstance(response, dict):
            raise ValueError("invalid_recording")
        return response

    async def recorded_stream(
        self: AgentProvider, payload: object
    ) -> AsyncIterator[dict[str, object]]:
        # M7 protocol replay only; M11 independently proves live progression.
        request = StreamRequest.model_validate(payload)
        assert request.reasoning_effort == "none"
        result = await self.complete(request.model_dump(exclude={"reasoning_effort"}))
        if result.get("text"):
            yield {"delta": {"content": result["text"]}}
        yield {"result": result}

    provider_env = {} if record else environment()
    ui = ExitStack()
    try:
        with (
            patch.dict(
                os.environ,
                {**provider_env, "GPU_LOCAL": "0", "ATLAS_SEARCH_PROVIDER": "tavily"},
            ),
            patch.object(
                AgentProvider, "configuration", return_value=archive["configuration"]
            ),
            patch.object(AgentProvider, "complete", transport),
            patch.object(AgentProvider, "stream", recorded_stream),
            patch(
                "services.orchestrator.chat_api.Interaction",
                side_effect=lambda: Interaction(execution=mode),
            ),
            stack("atlas-m7-test", False),
        ):
            if os.environ.get("ATLAS_UI_GATE") != "1":
                ui.enter_context(native_ui("atlas-m7-test-ui"))
            payload = {
                "model": "atlas-qwen",
                "messages": [
                    {
                        "role": "user",
                        "content": "D'après la politique interne de télétravail, quel est l'objet du document et quels salariés sont concernés ? Cite le document.",
                    }
                ],
            }
            request = Request(
                atlas_ports().url("adapter") + "/v1/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=125) as response:
                result = json.load(response)
            assert result["choices"][0]["message"]["content"]
            rows = [
                json.loads(line)
                for path in Path("BRAIN/interactions").glob("*.jsonl")
                for line in path.read_text().splitlines()
            ]
            selected = [row for row in rows if row["request_id"] == result["id"]]
            assert len(selected) == 1
            row = selected[0]
            assert row["modele_utilise"] == "escalade"
            assert row["execution"] == mode
            assert row["route_decision"] in ("simple", "complexe")
            assert row["citations"] and row["chunks_recuperes"] and not row["erreurs"]
            assert 0 < row["cout_eur"] <= 0.05
            assert row["tokens"]["in"] > 0 and row["tokens"]["out"] > 0
            assert all(
                row["latence_ms"][k] > 0 for k in ("retrieval", "generation", "total")
            )
            for citation in row["citations"]:
                assert any(
                    p["chunk_id"] == citation["chunk_id"]
                    and isinstance(p["score"], float)
                    for p in row["chunks_recuperes"]
                )
                with urlopen(
                    atlas_ports().url("adapter") + "/sources/" + citation["chunk_id"],
                    timeout=5,
                ) as source:
                    assert source.status == 200
            if not record:
                request.data = json.dumps({**payload, "stream": True}).encode()
                with urlopen(request, timeout=125) as response:
                    streamed = response.read().decode()
                assert "data: [DONE]" in streamed
                assert row["citations"][0]["chunk_id"] in streamed
            if record:
                encoded = json.dumps(archive, ensure_ascii=False).encode()
                assert not any(
                    v.encode() in encoded
                    for k, v in os.environ.items()
                    if len(v) >= 8
                    and any(s in k for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
                )
                RECORDING.write_bytes(gzip.compress(encoded, mtime=0))
            print("Source: UI gate", mode, result["id"], row["cout_eur"])
    finally:
        ui.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--verify"]:
        with (
            patch.dict(os.environ, {"ATLAS_UI_GATE": "1"}),
            native_ui("atlas-m7-test-ui"),
        ):
            subprocess.run(["bash", "scripts/verify-m7.sh"], check=True)
    else:
        main()
