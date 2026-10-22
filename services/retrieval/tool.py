"""Read-only JSON CLI boundary for rag_search (contracts/m3.md)."""

import json
import os
from packages.ports import atlas_ports
import sys

from pydantic import BaseModel, ConfigDict, Field
from services.retrieval.search import Gateway, rank
from services.retrieval.store import Store
from packages.evidence import whole_chunks
from packages.limits import LimitError


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=4000, pattern=r"\S")


def main() -> None:
    raw = sys.stdin.buffer.read(16001)
    if len(raw) > 16000:
        raise LimitError("retrieval_input_limit", len(raw), 16000, "bytes")
    request = Request.model_validate_json(raw)
    store = Store(os.environ["ATLAS_RETRIEVAL_DSN"])
    chunks = rank(
        request.query,
        store.read(),
        Gateway(os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway"))),
    )
    print(
        json.dumps(
            {
                "passages": whole_chunks(
                    [
                        {
                            "chunk_id": c.chunk_id,
                            "text": c.text,
                            "source": c.source,
                        }
                        for c in chunks
                    ],
                    1000,
                )
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
