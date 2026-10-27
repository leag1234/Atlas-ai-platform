"""Live same-request skills comparison through an owned local HTTP stack.

Run explicitly; no provider calls during unittest discovery. Uses the configured
provider preset; does not provision cloud resources or start a GPU.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any

import httpx
import uvicorn

from packages.ports import atlas_ports
from packages.profiles import PROFILES
from skill_cases import CASES, checks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", default="")
    parser.add_argument("--output", default="BRAIN/skills/live")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if os.environ.get("ATLAS_PROVIDER") != "hetzner":
        raise SystemExit("This bounded comparison requires the free Hetzner preset")
    if not os.environ.get("ATLAS_PROVIDER_API_KEY"):
        raise SystemExit("Configure ATLAS_PROVIDER_API_KEY before starting the harness")
    ports = atlas_ports()
    for service in ("gateway", "adapter"):
        port = ports.port(service)
        if port in {8000, 8020, 3000, 5432}:
            raise SystemExit("Choose a free non-default ATLAS_PORT_BASE")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
    for variable in ("ATLAS_PROJECT_DSN", "ATLAS_MCP_CONFIG"):
        os.environ.pop(variable, None)
    os.environ.update(
        {
            "GPU_LOCAL": "0",
            "ATLAS_PROJECT_MODE": "0",
            "ATLAS_STABLE": "0",
            "ATLAS_TERMINAL_ENABLED": "0",
            "ATLAS_IMAGE_ENABLED": "0",
            "ATLAS_GATEWAY_URL": ports.url("gateway"),
            "ATLAS_WEB_CACHE": str(output / "web.sqlite"),
            "ATLAS_DATA_DIR": str(output / "data"),
            "ATLAS_IMAGE_ON_DEMAND": "0",
            "ATLAS_INTERACTION_DIR": str(output / "request-journal"),
        }
    )
    from gateway_cpu import CPUModels
    from http_gateway import serve

    gateway = serve(CPUModels(), ports.port("gateway"))
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    gateway_thread.start()
    server = uvicorn.Server(
        uvicorn.Config(
            "services.orchestrator.chat_api:app",
            host="127.0.0.1",
            port=ports.port("adapter"),
            log_level="error",
            access_log=False,
        )
    )
    adapter_thread = threading.Thread(target=server.run, daemon=True)
    adapter_thread.start()
    results: list[dict[str, Any]] = []
    total = 0.0
    try:
        deadline = time.monotonic() + 60
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("local_adapter_start_timeout")
            time.sleep(0.1)
        with httpx.Client(trust_env=False, timeout=320) as client:
            for case in CASES:
                if args.case and case["id"] != args.case:
                    continue
                for enabled in (False, True):
                    os.environ["ATLAS_SKILLS_ENABLED"] = "1" if enabled else "0"
                    payload = dict(
                        model=PROFILES[0],
                        messages=[dict(role="user", content=case["request"])],
                        stream=False,
                        max_tokens=3000,
                        ui_locale="en",
                    )
                    started = time.monotonic()
                    response = client.post(
                        ports.url("adapter") + "/v1/chat/completions", json=payload
                    )
                    body = response.json()
                    text = (
                        body.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )
                    cost = float(body.get("atlas", {}).get("cost_eur", 0))
                    total += cost
                    result = dict(
                        case=case["id"],
                        enabled=enabled,
                        mode="live",
                        request=payload,
                        status=response.status_code,
                        response=body,
                        elapsed_seconds=time.monotonic() - started,
                        checks=checks(case, text),
                        cost_eur=cost,
                    )
                    loaded = body.get("atlas", {}).get("skills_loaded", [])
                    result["correct_skill_loaded"] = any(
                        s["name"] == case["skill"] for s in loaded
                    )
                    (
                        output / f"{case['id']}-{'with' if enabled else 'without'}.json"
                    ).write_text(json.dumps(result, indent=2))
                    results.append(result)
                    print(
                        json.dumps(
                            {
                                k: result[k]
                                for k in (
                                    "case",
                                    "enabled",
                                    "status",
                                    "checks",
                                    "correct_skill_loaded",
                                    "cost_eur",
                                )
                            }
                        ),
                        flush=True,
                    )
                    if response.status_code != 200 or total >= 20:
                        raise RuntimeError("live_request_failed_or_budget_exhausted")
        (output / "summary.json").write_text(
            json.dumps(
                dict(
                    results=[
                        {k: v for k, v in r.items() if k not in {"request", "response"}}
                        for r in results
                    ],
                    cost_eur=total,
                ),
                indent=2,
            )
        )
    finally:
        server.should_exit = True
        gateway.shutdown()
        gateway.server_close()
        adapter_thread.join(10)
        gateway_thread.join(10)


if __name__ == "__main__":
    main()
