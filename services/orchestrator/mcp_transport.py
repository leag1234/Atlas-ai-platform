"""Bounded MCP stdio subset: initialization, ping and synchronous tools/call."""

from packages.limits import LimitError

import asyncio
import json
import os
import signal
from contextlib import suppress

LIMIT = 256 * 1024
VERSION = "2025-11-25"


async def invoke(
    command: str,
    args: list[str],
    env: dict[str, str],
    secrets: list[str],
    tool: str,
    arguments: dict[str, object],
    timeout: float,
) -> dict[str, object]:
    process = await asyncio.create_subprocess_exec(
        command,
        *args,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=LIMIT,
        start_new_session=True,
    )
    assert process.stdin is not None and process.stdout is not None
    writer, reader = process.stdin, process.stdout
    consumed = 0

    async def send(message: dict[str, object]) -> None:
        writer.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
        await writer.drain()

    async def ask(
        number: int, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        nonlocal consumed
        await send({"id": number, "method": method, "params": params})
        for _ in range(32):
            try:
                raw = await reader.readuntil(b"\n")
            except asyncio.LimitOverrunError as exc:
                raise LimitError(
                    "mcp_response_limit", consumed + exc.consumed, LIMIT, "bytes"
                ) from None
            except asyncio.IncompleteReadError:
                raise ValueError("mcp_unexpected_eof") from None
            consumed += len(raw)
            if consumed > LIMIT:
                raise LimitError("mcp_response_limit", consumed, LIMIT, "bytes")
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("mcp_protocol")
            if any(
                secret in json.dumps(message, ensure_ascii=False) for secret in secrets
            ):
                raise ValueError("mcp_secret_echo")
            if "method" in message:
                if "id" in message:
                    await send(
                        {
                            "id": message["id"],
                            **(
                                {"result": {}}
                                if message["method"] == "ping"
                                else {
                                    "error": {"code": -32601, "message": "unsupported"}
                                }
                            ),
                        }
                    )
                continue
            result = message.get("result")
            if (
                type(message.get("id")) is not int
                or message["id"] != number
                or "error" in message
                or not isinstance(result, dict)
            ):
                raise ValueError("mcp_protocol")
            return result
        raise LimitError("mcp_notification_limit", 32, 31, "notifications before reply")

    try:
        async with asyncio.timeout(min(timeout, 15)):
            hello = await ask(
                1,
                "initialize",
                {
                    "protocolVersion": VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "atlas", "version": "1.0.0"},
                },
            )
            capabilities = hello.get("capabilities")
            if (
                hello.get("protocolVersion") != VERSION
                or not isinstance(capabilities, dict)
                or "tools" not in capabilities
            ):
                raise ValueError("mcp_capabilities")
            await send({"method": "notifications/initialized"})
            result = await ask(2, "tools/call", {"name": tool, "arguments": arguments})
            content = result.get("content")
            if (
                result.get("isError")
                or not isinstance(content, list)
                or not all(
                    isinstance(c, dict)
                    and c.get("type") == "text"
                    and isinstance(c.get("text"), str)
                    for c in content
                )
            ):
                raise ValueError("mcp_tool_error")
            return {"content": content}
    finally:
        writer.close()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                await asyncio.wait_for(process.wait(), 0.3)
                break
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, sig)
        await process.wait()
