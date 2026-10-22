"""Offline browser progression through the real gateway, adapter, WebUI and relay.

Run with ATLAS_PORT_BASE=2200 and PYTHONPATH=.:tests:services/model-gateway.
Only provider transport is synthetic: 400 tokens separated by 40 milliseconds.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import sys
import subprocess
import threading
import time
from typing import Any
from unittest.mock import Mock, patch

import aiohttp
from agent_provider import AgentProvider, AgentRequest
import stream_transport
from gateway_cpu import CPUModels
from http_gateway import serve
from playwright.sync_api import sync_playwright
from serverless_support import environment
from packages.ports import atlas_ports
from packages.profiles import PROFILES
from services.orchestrator.serving import docker
from services.orchestrator.webui import configure
from services.orchestrator.tools import Runtime
from services.orchestrator.terminal_stack import user_interface, network_gateway

TOKENS = [f"word{index:03d} " for index in range(400)]
QUESTION = "Write a long greeting."
OUTPUT = Path("BRAIN/progressive-display")
provider_times: list[list[float]] = []


async def synthetic_stream(
    request: AgentRequest, model: str, timeout: float
) -> AsyncIterator[dict[str, object]]:
    times: list[float] = []
    provider_times.append(times)
    for token in TOKENS:
        times.append(time.monotonic())
        yield {"delta": {"content": token}}
        await asyncio.sleep(0.04)
    yield {
        "result": {
            "text": "".join(TOKENS),
            "calls": [],
            "usage": {"prompt_tokens": 100, "completion_tokens": 400},
            "finish_reason": "stop",
        }
    }


def progression(samples: list[list[float]]) -> dict[str, float | int]:
    growing = []
    previous = 0.0
    for when, size in samples:
        if size > previous:
            growing.append([when, size, size - previous])
            previous = size
    return {
        "sizes": len(growing),
        "first_ms": growing[0][0] if growing else 0,
        "last_ms": growing[-1][0] if growing else 0,
        "final_size": previous,
        "largest_fraction": max((row[2] for row in growing), default=0)
        / max(previous, 1),
    }


async def http_probe(base: str, webui: bool) -> dict[str, Any]:
    """Separate upstream SSE delivery from the native UI's websocket delivery."""
    async with aiohttp.ClientSession(trust_env=False) as client:
        headers = {}
        if webui:
            async with client.post(
                base + "/api/v1/auths/signin",
                json={"email": "admin@localhost", "password": ""},
            ) as response:
                headers["Authorization"] = "Bearer " + (await response.json())["token"]
        started = time.monotonic()
        chunks = []
        content = []
        pending = b""
        size = 0
        async with client.post(
            base + ("/api/chat/completions" if webui else "/v1/chat/completions"),
            headers=headers,
            json={
                "model": PROFILES[0],
                "stream": True,
                "messages": [{"role": "user", "content": QUESTION}],
            },
        ) as response:
            assert response.status == 200
            async for chunk in response.content.iter_any():
                now = (time.monotonic() - started) * 1000
                chunks.append([now, len(chunk)])
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                        continue
                    event = json.loads(line[5:])
                    assert "error" not in event, event
                    for choice in event.get("choices", []):
                        size += len(choice.get("delta", {}).get("content", ""))
                content.append([now, size])
        return {"chunks": chunks, "content": content, "summary": progression(content)}


OBSERVER = """() => {
  window.measurements = {dom: [], chunks: [], frames: [], started: 0};
  const now = () => performance.now() - window.measurements.started;
  const Socket = window.WebSocket;
  window.WebSocket = class extends Socket {
    constructor(...args) {
      super(...args);
      this.addEventListener('message', event => {
        if (window.measurements.started && typeof event.data === 'string'
            && event.data.includes('completion'))
          window.measurements.frames.push([now(), event.data.length]);
      });
    }
  };
  const original = window.fetch;
  window.fetch = async (...args) => {
    const response = await original(...args);
    if (String(args[0]).includes('/api/chat/completions')) {
      const reader = response.clone().body.getReader();
      (async () => { while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        window.measurements.chunks.push([now(), value.length]);
      }})();
    }
    return response;
  };
  let previous = -1;
  setInterval(() => {
    if (!window.measurements.started) return;
    const text = [...document.querySelectorAll('.chat-assistant .markdown-prose')]
      .map(node => node.innerText).join('');
    const size = (text.match(/word\\d{3}/g) || []).length * 8;
    if (size !== previous) {window.measurements.dom.push([now(), size]); previous = size;}
  }, 20);
}"""


def browser(base: str, label: str) -> list[dict[str, Any]]:
    rows = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            for style in ("dense", "arrondi", "document"):
                page = browser.new_page()
                page.add_init_script("(" + OBSERVER + ")()")
                page.goto(base, wait_until="networkidle")
                intro = page.get_by_text("Okay, Let's Go!", exact=True)
                if intro.count():
                    intro.click()
                page.evaluate(
                    "style => localStorage.setItem('atlas-ui', JSON.stringify({style, mode:'light', size:'16', accent:'green'}))",
                    style,
                )
                page.reload(wait_until="networkidle")
                page.locator("#chat-input").wait_for(timeout=60_000)
                page.locator("#chat-input").fill(QUESTION)
                page.evaluate("window.measurements.started = performance.now()")
                page.locator("#send-message-button").click()
                page.wait_for_function(
                    "() => (document.body.innerText.match(/word399/g) || []).length > 0",
                    timeout=45_000,
                )
                page.wait_for_timeout(500)
                measured = page.evaluate("window.measurements")
                row = {
                    "style": style,
                    **measured,
                    "summary": progression(measured["dom"]),
                }
                rows.append(row)
                print(
                    json.dumps({"label": label, "style": style, **row["summary"]}),
                    flush=True,
                )
                page.screenshot(path=str(OUTPUT / f"{label}-{style}.png"))
                page.close()
        finally:
            browser.close()
    return rows


def main() -> None:
    assert atlas_ports().base == 2200
    label = sys.argv[1] if len(sys.argv) > 1 else "after"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    ports = atlas_ports()
    network = ports.name("atlas-files")
    container = ports.name("atlas-progressive-ui")
    env = {
        **environment(),
        "GPU_LOCAL": "0",
        "ATLAS_STABLE": "0",
        "ATLAS_TERMINAL_ENABLED": "0",
        "ATLAS_TTS": "0",
        "ATLAS_GATEWAY_URL": ports.url("gateway"),
        "OPEN_TERMINAL_API_KEY": secrets.token_hex(16),
        "ATLAS_WEB_CACHE": str(OUTPUT / "cache.sqlite"),
        "ATLAS_PROJECT_DB": str(OUTPUT / "projects.sqlite"),
        "ATLAS_INTERACTION_DIR": str(OUTPUT / "synthetic-log"),
        "ATLAS_SEARCH_PROVIDER": "serpapi",
        "SERPAPI_KEY": "synthetic-only",
    }
    docker("network", "create", "--internal", "--label", "atlas.owner=files", network)
    try:
        with patch.dict(os.environ, env), ExitStack() as contexts:
            contexts.enter_context(
                patch.object(stream_transport, "attempt", synthetic_stream)
            )
            contexts.enter_context(
                patch.object(
                    Runtime,
                    "search",
                    side_effect=AssertionError("live search forbidden"),
                )
            )
            contexts.enter_context(
                patch.object(
                    Runtime, "fetch", side_effect=AssertionError("live fetch forbidden")
                )
            )
            contexts.enter_context(
                patch.object(
                    AgentProvider,
                    "complete",
                    side_effect=AssertionError("unexpected atomic model call"),
                )
            )
            gateway = serve(Mock(spec=CPUModels))
            thread = threading.Thread(target=gateway.serve_forever, daemon=True)
            thread.start()
            try:
                contexts.enter_context(user_interface("atlas-progressive-ui", False))
                configure()
                # All clients use actual HTTP boundaries; there is no mocked UI or adapter.
                adapter = ports.url("adapter", network_gateway())
                details = json.loads(docker("inspect", container))[0]
                address = details["NetworkSettings"]["Networks"][network]["IPAddress"]
                probes = {}
                for name, url, webui in (
                    ("adapter", adapter, False),
                    ("webui", ports.url("chat_ui", address), True),
                    ("relay", ports.url("chat_ui"), True),
                ):
                    probes[name] = asyncio.run(http_probe(url, webui))
                    print(
                        json.dumps({"stage": name, **probes[name]["summary"]}),
                        flush=True,
                    )
                rows = browser(ports.url("chat_ui"), label)
                report = {
                    "mode": "offline synthetic provider",
                    "tokens": 400,
                    "interval_ms": 40,
                    "profiles": PROFILES,
                    "probes": probes,
                    "rows": rows,
                    "provider_times": provider_times,
                }
                (OUTPUT / f"{label}.json").write_text(json.dumps(report, indent=2))
                for row in rows:
                    assert row["summary"]["final_size"] == 3200
                    assert row["summary"]["sizes"] >= 40, row["summary"]
                    assert row["summary"]["largest_fraction"] <= 0.1, row["summary"]
                    assert (
                        row["summary"]["last_ms"] - row["summary"]["first_ms"] >= 8000
                    ), row["summary"]
            finally:
                contexts.close()
                gateway.shutdown()
                gateway.server_close()
                thread.join(timeout=5)
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        docker("network", "rm", network)


if __name__ == "__main__":
    main()
