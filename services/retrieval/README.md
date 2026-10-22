# retrieval

Owner: `OWNERS`. Requirement: POC-F2, REQ-ENG-004/005/007.
Contracts: `contracts/m2.md`, `contracts/m2-implementation-plan.md`.

First M2 increment: local extraction and chunking, without database or inference.
Command: `python -m services.retrieval.extract corpus/01-rh-teletravail.md`
(replace with the actual path). JSON on stdout; structured events on stderr,
without document text. Do not expose stdout in shared logs.

Formats: Textual PDF (no OCR), DOCX (paragraphs/tables in order),
UTF-8 Markdown and HTML without script/style/template. No external links followed.
File <= 10 MiB; text <= 1 million characters; decompressed DOCX <= 1 MB.
The CLI also limits memory space to 512 MiB and CPU time to 15 seconds.
Future ingestion calls must use this process with a wall-clock timeout,
not call the parser directly in a server exposed to untrusted documents.
Deterministic chunking by windows of 2400 characters, overlap 200.

Runbook: an empty, encrypted, invalid, or too large document is rejected; correct
the source, do not ignore the error. Preserve the original corpus. Chunking
is not an index: persistence, metadata, embeddings, and retrieval remain to be delivered.
Provisional CLI SLO: CPU stop <= 15 s; no attested RAG SLO before the pipeline.
Observability: events `extracted` (format, characters) and `extraction_failed`.
Dashboard: not deployed; logs available on stderr for this ad-hoc tool.
Additional cloud cost: 0 EUR/h, no GPU or remote provider.

Dependencies: pypdf (BSD-3-Clause, PDF extraction without office engine),
python-docx (MIT, OOXML reading with lxml BSD), lxml-stubs (Apache-2.0, typing
only). The stdlib does not decode PDF; LibreOffice is heavier.
Versions pinned in requirements-dev.txt for CI and this CLI increment.

## M2 — answers and proof
`make eval-retrieval` measures E1 (recall and MRR per language), then generates three
responses FR/EN/DE from the retrieval and resolves each cited chunk in Postgres.
`ATLAS_RETRIEVAL_DSN` and `ATLAS_GATEWAY_URL` configure these commands.
A missing, unknown, deleted, or modified citation causes the evaluation to fail.
The report BRAIN/eval/retrieval.json is indicative: business datasets are not
human-validated and semantic fidelity E5 remains outside this gate.
The M2 gate is executed by make test in CI, with ephemeral PostgreSQL and real
CPU engines; only the generation provider is replayed under tests/.

M7: `services.retrieval.api:app` exposes POST /search and GET /sources/{chunk_id}
(contracts/m7.md). Only this service reads the index. The published score is that of
the reranker used for ranking; resolution does not expose embeddings.
