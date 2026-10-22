# Hybrid Retrieval — REQ-ENG-004/006/009, POC-F2/A3

`rank(question, chunks, gateway, k=8)` combines BM25 (k1=1.2, b=0.75)
and dense cosine, RRF fusion (60), then cross-encoder on 32 candidates.
Ties are broken by chunk_id. Evaluation labels are never loaded by the service. CJK characters are indexed individually.

The HTTP gateway is the sole model boundary; 30 s timeout, no retry,
strict limits on body size and validation of cardinalities, identifiers,
finite values, and dimensions. Changing the revision requires explicit re-ingestion.
Explicit ValueError errors; no silent fallback to lexical scores.
The retrieved log exposes the number of candidates/results, without request content.

PoC index traversed in memory; cost O(number of chunks), out of scope for
large-scale optimization. No additional cloud cost. Final SLO POC-P3 to
be measured with the complete pipeline; this increment does not validate M2.
Tests: ranking with distractors, unicode, revision, invalid inputs,
HTTP/timeout errors, and invalid provider responses. Mocks limited to tests.

`make ingest` requires ATLAS_RETRIEVAL_DSN and a running gateway
(ATLAS_GATEWAY_URL, default http://127.0.0.1:8010). Markdown: front matter
with doc_id/langue; pdf/docx/html: adjacent file `<nom.ext>.json` with
these two fields. No symlink sources are accepted. Canonical SHA-256 identifiers
of relative source/fingerprint/chunking version/position.
Ingestion prepares and validates all embeddings before the transaction;
an empty corpus is rejected to prevent accidental deletion.

`services.retrieval.answer.answer` calls /answer on the gateway then resolves each
reference in the Store. Any unknown, duplicated, absent-from-text,
outside-retrieved-passages, or modified-since-retrieval citation is rejected.
Multilingual cassettes and refusals were recorded via the real provider.
