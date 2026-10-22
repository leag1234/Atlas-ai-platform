"""CI-only recorded provider, real CPU retrieval and disposable PostgreSQL."""

import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from gateway_cpu import CPUModels
from generation import Generator
from http_gateway import serve
from test_storage import StorageTests


def main() -> None:
    cassette = json.loads(Path("tests/cassettes/rag.json").read_text())
    if (
        cassette["prompt_sha256"]
        != hashlib.sha256(Path("prompts/rag.txt").read_bytes()).hexdigest()
    ):
        raise ValueError("stale_prompt_cassette")
    generate = Generator.__call__

    def replay(self: Generator, payload: object) -> dict[str, object]:
        record = next((r for r in cassette["records"] if r["request"] == payload), None)
        if record is None:
            raise RuntimeError("unrecorded_request")
        with patch(
            "generation.urlopen",
            return_value=io.BytesIO(json.dumps(record["provider_response"]).encode()),
        ):
            return generate(self, payload)

    try:
        StorageTests.setUpClass()
        with serve(CPUModels(), 0) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with (
                    patch.object(Generator, "__call__", replay),
                    patch.dict(
                        os.environ,
                        {
                            "ESCALATION_MODEL": "recording",
                            "SCW_GENERATIVE_BASE_URL": "https://example.invalid/v1",
                            "SCW_GENERATIVE_API_KEY": "test-only",
                        },
                    ),
                ):
                    subprocess.run(
                        ["make", "verify-m2"],
                        check=True,
                        env={
                            **os.environ,
                            "ATLAS_RETRIEVAL_DSN": StorageTests.dsn,
                            "ATLAS_GATEWAY_URL": f"http://127.0.0.1:{server.server_port}",
                        },
                    )
            finally:
                server.shutdown()
                thread.join()
    finally:
        StorageTests.doClassCleanups()


if __name__ == "__main__":
    main()
