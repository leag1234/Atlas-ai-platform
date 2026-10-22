# M2 — concrete supplement to the integrated contract on main

Source: contracts/m2.md, POC-F2/A3, REQ-ENG-002/004/009/011,
REQ-FIN-002 ; docs/11 R-02, R-06 and §9.2.

Human commit 736b421 has integrated the general contract. E1 questions are
now present; no regeneration of E1/E2/E3 keys is planned.
This proposal completes the technical boundaries before their implementation.

## Boundaries to review

`m2-storage.sql` describes the derived Postgres/pgvector index, exclusively owned
by retrieval. Metadata is resolved via document/chunk join.
A transaction replaces chunks of a modified document; an error preserves
the old index. A deleted source disappears during a full synchronization.
Identifiers are SHA-256 hashes of a canonical JSON encoding of the relative source,
its fingerprint, the chunking version, and the position. No absolute path or external URL
provided by a document is followed. Outgoing symbolic links are refused.

`m2-gateway.schema.json` defines the JSON bodies for the three internal operations.
HTTP 200 success; errors 400 invalid_input, 413 context_exceeded,
502 provider_error/invalid_citation, 504 timeout. No provider detail or
secret in the error. Client timeout bounded to 30 seconds, no implicit retry.
The gateway receives a maximum of 128,000 cumulative characters per request.
Strings composed solely of spaces are refused at the boundary.

Embeddings: output order and cardinality equal to input; constant dimensions,
finite values, non-zero vectors. The opaque revision invalidates
the index when it changes; model names remain in model-gateway.
Reranking: one finite score per provided passage, no identifier added or omitted.
Generation: each citation belongs to the provided passages; refusal without citation
if sources are insufficient. Prompts will be versioned under prompts/.
These relational invariants complement the JSON Schema constraints.

## Planned increments and tests

Each implementation PR will stay under 400 lines of diff; subdivision if
necessary. Merges remain human. No gate will be presented as an
M2 proof before effective execution of the complete pipeline.

1. PDF/docx/md/html extraction and deterministic chunking with explicit limits
   (10 MB per file, 1 million extracted characters), Unicode, empty pages,
   corrupted archives, and excessive decompression. Red tests before code.
2. Persistence: exact round-trip, idempotence, replacement, deletion,
   rollback on failure, and schema reversibility on an isolated test database.
3. CPU Gateway for multilingual embeddings and cross-encoder. Contracts tested
   against real local engines; error injection in tests only.
4. Retrieval BM25 + dense cosine, RRF fusion then cross-encoder; unique
   and stable top-k, k from 1 to 8. Test distractors and absence of access to E1 keys.
5. Generation and resolution; tests on actually recorded cassettes,
   timeout, provider error, invented citation, and context exceeded.
   No live LLM call in PR CI, no cassette in runtime.
6. Evaluation: recall/MRR per language, effective resolution of citations,
   report compliant with existing schema. `make test` will call the M2 gate in CI
   with anti-recursion guard. Workflows and verify-* will remain unchanged.

Tests for each increment then diff coverage >= 80%, latency measurement,
structured logs without document content, documented runbook and SLO.
Finally local `make verify-m2` then GitHub `ci` job, update BRAIN/ and stop.

## Proposed dependencies, to be decided before installation

- psycopg (LGPL-3.0, a few MB) for Postgres; psycopg2 less suited to
  modern typing, psql subprocess too fragile for runtime transactions.
- pypdf (BSD-3-Clause, a few MB) and python-docx (MIT, with lxml of several
  MB) for binary formats; stdlib insufficient, LibreOffice much heavier.
- pydantic (MIT, a few MB) for boundary validation and strict types;
  manual validation harder to audit. JSON Schema for contract tests.
- sentence-transformers (Apache-2.0) for CPU embeddings/cross-encoder; dominant
  disk cost: PyTorch and weights, potentially several GB. Lighter
  ONNX alternative to measure, remote API incompatible with reproducible CI.
  Versions, weight licenses, and exact sizes to verify before final choice.

RISK: the 20-minute CI limit includes download and CPU inference; measure
the cold path, do not replace engines with dummy scores.
RISK: no recorded generation cassette is present. Recording
requires a real engine and, if serverless, a confirmed spending cap before
billed call. This preparation triggers no call nor provisioning.
CONTRADICTION: MISSION gate 0.70 versus docs/13 target 0.85; keep the gate
protected and also measure the 0.85 target. Do not announce a quality GO on a draft.
NOTICED BUT NOT TOUCHING: PR M1 #6 still open; it reports a local
proof, not re-executed in M2. No GPU will be created for this milestone.

Additional cost of this preparation: 0 EUR/h. No package installed.
Validation of this PR: JSON syntax and diff; no runtime SQL validation,
no retrieval measurement and no M2 proof. Prior review required by R-02.

## Update of M2 mandate
The user authorizes contract and implementation in the same PR, and agent merge
only after green ci job. Corresponding old restrictions are
replaced. The L serverless engine and its cap are confirmed in MISSION.
E1/E2/E3 are already provided: no modification of their keys.
PyYAML 6.0.3 (MIT, about 1 MB, maintained project) reads the format of provided datasets;
alternative home-made parser rejected for YAML fidelity. types-PyYAML, Apache-2.0,
about 50 KB, maintained typeshed stubs, ensures strict typing. No other
new dependency; stdlib urllib suffices for the provider.
