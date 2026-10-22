"""Isolated real HTTP stack; external provider exchanges are recorded only here."""

import copy
import logging
import gzip
import json
import os
from contextlib import ExitStack, aclosing
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
from unittest.mock import patch
from uuid import UUID

from packages.ports import DEFAULT_PORTS, atlas_ports
from agent_provider import AgentProvider
from vision import VisionProvider
import imagegen
import stream_transport
from serverless_support import environment
from provider_recording import (
    ExactHistory,
    capture_stream,
    capture_tool,
    match_request,
    mismatch_report,
    request_manifest,
    replay_stream,
)
from services.orchestrator.serving import stack
from services.orchestrator.tools import Runtime

ARCHIVE = Path("tests/cassettes/journeys.json.gz")


def cassette_request(value: Any) -> Any:
    """Compare offset-stack requests with base-0 recordings without rewriting either."""
    ports = atlas_ports()
    if ports.base == 0:
        return value
    if isinstance(value, dict):
        return {key: cassette_request(item) for key, item in value.items()}
    if isinstance(value, list):
        return [cassette_request(item) for item in value]
    if isinstance(value, str):
        for service in ("chat_ui", "terminal", "gateway", "retrieval", "adapter"):
            value = value.replace(
                ":" + str(ports.port(service)), ":" + str(DEFAULT_PORTS[service])
            )
    return value


def main() -> None:
    logger = logging.getLogger("services.orchestrator.loop")
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)
    live = os.environ.get("JOURNEYS_LIVE") == "1"
    refresh = os.environ.get("JOURNEYS_REFRESH") == "1"
    resume = os.environ.get("JOURNEYS_RESUME")
    previous = (
        json.loads(gzip.decompress(Path(resume).read_bytes()))
        if resume
        else (json.loads(gzip.decompress(ARCHIVE.read_bytes())) if not live else [])
    )
    rows: list[dict[str, Any]] = previous if resume or not (live or refresh) else []
    prefix_count = len(previous) if resume else 0
    position = 0
    replay_summary: list[dict[str, object]] = []
    generating = False
    injected_timeout = False

    def recording() -> bool:
        return live or refresh

    def replaying() -> bool:
        return not recording() or position < prefix_count

    def replay(kind: str, request: object) -> Any:
        nonlocal position
        if position >= len(rows):
            raise AssertionError(
                f"unrecorded_journey_exchange:{position + 1}:{kind}: no recorded row"
            )
        row = rows[position]
        position += 1
        if row["kind"] != kind:
            raise AssertionError(
                f"unrecorded_journey_exchange:{position}:{kind}: kind: {row['kind']} -> {kind}"
            )
        match = match_request(
            kind, cassette_request(row["request"]), cassette_request(request)
        )
        if match["matched"] == "none":
            raise AssertionError(
                f"unrecorded_journey_exchange:{position}:{kind}: "
                + "; ".join(mismatch_report(match))
            )
        replay_summary.append(
            {
                "row": position,
                "kind": kind,
                "matched": match["matched"],
                "excluded_paths": match["excluded_paths"],
                "prompt_hashes": match["prompt_hashes"],
            }
        )
        return copy.deepcopy(row["response"])

    def record(kind: str, request: object, response: object) -> None:
        rows.append(
            copy.deepcopy(
                {
                    "kind": kind,
                    "request": request,
                    "response": response,
                    "manifest": request_manifest(kind, request),
                }
            )
        )
        encoded = json.dumps(rows, ensure_ascii=False).encode()
        assert not any(
            value.encode() in encoded
            for key, value in os.environ.items()
            if len(value) >= 8
            and any(word in key for word in ("KEY", "SECRET", "TOKEN", "PASSWORD"))
        )
        ARCHIVE.write_bytes(gzip.compress(encoded, mtime=0))

    def wrap(cls: Any, name: str, kind: str) -> Any:
        original = getattr(cls, name)

        async def transport(self: Any, request: Any, *args: Any) -> Any:
            nonlocal injected_timeout
            if generating:
                return await original(self, request, *args)
            value = request.model_dump() if hasattr(request, "model_dump") else request
            if isinstance(value, dict):
                value = {k: v for k, v in value.items() if k != "timeout"}
            if replaying():
                result = replay(kind, value)
                if isinstance(result, dict) and result.get("recorded_timeout") is True:
                    if result.get("fault_injected"):
                        injected_timeout = True
                    raise TimeoutError
                if isinstance(result, dict) and "recorded_exception" in result:
                    errors = {"ValueError": ValueError, "TimeoutError": TimeoutError}
                    raise errors[result["recorded_exception"]](result["message"])
                return result
            saved = unchanged.take(kind, value) if refresh else None
            if saved is not None:
                record(kind, value, saved)
                if isinstance(saved, dict) and saved.get("recorded_timeout") is True:
                    if saved.get("fault_injected"):
                        injected_timeout = True
                    raise TimeoutError
                if isinstance(saved, dict) and "recorded_exception" in saved:
                    errors = {"ValueError": ValueError, "TimeoutError": TimeoutError}
                    raise errors[saved["recorded_exception"]](saved["message"])
                return saved
            if kind == "search" and "Python" in str(value) and not injected_timeout:
                injected_timeout = True
                record(
                    kind,
                    value,
                    {
                        "recorded_timeout": True,
                        "fault_injected": "J12 first search timeout",
                    },
                )
                raise TimeoutError
            return await capture_tool(
                original(self, request, *args),
                lambda result: record(kind, value, result),
            )

        return patch.object(cls, name, transport)

    unchanged = ExactHistory(previous)
    original_stream = stream_transport.attempt

    async def stream(request: Any, model: str, timeout: float) -> Any:
        payload = request.model_dump(exclude={"timeout"})
        saved = unchanged.take("stream", payload) if refresh else None
        if saved is not None:
            record("stream", payload, saved)
            async with aclosing(replay_stream(saved)) as events:
                async for event in events:
                    yield event
            return
        source = (
            replay_stream(replay("stream", payload))
            if replaying()
            else capture_stream(
                original_stream(request, model, timeout),
                lambda events: record("stream", payload, events),
            )
        )
        async with aclosing(source):
            async for event in source:
                yield event

    original_image = imagegen.generate

    async def generate(request: Any) -> Any:
        nonlocal generating
        value = {k: v for k, v in request.items() if k != "timeout"}
        if replaying():
            return replay("image", value)
        if refresh:
            saved_image = next(
                (r for r in previous if r["kind"] == "image" and r["request"] == value),
                None,
            )
            assert saved_image, "unrecorded_image_request"
            result = copy.deepcopy(saved_image["response"])
        else:
            generating = True
            try:
                result = await original_image(request)
            finally:
                generating = False
        record("image", value, result)
        return result

    with tempfile.TemporaryDirectory() as directory, ExitStack() as contexts:
        contexts.enter_context(
            patch.dict(
                os.environ,
                {
                    **({} if live or refresh else environment()),
                    "GPU_LOCAL": "0",
                    # This suite injects SerpApi failures; M24 covers Tavily.
                    **(
                        {"ATLAS_SEARCH_PROVIDER": "serpapi"}
                        if not live and not refresh
                        else {}
                    ),
                    "ATLAS_PROJECT_DB": directory + "/projects.sqlite",
                    "ATLAS_IMAGE_DIR": directory + "/images",
                    "ATLAS_WEB_CACHE": directory + "/web.sqlite",
                    "ATLAS_INTERACTION_DIR": directory + "/interactions",
                },
            )
        )
        contexts.enter_context(
            patch(
                "services.retrieval.projects.uuid4",
                side_effect=(UUID(int=n) for n in range(1, 100)),
            )
        )
        contexts.enter_context(
            patch(
                "services.orchestrator.image_store.uuid4",
                side_effect=(UUID(int=n) for n in range(1000, 2000)),
            )
        )
        for cls, name, kind in (
            (AgentProvider, "_complete", "complete"),
            (VisionProvider, "post", "vision"),
            (Runtime, "search", "search"),
            (Runtime, "fetch", "fetch"),
        ):
            contexts.enter_context(wrap(cls, name, kind))
        contexts.enter_context(patch.object(stream_transport, "attempt", stream))
        contexts.enter_context(patch.object(imagegen, "generate", generate))
        contexts.enter_context(stack("atlas-m17-test", False))
        completed = subprocess.run(
            [sys.executable, "tests/journeys/chat.py"], check=False
        )
        if completed.returncode:
            raise SystemExit(completed.returncode)
        if not live and not refresh:
            summary_path = Path("BRAIN/eval/journey-replay-summary.json")
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(replay_summary, indent=2))
        if not live and not refresh and position != len(rows):
            raise AssertionError("unused_journey_exchanges")


if __name__ == "__main__":
    main()
