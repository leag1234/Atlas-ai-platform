# Derived Index M2

POC-F2; REQ-ENG-002/004/005/007. SQL contract approved in PR #8.
The retrieval service is the sole owner of the atlas_retrieval schema.

Install requirements-dev.txt, provide ATLAS_RETRIEVAL_DSN outside the repository,
then run `python -m services.retrieval.store init` on a dedicated database possessing
pgvector. `sync` reads a JSON list of chunks from stdin; `read` returns the index.
Do not log the output: it contains documents. No source data is deleted. The CLI is
administrative and never exposed to the end user.

Each sync replaces the entire index within a transaction: uniform revision/dimension,
finite non-zero vectors, consistent metadata, SQL constraints.
A failure restores the previous index. Missing sources are deleted.
Full replacement favors simplicity for the small PoC corpus;
stable identifiers preserve citations during identical ingestion.
Unknown resolution raises KeyError; explicit SQL and validation errors.

Rollback on this dedicated database: transaction `DROP SCHEMA atlas_retrieval
CASCADE`, then init and re-ingestion. Do not delete the shared vector extension.
Forward/backward migration and rollback tested on real PostgreSQL/pgvector in an
ephemeral container; cleanup is recorded before test initialization.
The test port is dynamic and bound only to 127.0.0.1.

Dependencies: psycopg[binary] 3.3.5 (LGPL-3.0, typed driver; fragile psql
alternative for runtime transactions), pydantic 2.13.5 (MIT, strict validation;
manual validation alternative harder to audit). Versions compatible with
local Python 3.14 and CI 3.12. No remote calls or GPU costs.
Observability: index_synced event with cardinalities, without documents.
RAG SLO and dashboard await integration; no retrieval results claimed.
