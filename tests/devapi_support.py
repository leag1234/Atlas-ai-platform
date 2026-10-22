"""HTTP auth, real SSE framing, quota reconciliation and cancellation without live I/O."""

from collections.abc import AsyncGenerator
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient
import dev_gateway as gateway
from services.orchestrator.dev_auth import Store, provision
from services.orchestrator.devapi import app


class Environment:
    def __init__(self) -> None:
        self.context = ExitStack()
        root = Path(self.context.enter_context(tempfile.TemporaryDirectory()))
        self.context.enter_context(
            patch.dict(
                os.environ,
                {
                    "ATLAS_DEVAPI_DB": str(root / "db"),
                    "SCW_GENERATIVE_BASE_URL": "https://provider.invalid/v1",
                },
            )
        )
        self.store = Store(root / "db")
        provision(self.store, "alice", root / "key", 2, 50000)
        self.headers = {"Authorization": "Bearer " + (root / "key").read_text().strip()}
        self.client = self.context.enter_context(TestClient(app))
        self.raw = json.loads(
            (Path(__file__).parent / "fixtures/devapi-streams.json").read_text()
        )["named"].encode()
        self.inputs: list[str] = []
        self.closed = False
        self.context.enter_context(patch.object(gateway, "transport", self.transport))

    async def transport(self, plan: gateway.Plan) -> AsyncGenerator[bytes, None]:
        self.inputs.append(plan.body)
        try:
            for offset in range(0, len(self.raw), 40):
                yield self.raw[offset : offset + 40]
        finally:
            self.closed = True

    def close(self) -> None:
        self.context.close()
