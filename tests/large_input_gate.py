"""M10: real extraction/retrieval and exact replay of bounded provider requests."""

import asyncio
import gzip
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Thread
from typing import Any
from unittest.mock import patch

from agent_provider import AgentProvider
from gateway_cpu import CPUModels
from http_gateway import serve
from test_storage import StorageTests
from services.orchestrator.cache import Cache
from services.orchestrator.content import select_passages
from services.orchestrator.loop import Limits, Query, run
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.tools import Fetch, Runtime
from services.retrieval.pipeline import ingest
from services.retrieval.search import Gateway, rank

ARCHIVE = Path("tests/cassettes/large-input.json.gz")
URL = "https://docs.python.org/3/library/asyncio-task.html"
QUESTION = "According to the documentation, what does Task.cancelling() return, and how do cancel() and uncancel() affect that count? Give the exact subtraction formula for the pending count and cite the source."


def assert_formula(answer: str) -> None:
    normalized = answer.replace("`", "").replace("*", "")
    assert re.search(
        r"\bcancel(?:\(\))?[^.\n]{0,120}(?:−|-|minus|less|subtract)[^.\n]{0,120}\buncancel",
        normalized,
        re.IGNORECASE,
    ), "missing_pending_count_formula"


async def check() -> None:
    record = os.environ.get("ATLAS_M10_MODE", "replay") == "record"
    archive: dict[str, Any] = (
        {} if record else json.loads(gzip.decompress(ARCHIVE.read_bytes()))
    )
    started = time.monotonic()
    if record:
        archive["configuration"] = AgentProvider().configuration(False)
        archive["calls"] = []
    config = Configuration.model_validate(archive["configuration"])
    model = GatewayModel("http://recording.invalid", config)
    model.configure_quality("atlas-qwen", 3000)
    provider = AgentProvider() if record else None
    index = 0

    async def transport(
        url: str, payload: dict[str, object], timeout: float
    ) -> dict[str, object]:
        nonlocal index
        stable = {k: v for k, v in payload.items() if k != "timeout"}
        if record:
            assert provider is not None
            response = await provider.complete({**payload, "local_enabled": False})
            archive["calls"].append({"request": stable, "response": response})
            partial = json.dumps(archive, ensure_ascii=False).encode()
            assert not any(
                v.encode() in partial
                for k, v in os.environ.items()
                if len(v) >= 8
                and any(t in k for t in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
            )
            Path("BRAIN/m10-recording-progress.json").write_bytes(partial)
        else:
            row = archive["calls"][index]
            assert row["request"] == stable, "unrecorded_request"
            response = row["response"]
        index += 1
        assert isinstance(response, dict)
        return response

    with tempfile.TemporaryDirectory() as directory:
        runtime = Runtime(
            Cache(Path(directory) / "cache.sqlite"),
            config.input_eur_per_mtok * 0,
            QUESTION,
        )
        if record:
            final_url, body = await runtime.web.fetch(URL, 15)
            archive["web"] = {
                "url": final_url,
                "body": body.decode(),
                "consulted_at": datetime.now(timezone.utc).isoformat(),
            }
        web = archive["web"]

        async def fetch(url: str, timeout: float) -> tuple[str, bytes]:
            assert url == URL
            return web["url"], web["body"].encode()

        with patch.object(runtime.web, "fetch", fetch):
            selected = await runtime.fetch(Fetch(url=URL), 15)
        # Fix only the consultation clock to the recorded real fetch date.
        selected["consulted_at"] = web["consulted_at"]
        complete = runtime.cache.get(str(selected["handle"]))
        assert complete is not None
        text = str(complete["text"])
        assert len(text.encode()) > 32000
        assert "number of calls to cancel() less the number ofuncancel() calls" in str(
            selected["text"]
        )
        assert text.index("cancelling()") > 2000
        offsets = selected["passages"]
        assert isinstance(offsets, list)
        for p in offsets:
            assert text[p["start"] : p["end"]] in str(selected["text"])
        source_id = hashlib.sha256(text.encode()).hexdigest()
        with patch.object(GatewayModel, "post", staticmethod(transport)):
            web_result = await run(
                Query(question=QUESTION, lang="en"),
                model,
                runtime,
                Path("prompts/agent.txt").read_text(),
                limits=Limits(
                    profile="atlas-qwen", tokens=262144, cost=Decimal("0.10")
                ),
                history=[
                    {
                        "role": "tool",
                        "tool_call_id": "source",
                        "content": json.dumps(
                            {"trust": "untrusted", "data": selected}, ensure_ascii=False
                        ),
                    }
                ],
            )
        assert web_result.state == "done" and not web_result.reason, web_result.reason
        assert_formula(web_result.text)
        answer = web_result.text.casefold()
        assert "cancel" in answer and "uncancel" in answer
        assert not any(
            phrase in answer
            for phrase in (
                "does not contain",
                "cannot state",
                "cut off",
                "truncated",
                "not provided",
                "omitting",
            )
        )
        assert any(
            word in answer
            for word in ("minus", "less", "subtract", "decrement", "difference")
        )
        assert (
            str(web["url"]) in web_result.text
            and web["consulted_at"][:10] in web_result.text
        )
        archive["source_sha256"] = source_id if record else archive["source_sha256"]
        assert archive["source_sha256"] == source_id
        # The same long public source exercises real document ingestion and ranking.
        corpus = Path(directory) / "corpus"
        corpus.mkdir()
        (corpus / "asyncio.html").write_text(web["body"])
        (corpus / "asyncio.html.json").write_text(
            json.dumps({"doc_id": "asyncio", "langue": "en"})
        )
        try:
            StorageTests.setUpClass()
            with serve(CPUModels(), 0) as server:
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    gateway = Gateway(f"http://127.0.0.1:{server.server_port}")
                    ingest(corpus, StorageTests.store, gateway)
                    chunks = StorageTests.store.read()
                    retrieved = rank(QUESTION, chunks, gateway)
                finally:
                    server.shutdown()
                    thread.join()
            assert len(chunks) > 10
            passages = [
                {
                    "chunk_id": c.chunk_id,
                    "text": "\n".join(
                        p.text for p in select_passages(c.text, QUESTION, 1199)
                    ),
                }
                for c in retrieved
            ]
            assert any(
                "number of calls to cancel() less the number of" in str(p["text"])
                for p in passages
            )
            model = GatewayModel("http://recording.invalid", config)
            model.configure_quality("atlas-qwen", 3000)
            with patch.object(GatewayModel, "post", staticmethod(transport)):
                document_result = await run(
                    Query(question=QUESTION, lang="en"),
                    model,
                    runtime,
                    Path("prompts/agent.txt").read_text()
                    + Path("prompts/chat.txt").read_text(),
                    limits=Limits(
                        profile="atlas-qwen", tokens=262144, cost=Decimal("0.10")
                    ),
                    history=[
                        {
                            "role": "tool",
                            "tool_call_id": "document",
                            "content": json.dumps(
                                {"trust": "untrusted", "data": {"passages": passages}},
                                ensure_ascii=False,
                            ),
                        }
                    ],
                )
            assert document_result.state == "done" and not document_result.reason
            assert not any(
                phrase in document_result.text.casefold()
                for phrase in ("cut off", "truncated", "strongly imply", "cannot state")
            )
            assert any(
                word in document_result.text.casefold()
                for word in ("minus", "less", "subtract", "difference")
            )
            assert_formula(document_result.text)
            cited = [c for c in retrieved if c.chunk_id in document_result.text]
            assert cited and any("uncancel" in c.text for c in cited)
            assert all(StorageTests.store.resolve(c.chunk_id) == c for c in cited)
            assert (
                "uncancel" in document_result.text and "cancel" in document_result.text
            )
        finally:
            StorageTests.doClassCleanups()
    elapsed = time.monotonic() - started
    assert elapsed <= 120
    assert 0 < web_result.cost <= 0.05 and 0 < document_result.cost <= 0.05
    if record:
        archive["recorded_seconds"] = elapsed
        encoded = json.dumps(archive, ensure_ascii=False).encode()
        assert not any(
            v.encode() in encoded
            for k, v in os.environ.items()
            if len(v) >= 8
            and any(s in k for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        )
        ARCHIVE.write_bytes(gzip.compress(encoded, mtime=0))
    assert index == len(archive["calls"])
    report = {
        "answered": True,
        "context_exceeded": False,
        "provenance": "record" if record else "exact_provider_replay_real_retrieval",
        "seconds": elapsed,
        "recorded_seconds": archive["recorded_seconds"],
        "cost_eur": {
            "web": str(web_result.cost),
            "document": str(document_result.cost),
        },
        "source_sha256": source_id,
        "source_bytes": len(text.encode()),
        "chunks": len(chunks),
        "citations_resolved": len(cited),
    }
    target = Path("BRAIN/eval/large-input.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    asyncio.run(check())
