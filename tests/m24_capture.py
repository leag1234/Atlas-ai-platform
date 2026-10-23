"""Archive real public responses, provider/tools and authenticated downloaded bytes."""

import asyncio
import base64
import gzip
import json
import os
from pathlib import Path
import sys
from typing import Any
import aiohttp
from packages.ports import atlas_ports


ARCHIVE = Path("tests/cassettes/m24.json.gz")


async def _download_files(files: list[str]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    if not files:
        return artifacts
    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
    ) as client:
        async with client.post(
            atlas_ports().url("chat_ui") + "/api/v1/auths/signin",
            json={"email": "admin@localhost", "password": ""},
            allow_redirects=False,
        ) as reply:
            if reply.status != 200:
                raise RuntimeError("download_authentication_http_" + str(reply.status))
            token = (await reply.json(content_type=None))["token"]
        if not isinstance(token, str) or not token:
            raise RuntimeError("download_authentication_failed")
        for url in files:
            assert url.startswith("/api/v1/terminals/atlas-files/files/serve/")
            async with client.get(
                atlas_ports().url("chat_ui") + url,
                headers={"Authorization": "Bearer " + token},
                allow_redirects=False,
            ) as reply:
                assert reply.status == 200
                raw = bytearray()
                async for part in reply.content.iter_chunked(16384):
                    raw.extend(part)
                    assert len(raw) <= 16 * 1024 * 1024
                assert raw
                artifacts.append(
                    {
                        "url": url,
                        "status": reply.status,
                        "bytes": base64.b64encode(raw).decode(),
                    }
                )
    return artifacts


def download_files(files: list[str]) -> list[dict[str, Any]]:
    """The preflight and capture share authenticated downloads with total deadlines."""
    return asyncio.run(_download_files(files))


def capture(
    name: str,
    input_path: Path,
    response_path: Path,
    exchanges: list[dict[str, Any]] | None = None,
    *,
    archive: Path = ARCHIVE,
) -> None:
    request = json.loads(input_path.read_text())
    response = json.loads(response_path.read_text())
    identifier = response["id"]
    if exchanges is None:
        source = json.loads(
            gzip.decompress(
                Path(
                    os.environ.get(
                        "M24_STREAM_ARCHIVE", "/tmp/m24-session-streams.json.gz"
                    )
                ).read_bytes()
            )
        )
        exchanges = [row for row in source if identifier in json.dumps(row["request"])]
    # A document that cannot be read at all is answered by its reading note, without a model call.
    assert exchanges or name == "J46"
    # Attached documents are read in process: only cases that produce files must use tools.
    if name in {"J42", "J43", "J44", "formats_write"}:
        # J42 decks are built by trusted code: their file evidence is the recorded
        # terminal publication instead of a model-authored tool call.
        assert any(
            row["request"].get("kind") in {"tool", "terminal_publish"}
            for row in exchanges
        )
    files = response["atlas"]["files"]
    artifacts = download_files(files)
    cases = (
        json.loads(gzip.decompress(archive.read_bytes())) if archive.exists() else {}
    )
    cases[name] = {
        "request": request,
        "response": response,
        "exchanges": exchanges,
        "artifacts": artifacts,
        "mode": "live",
    }
    raw = json.dumps(cases, ensure_ascii=False).encode()
    assert not any(
        value.encode() in raw
        for key, value in os.environ.items()
        if len(value) >= 8
        and any(word in key for word in ("KEY", "SECRET", "TOKEN", "PASSWORD"))
    ), "secret_in_recording"
    archive.write_bytes(gzip.compress(raw, mtime=0))
    print(name, "captured", len(exchanges), "exchanges", len(artifacts), "downloads")


if __name__ == "__main__":
    capture(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
