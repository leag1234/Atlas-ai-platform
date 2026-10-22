"""Atomic corpus ingestion with stable, content-addressed citations."""

import argparse
import hashlib
import json
import logging
import os
from packages.ports import atlas_ports
import re
from pathlib import Path

from services.retrieval.extract import extract, split_text
from services.retrieval.search import Gateway
from services.retrieval.store import Chunk, Store


def ingest(root: Path, store: Store, gateway: Gateway) -> list[Chunk]:
    chunks: list[Chunk] = []
    revision: str | None = None
    documents: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlink_in_corpus")
        if path.suffix.lower() not in {".md", ".html", ".pdf", ".docx"}:
            continue
        source = path.relative_to(root).as_posix()
        text = extract(path)
        metadata: object
        if path.suffix.lower() == ".md":
            front = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
            if front is None:
                raise ValueError("missing_metadata")
            metadata = dict(
                re.findall(
                    r"^(doc_id|langue):\s*(\S+)\s*$", front[1], flags=re.MULTILINE
                )
            )
            text = text[front.end() :]
        else:
            sidecar = path.with_suffix(path.suffix + ".json")
            if sidecar.is_symlink():
                raise ValueError("symlink_metadata")
            metadata = json.loads(sidecar.read_text())
        if not isinstance(metadata, dict) or not all(
            isinstance(metadata.get(k), str) and metadata[k].strip()
            for k in ("doc_id", "langue")
        ):
            raise ValueError("invalid_metadata")
        if metadata["doc_id"] in documents:
            raise ValueError("duplicate_document")
        documents.add(metadata["doc_id"])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        parts = split_text(text)
        if not parts:
            raise ValueError("empty_document")
        for start in range(0, len(parts), 32):
            batch = parts[start : start + 32]
            vectors, current = gateway.embed(batch, "document")
            if revision is not None and revision != current:
                raise ValueError("index_revision")
            revision = current
            for offset, (part, vector) in enumerate(zip(batch, vectors, strict=True)):
                position = start + offset
                key = json.dumps(
                    [source, digest, "1", position],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                chunks.append(
                    Chunk(
                        chunk_id=hashlib.sha256(key.encode()).hexdigest(),
                        doc_id=metadata["doc_id"],
                        langue=metadata["langue"],
                        source=source,
                        source_sha256=digest,
                        embedding_revision=current,
                        position=position,
                        text=part,
                        embedding=vector,
                    )
                )
    if not chunks:
        raise ValueError("empty_corpus")
    store.sync(chunks)
    logging.getLogger(__name__).info(
        json.dumps(
            {"event": "ingested", "documents": len(documents), "chunks": len(chunks)}
        )
    )
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest a local corpus into the retrieval-owned index"
    )
    parser.add_argument("--corpus", type=Path, default=Path("corpus"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    store = Store(os.environ["ATLAS_RETRIEVAL_DSN"])
    store.initialize()
    ingest(
        args.corpus,
        store,
        Gateway(os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway"))),
    )


if __name__ == "__main__":
    main()
