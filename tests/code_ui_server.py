"""Scripted model replay on the real HTTP pipeline and Docker workspace, tests only."""

import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import threading
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import Request
import uvicorn

from packages.ports import AtlasPorts
from services.orchestrator import code_mode
from services.orchestrator.chat_api import app
from services.orchestrator.loop import Call, Message, Turn
from services.orchestrator.terminal_stack import loopback_html_injector
from test_code_mode import FakeModel

OUTPUT = Path("BRAIN/code-mode-ui")
observed: list[dict[str, Any]] = []


def call(name: str, **args: Any) -> Call:
    return Call(name, name, json.dumps(args))


class ReplayModel(FakeModel):
    async def complete(self, messages: list[Message], timeout: float) -> Turn:
        if not self.messages:
            question = str(messages[-1].get("content", ""))
            observed.append({"question": question})
            if "Stop this" in question:
                turns = [Turn("", (call("run_command", command="sleep 30"),))]
            elif "Queue followup" in question or "Review the changes" in question:
                turns = [
                    Turn(
                        "The workspace is ready for review. No further files were changed."
                    )
                ]
            elif "Limit this" in question:
                # The real loop measures and emits the configured model-call limit.
                turns = [Turn("", (call("list_dir"),))] * 5
            else:
                turns = [
                    Turn(
                        "",
                        (
                            call(
                                "update_plan",
                                items=[
                                    {
                                        "text": "Repair addition",
                                        "status": "in_progress",
                                    },
                                    {"text": "Run tests", "status": "pending"},
                                ],
                            ),
                            call("read_file", path="calc.py"),
                        ),
                    ),
                    Turn(
                        "",
                        (
                            call("run_command", command="python3 -m unittest -v"),
                            call(
                                "edit_file",
                                path="calc.py",
                                old_text="a - b",
                                new_text="a + b",
                            ),
                        ),
                    ),
                    Turn(
                        "",
                        (
                            call(
                                "request_approval",
                                action="Fake sensitive action (test only)",
                            ),
                        ),
                    ),
                    Turn(
                        "",
                        (
                            call("run_command", command="python3 -m unittest -v"),
                            call(
                                "update_plan",
                                items=[
                                    {"text": "Repair addition", "status": "completed"},
                                    {"text": "Run tests", "status": "completed"},
                                ],
                            ),
                        ),
                    ),
                    Turn(
                        "The addition function is fixed. The recorded test command passed.\n\n```sh\npython3 -m unittest -v\n```"
                    ),
                ]
                if "Honesty" in question:
                    turns = [Turn("All tests passed. The workspace is ready.")]
            self.turns = iter(turns)
        await asyncio.sleep(0.2)
        return await super().complete(messages, timeout)


@app.post("/testing/mode")
async def mode(request: Request) -> dict[str, bool]:
    values = await request.json()
    os.environ["ATLAS_CODE_MODE"] = str(values.get("flag", "1"))
    os.environ["ATLAS_STABLE"] = str(values.get("stable", "0"))
    os.environ["ATLAS_CODE_MAX_MODEL_CALLS"] = str(values.get("calls", "10"))
    return {"ok": True}


@app.get("/testing/evidence")
async def evidence() -> dict[str, Any]:
    from services.orchestrator.code_tools import gate

    return {
        "requests": observed,
        "active": sorted(code_mode.active),
        "approvals": len(gate.pending),
    }


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        ATLAS_CODE_MODE="1",
        ATLAS_STABLE="0",
        ATLAS_CODE_MAX_MODEL_CALLS="10",
        ATLAS_CODE_STATE_DIR=str(OUTPUT / "replay-state"),
    )
    # The shell and terminal stay on the single real stack; only this test adapter
    # and relay get adjacent ports. No scripted branch is installed in production.
    import subprocess

    terminal_env = json.loads(
        subprocess.check_output(
            [
                "docker",
                "inspect",
                "--format",
                "{{json .Config.Env}}",
                "atlas-open-terminal-6300",
            ],
            text=True,
        )
    )
    os.environ["OPEN_TERMINAL_API_KEY"] = next(
        value.split("=", 1)[1]
        for value in terminal_env
        if value.startswith("OPEN_TERMINAL_API_KEY=")
    )
    os.environ["ATLAS_TERMINAL_ENABLED"] = "1"
    target = subprocess.check_output(
        [
            "docker",
            "inspect",
            "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            "atlas-chat-ui-6300",
        ],
        text=True,
    ).strip()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=14321, log_level="warning")
    )
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                code_mode.CodeModel,
                "connect",
                AsyncMock(side_effect=lambda *args, **kwargs: ReplayModel([])),
            )
        )
        stack.enter_context(
            patch("services.orchestrator.chat_stream.write_interaction")
        )
        stack.enter_context(patch("services.orchestrator.chat_api.write_interaction"))
        stack.enter_context(
            patch(
                "services.orchestrator.terminal_stack.atlas_ports",
                return_value=AtlasPorts(6301),
            )
        )
        stack.enter_context(loopback_html_injector("127.0.0.1:9301", (target, 9300)))
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        print("Code UI replay ready on 9301", flush=True)
        try:
            worker.join()
        finally:
            server.should_exit = True
            worker.join(timeout=10)


if __name__ == "__main__":
    main()
