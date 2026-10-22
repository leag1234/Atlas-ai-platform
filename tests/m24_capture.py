"""Archive real public responses, provider/tools and authenticated downloaded bytes."""

import base64
import gzip
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.request import Request, urlopen
from packages.ports import atlas_ports


ARCHIVE = Path("tests/cassettes/m24.json.gz")


def capture(
    name: str,
    input_path: Path,
    response_path: Path,
    exchanges: list[dict[str, Any]] | None = None,
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
    artifacts: list[dict[str, Any]] = []
    if files:
        with urlopen(
            Request(
                atlas_ports().url("chat_ui") + "/api/v1/auths/signin",
                data=json.dumps({"email": "admin@localhost", "password": ""}).encode(),
                headers={"Content-Type": "application/json"},
            ),
            timeout=10,
        ) as reply:
            token = json.load(reply)["token"]
        for url in files:
            assert url.startswith("/api/v1/terminals/atlas-files/files/serve/")
            with urlopen(
                Request(
                    atlas_ports().url("chat_ui") + url,
                    headers={"Authorization": "Bearer " + token},
                ),
                timeout=10,
            ) as reply:
                raw = reply.read(16 * 1024 * 1024 + 1)
                assert reply.status == 200 and 0 < len(raw) <= 16 * 1024 * 1024
                artifacts.append(
                    {
                        "url": url,
                        "status": reply.status,
                        "bytes": base64.b64encode(raw).decode(),
                    }
                )
    cases = (
        json.loads(gzip.decompress(ARCHIVE.read_bytes())) if ARCHIVE.exists() else {}
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
    ARCHIVE.write_bytes(gzip.compress(raw, mtime=0))
    print(name, "captured", len(exchanges), "exchanges", len(artifacts), "downloads")


if __name__ == "__main__":
    capture(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
