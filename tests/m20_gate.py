"""M20 HTTP journeys with explicitly recorded external inference and GPU startup."""

import base64
from contextlib import ExitStack, aclosing
import copy
import gzip
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from packages.configuration import MissingConfiguration

import uvicorn
from agent_provider import AgentProvider
from gateway_cpu import CPUModels
from http_gateway import serve
import imagegen
import stream_transport
import image_lifecycle
from vision import VisionProvider
from journeys.m20 import run_m20
from serverless_support import environment
from provider_recording import ExactHistory, capture_stream, replay_stream
from j8_gate import startup_journey

ARCHIVE = Path("tests/cassettes/m20.json.gz")


def main() -> None:
    live = os.environ.get("M20_LIVE") == "1"
    refresh = os.environ.get("M20_REFRESH") == "1"
    previous = json.loads(gzip.decompress(ARCHIVE.read_bytes()))
    historical_used: set[int] = set()
    image_modes: list[str] = []
    resume = os.environ.get("M20_RESUME")
    rows: list[dict[str, Any]] = (
        json.loads(gzip.decompress(Path(resume).read_bytes()))
        if resume
        else ([] if live or refresh else previous)
    )
    prefix = len(rows) if resume else 0
    position = 0

    def replaying() -> bool:
        return not (live or refresh) or position < prefix

    inside = False

    def record(kind: str, request: Any, response: Any) -> Any:
        rows.append(
            copy.deepcopy({"kind": kind, "request": request, "response": response})
        )
        raw = json.dumps(rows, ensure_ascii=False).encode()
        assert not any(
            v.encode() in raw
            for k, v in os.environ.items()
            if len(v) >= 8
            and any(w in k for w in ("KEY", "SECRET", "TOKEN", "PASSWORD"))
        )
        ARCHIVE.write_bytes(gzip.compress(raw, mtime=0))
        return response

    def replay(kind: str, request: Any) -> Any:
        nonlocal position
        row = rows[position]
        position += 1
        assert row["kind"] == kind and row["request"] == request, (position, kind)
        return copy.deepcopy(row["response"])

    def historical(kind: str, request: Any) -> Any:
        for index, row in enumerate(previous):
            if (
                index not in historical_used
                and row["kind"] == kind
                and row["request"] == request
            ):
                historical_used.add(index)
                return record(kind, request, copy.deepcopy(row["response"]))
        raise AssertionError("unrecorded_historical_m20_" + kind)

    def wrap(cls: Any, name: str, kind: str) -> Any:
        original = getattr(cls, name)

        async def transport(self: Any, value: Any, *args: Any) -> Any:
            request = value.model_dump() if hasattr(value, "model_dump") else value
            if isinstance(request, dict):
                request = {k: v for k, v in request.items() if k != "timeout"}
            if inside:
                return await original(self, value, *args)
            if replaying():
                return replay(kind, request)
            return record(kind, request, await original(self, value, *args))

        return patch.object(cls, name, transport)

    original_image = imagegen.generate

    async def image(request: Any) -> Any:
        nonlocal inside
        value = {k: v for k, v in request.items() if k != "timeout"}
        if replaying():
            return replay("image", value)
        if refresh and any(
            row["kind"] == "image" and row["request"] == value for row in previous
        ):
            image_modes.append("replay")
            return historical("image", value)
        image_modes.append("live")
        inside = True
        try:
            return record("image", value, await original_image(request))
        finally:
            inside = False

    original_ready = image_lifecycle.ready
    original_ensure = image_lifecycle.ensure_worker

    def ready() -> bool:
        if inside:
            return original_ready()
        if refresh and not replaying():
            return bool(historical("ready", {}))
        return bool(
            replay("ready", {})
            if replaying()
            else record("ready", {}, original_ready())
        )

    async def ensure(timeout: float) -> None:
        nonlocal inside
        if replaying():
            saved = replay("startup", {})
            if "missing" in saved:
                raise MissingConfiguration(tuple(saved["missing"]))
            return
        if refresh:
            saved = historical("startup", {})
            if "missing" in saved:
                raise MissingConfiguration(tuple(saved["missing"]))
            return
        inside = True
        started = time.monotonic()
        try:
            await original_ensure(timeout)
            record(
                "startup", {}, {"ready": True, "seconds": time.monotonic() - started}
            )
        except MissingConfiguration as exc:
            record("startup", {}, {"missing": list(exc.missing)})
            raise
        finally:
            inside = False

    unchanged = ExactHistory(previous)
    original_stream = stream_transport.attempt

    async def stream(request: Any, model: str, timeout: float) -> Any:
        value = request.model_dump(exclude={"timeout"})
        saved = unchanged.take("stream", value) if refresh else None
        if saved is not None:
            record("stream", value, saved)
            async with aclosing(replay_stream(saved)) as events:
                async for event in events:
                    yield event
            return
        source = (
            replay_stream(replay("stream", value))
            if replaying()
            else capture_stream(
                original_stream(request, model, timeout),
                lambda events: record("stream", value, events),
            )
        )
        async with aclosing(source):
            async for event in source:
                yield event

    def cold_start() -> None:
        if not replaying() and not refresh:
            # End the earlier launcher's trap before creating its replacement.
            for path in Path("/proc").glob("[0-9]*/cmdline"):
                try:
                    if path.read_bytes().split(b"\0")[:3] != [
                        b"bash",
                        b"infra/imagegen.sh",
                        b"serve",
                    ]:
                        continue
                    if (path.parent / "cwd").resolve() != Path.cwd():
                        continue
                    pid = int(path.parent.name)
                    children = (
                        (path.parent / "task" / str(pid) / "children")
                        .read_text()
                        .split()
                    )
                    os.kill(pid, signal.SIGTERM)
                    for child in children:
                        if Path(f"/proc/{child}/comm").read_text().strip() == "sleep":
                            os.kill(int(child), signal.SIGTERM)
                    deadline = time.monotonic() + 60
                    while path.exists() and path.read_bytes():
                        assert time.monotonic() < deadline, (
                            "previous_launcher_still_active"
                        )
                        time.sleep(0.2)
                except ProcessLookupError:
                    continue
            subprocess.run(["bash", "infra/gpu-down.sh"], check=True)
        # In replay, readiness and startup come from the actual cold-start recording.

    report_path = Path("BRAIN/eval/journeys.json")
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    report["m20_mode"] = "mixed" if resume or refresh else "live" if live else "replay"
    report["m20_journey_modes"] = {
        f"J{n}": "mixed"
        if refresh
        else "replay"
        if not live or (resume and n < 26)
        else "live"
        for n in range(21, 27)
    }
    report.setdefault("mode", "live" if live or refresh else "replay")
    if refresh:
        report["m20_external_modes"] = {
            "inference": "mixed",
            "image": image_modes,
            "startup": "replay",
        }
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    **({} if live or refresh else environment()),
                    "ATLAS_GATEWAY_URL": "http://127.0.0.1:18010",
                    "ATLAS_PUBLIC_URL": "http://127.0.0.1:18020",
                    "ATLAS_IMAGE_DIR": directory + "/images",
                    "ATLAS_SEARCH_PROVIDER": "tavily",
                    "ATLAS_INTERACTION_DIR": directory + "/logs",
                    "ATLAS_IMAGE_ON_DEMAND": "1",
                },
            )
        )
        stack.enter_context(wrap(AgentProvider, "_complete", "complete"))
        stack.enter_context(wrap(VisionProvider, "post", "vision"))
        stack.enter_context(patch.object(imagegen, "generate", image))
        stack.enter_context(patch.object(stream_transport, "attempt", stream))
        stack.enter_context(patch.object(image_lifecycle, "ready", ready))
        stack.enter_context(patch.object(image_lifecycle, "ensure_worker", ensure))
        gateway = serve(CPUModels(), 18010)
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        server = uvicorn.Server(
            uvicorn.Config(
                "services.orchestrator.chat_api:app",
                host="127.0.0.1",
                port=18020,
                log_level="error",
            )
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        from services.orchestrator.serving import wait_http

        wait_http("http://127.0.0.1:18020/v1/models")

        def ask(
            prompt: str,
            images: list[dict[str, object]] | None = None,
            *,
            lang: str = "fr",
            stream: bool = False,
            error: str | None = None,
        ) -> tuple[str, list[dict[str, Any]]]:
            content: Any = (
                [{"type": "text", "text": prompt}, *images] if images else prompt
            )
            data = {
                "messages": [{"role": "user", "content": content}],
                "ui_locale": lang,
                "stream": stream,
            }
            try:
                with urlopen(
                    Request(
                        "http://127.0.0.1:18020/v1/chat/completions",
                        data=json.dumps(data).encode(),
                        headers={"Content-Type": "application/json"},
                    ),
                    timeout=1030,
                ) as response:
                    body = response.read().decode()
            except HTTPError as exc:
                if error is None:
                    raise
                failure = json.loads(exc.read())["error"]
                assert exc.code == 503 and failure["code"] == error, failure
                return str(failure["message"]), []
            assert error is None, "expected startup refusal"
            if stream:
                events = [
                    json.loads(line[6:])
                    for line in body.splitlines()
                    if line.startswith("data: ") and line != "data: [DONE]"
                ]
                assert not any("error" in event for event in events), body[:1000]
                text = "".join(
                    e["choices"][0]["delta"].get("content", "") for e in events
                )
                metadata = events[-1]["atlas"].get("images", [])
            else:
                result = json.loads(body)
                text = result["choices"][0]["message"]["content"]
                metadata = result["atlas"]["images"]
            for im in metadata:
                if "reference" in im:
                    with urlopen(im["reference"], timeout=5) as response:
                        im["assessment_image"] = (
                            "data:image/png;base64,"
                            + base64.b64encode(response.read()).decode()
                        )
            return text, metadata

        try:
            run_m20(ask, report, cold_start)
            cold_start()
            original_model = os.environ.pop("LOCAL_MODEL", None)
            try:
                refusal, _ = ask(
                    "Dessine un cercle rouge.", error="configuration_missing"
                )
                assert (
                    "LOCAL_MODEL" in refusal
                    and "Missing required environment variables" in refusal
                ), refusal
            finally:
                if original_model is not None:
                    os.environ["LOCAL_MODEL"] = original_model
            result = subprocess.run(
                [sys.executable, "-m", "services.orchestrator.serving"],
                env={
                    "PATH": os.environ["PATH"],
                    "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
                },
                capture_output=True,
                timeout=15,
            )
            assert (
                result.returncode != 0
                and b"SCW_GENERATIVE_API_KEY" in result.stderr
                and b"Missing required environment variables" in result.stderr
            )
            report["J26_missing_var_refused"] = True
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            gateway.shutdown()
            gateway.server_close()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        if not live and not refresh:
            assert position == len(rows), "unused_m20_exchanges"
    with patch.dict(os.environ, environment()):
        report.update(startup_journey())
    report.setdefault("journey_modes", {}).update(
        {"J8": "live", **report["m20_journey_modes"]}
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
