"""Fresh provider captures through public HTTP; no replay transport is installed."""

from contextlib import aclosing, contextmanager
import gzip
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import sys
import socket
import subprocess
from collections.abc import Iterator
import tempfile
import threading
import time
from typing import Any
from urllib.parse import unquote
from unittest.mock import patch

from packages.ports import atlas_ports
from fastapi.testclient import TestClient
from gateway_cpu import CPUModels
from http_gateway import serve
from services.orchestrator.chat_api import app
from services.orchestrator.chat_pipeline import ChatTools
from services.orchestrator.loop import Call, Message
from services.orchestrator.terminal_client import TerminalClient
from services.orchestrator.tools import Runtime
import stream_transport
from streaming import StreamDecoder

sys.path.insert(0, str(Path("scripts").resolve()))
import daily_probe


def preserve_files(root: Path, name: str, files: list[str]) -> list[dict[str, Any]]:
    artifacts = []
    for index, url in enumerate(files):
        entry: dict[str, Any] = {"url": url}
        try:
            raw = daily_probe.download(url)
            directory = root / "files" / name / str(index)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = directory / Path(unquote(url)).name
            target.write_bytes(raw)
            entry.update(path=str(target), bytes=len(raw))
        except (OSError, ValueError) as exc:
            entry["copy_error"] = type(exc).__name__
        artifacts.append(entry)
    return artifacts


@contextmanager
def terminal_connection() -> Iterator[None]:
    from services.orchestrator.terminal_stack import loopback_forward

    ports = atlas_ports()
    with socket.socket() as check:
        if check.connect_ex(("127.0.0.1", ports.port("terminal"))) == 0:
            yield
            return
    inspected = subprocess.run(
        ["docker", "inspect", ports.name("atlas-open-terminal")],
        capture_output=True,
        text=True,
    )
    if inspected.returncode == 0:
        details = json.loads(inspected.stdout)[0]
        if details["Config"].get("Labels", {}).get("atlas.owner") != "files":
            raise RuntimeError("terminal_container_owner_mismatch")
        if details["State"]["Running"]:
            address = details["NetworkSettings"]["Networks"][ports.name("atlas-files")][
                "IPAddress"
            ]
            with loopback_forward(
                "127.0.0.1:" + str(ports.port("terminal")), (address, 8000)
            ):
                yield
            return
    from services.orchestrator.terminal_stack import terminal

    with terminal("atlas-m26-terminal", "127.0.0.1:" + str(ports.port("terminal"))):
        yield


def main() -> None:
    if any(
        "REPLAY" in key and value not in ("", "0") for key, value in os.environ.items()
    ):
        raise RuntimeError("replay_environment_forbidden")
    root = Path(os.environ.get("M26_OUTPUT_DIR", "BRAIN/m26"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    report: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    original = stream_transport.attempt
    original_tool = ChatTools.execute
    observed_id: str | None = None

    class ObservedDecoder(StreamDecoder):
        def feed(self, piece: bytes) -> list[dict[str, object]]:
            nonlocal observed_id
            try:
                return super().feed(piece)
            finally:
                observed_id = self.provider_id

    async def execute(runtime: ChatTools, call: Call, timeout: float) -> Message:
        result = await original_tool(runtime, call, timeout)
        if call.name in {"terminal_command", "publish_document"}:
            rows.append(
                {"kind": "terminal", "request": asdict(call), "response": result}
            )
        return result

    async def stream(request: Any, model: str, timeout: float) -> Any:
        nonlocal observed_id
        observed_id = None
        events = []
        provider_id = None
        async with aclosing(original(request, model, timeout)) as source:
            try:
                async for event in source:
                    events.append(event)
                    result = event.get("result")
                    if isinstance(result, dict):
                        provider_id = result.get("provider_id")
                    yield event
            finally:
                rows.append(
                    {
                        "kind": "stream"
                        if events or observed_id
                        else "provider_failure",
                        "provider_id": provider_id or observed_id,
                        "response": events,
                    }
                )

    def wrap(name: str) -> Any:
        method = getattr(Runtime, name)

        async def operation(self: Runtime, request: Any, timeout: float) -> Any:
            result = await method(self, request, timeout)
            rows.append(
                {"kind": name, "request": request.model_dump(), "response": result}
            )
            return result

        return patch.object(Runtime, name, operation)

    def save(name: str, row: dict[str, Any], answer: dict[str, Any]) -> None:
        content = answer.get("choices", [{}])[0].get("message", {}).get("content", "")
        capture = root / (name + ".capture.json.gz")
        raw = json.dumps({"rows": rows, "answer": content}, ensure_ascii=False).encode()
        if any(
            value.encode() in raw
            for key, value in os.environ.items()
            if len(value) >= 8
            and any(k in key for k in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        ):
            raise RuntimeError("secret_in_capture")
        capture.write_bytes(gzip.compress(raw))
        report[name] = {
            **answer.get("atlas", {}),
            **row,
            "mode": "live",
            "capture": str(capture),
            "error": answer.get("error"),
            "preserved_files": preserve_files(
                root, name, answer.get("atlas", {}).get("files", [])
            ),
        }
        (root / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )

    def observe(name: str, row: Any, answer: Any) -> None:
        if row is None:
            rows.clear()
        else:
            save(
                {"presentation": "J54", "owner_pdf": "J55", "plain": "J58"}.get(
                    name, name
                ),
                row,
                answer,
            )

    with (
        terminal_connection(),
        tempfile.TemporaryDirectory() as directory,
        patch.dict(
            os.environ,
            {
                "GPU_LOCAL": "0",
                "ATLAS_TERMINAL_ENABLED": "1",
                "ATLAS_SEARCH_PROVIDER": "tavily",
                "ATLAS_IMAGE_ON_DEMAND": "0",
                "ATLAS_INTERACTION_DIR": directory + "/logs",
                "ATLAS_WEB_CACHE": directory + "/cache.sqlite",
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
                patch.object(stream_transport, "StreamDecoder", ObservedDecoder),
                patch.object(ChatTools, "execute", execute),
                wrap("search"),
                wrap("fetch"),
                TestClient(app) as client,
            ):

                def post(payload: Any) -> Any:
                    response = client.post("/v1/chat/completions", json=payload)
                    return response.status_code, response.json()

                selection = os.environ.get("M26_CASES")
                selected = set(selection.split(",")) if selection else None
                probe_selected = (
                    {"presentation" if name == "J54" else name for name in selected}
                    if selected
                    else None
                )
                provider = "tavily" if selected == {"J54"} else "serpapi"
                with patch.dict(os.environ, {"ATLAS_SEARCH_PROVIDER": provider}):
                    probe = daily_probe.run(
                        post, selected=probe_selected, observer=observe
                    )
                if selected and not selected.intersection({"J56", "J57", "J59"}):
                    if not probe or not all(row["status"] for row in probe.values()):
                        raise SystemExit(1)
                    return
                if not selected:
                    # Exercise the alarm through the public failure path as well.
                    from services.orchestrator.model import GatewayError
                    from services.orchestrator.chat_schema import ChatRequest
                    from services.orchestrator.interactions import Interaction

                    async def failed_probe(
                        request: ChatRequest, item: Interaction
                    ) -> None:
                        raise GatewayError(
                            "injected_probe_failure",
                            503,
                            "Deliberate probe failure: 0 completed requests, minimum 1",
                        )

                    with patch("services.orchestrator.chat_api.process", failed_probe):
                        failure = daily_probe.run(post, selected={"plain"})
                    alarm = (
                        failure["plain"]["status"] is False
                        and "injected_probe_failure"
                        in Path("BRAIN/PROBE_FAILED.txt").read_text()
                    )
                    daily_probe.write_report(probe)
                    report["J58"]["status"] = (
                        alarm
                        and len(probe) == 6
                        and all(r["status"] for r in probe.values())
                    )
                for name, question in (
                    (
                        "J56",
                        "Écris un script Python qui calcule la médiane d’une liste de nombres.",
                    ),
                    (
                        "J57",
                        "Crée un document DOCX contenant trois conseils concrets pour organiser une réunion.",
                    ),
                    (
                        "J59",
                        "conçois une monnaie locale pour une ville de 50 000 habitants",
                    ),
                ):
                    if selected and name not in selected:
                        continue
                    rows.clear()
                    started = time.monotonic()

                    async def broken(
                        self: TerminalClient, command: str, timeout: float
                    ) -> dict[str, object]:
                        raise RuntimeError("deliberate_terminal_failure")

                    with (
                        patch.object(TerminalClient, "execute", broken)
                        if name == "J57"
                        else patch.dict(os.environ, {})
                    ):
                        status, answer = post(
                            {"messages": [{"role": "user", "content": question}]}
                        )
                    content = (
                        answer.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    good = status == 200 and len(content) > 100
                    if name == "J56":
                        good &= "```" in content and "def " in content
                    if name == "J57":
                        good &= (
                            bool(
                                re.search(
                                    r"impossible|pas pu|[eé]chec|failed|could not|ne (?:peux|peut) pas (?:cr[eé]er|produire|g[eé]n[eé]rer)",
                                    content,
                                    re.I,
                                )
                            )
                            and len(content) > 250
                            and not answer.get("atlas", {}).get("files")
                            and any(
                                r.get("response", {}).get("error")
                                == "terminal_operation_failed"
                                for r in rows
                                if r["kind"] == "terminal"
                            )
                            and all(
                                re.search(r"\b" + str(n) + r"[.)]", content)
                                for n in (1, 2, 3)
                            )
                        )
                    if name == "J59":
                        retrieved = [
                            r for r in rows if r["kind"] in ("search", "fetch")
                        ]
                        from services.orchestrator.source_truth import urls

                        good &= bool(retrieved) and all(
                            url in json.dumps(retrieved, ensure_ascii=False)
                            for url in urls(content)
                        )
                    save(
                        name,
                        {
                            "status": good,
                            "seconds": time.monotonic() - started,
                            "cost_eur": answer.get("atlas", {}).get("cost_eur", 0),
                            "http_status": status,
                        },
                        answer,
                    )
                    print(name, good, status, flush=True)
                if selected:
                    if not all(
                        report.get(name, {}).get("status") is True for name in selected
                    ):
                        raise SystemExit(1)
                    return
                rows.clear()
                status, answer = post(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": "Summarize: " + "A measured fact. " * 24000,
                            }
                        ]
                    }
                )
                message = answer.get("error", {}).get("message", "")
                values = re.findall(r"(\d+) microEUR", message)
                report["J60"] = {
                    "mode": "live",
                    "status": status == 429 and len(values) >= 2 and not rows,
                    "http_status": status,
                    "message": message,
                    "measured_microeur": int(values[0]) if values else -1,
                    "limit_microeur": int(values[1]) if len(values) > 1 else -1,
                }
                (root / "report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2)
                )
        finally:
            gateway.shutdown()
            gateway.server_close()
            worker.join(timeout=10)
    if not all(
        report.get("J" + str(n), {}).get("status") is True for n in range(54, 61)
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
