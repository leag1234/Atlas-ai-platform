# 08 — Platform: API, Multi-tenant, Data, UI

## 1. Public API (Contract)

- REQ-API-001 (MUST): **OpenAPI 3.1** specification versioned in the repository, merged
  **before** any implementation. Clients and server stubs are generated from it. An endpoint not specified does not exist.
- REQ-API-002 (MUST): Versioning via `/v1/…`; breaking change = new major version,
  with a deprecation period ≥ 6 months and a `Sunset` header.
- REQ-API-003 (MUST): SSE streaming with typed events:
  `message_start`, `content_block_delta`, `tool_use`, `citation`, `usage`, `error`,
  `message_stop`. The client must be able to display an intermediate state ("searching your documents...", "executing tool X...") — this is an element of perceived quality, not cosmetic.
- REQ-API-004 (MUST): Idempotency on writes via `Idempotency-Key`.
- REQ-API-005 (MUST): Normalized errors (RFC 9457 `application/problem+json`) with a
  stable and actionable `type`, never leaking internal details (stack trace, model name,
  system prompt).
- REQ-API-006 (MUST): **OpenAI-like** compatibility for the raw completion endpoint,
  so internal teams can connect their existing tools without an adapter.

Surfaces:
```
POST /v1/conversations
POST /v1/conversations/{id}/messages        (SSE)
GET  /v1/conversations/{id}
POST /v1/files                              (upload → asynchronous ingestion)
GET  /v1/files/{id}/status
POST /v1/corpora/{id}/documents             (Enterprise RAG)
POST /v1/assistants                         (config: prompt, tools, corpus, model-class)
POST /v1/chat/completions                   (OpenAI compat, machine usage)
GET  /v1/usage                              (tokens, cost, per tenant/user)
```

## 2. Multi-tenant

- REQ-PLT-001 (MUST): **Logical silo** model: one Postgres database, isolation by
  `tenant_id` + Row Level Security **enabled at the database level**, not only in the
  application code. RLS is the last line of defense when a query forgets a `WHERE`.
- REQ-PLT-002 (MUST): Sensitive tenants may require a **physical** silo (dedicated schema or
  instance). The architecture must allow this without rewriting (connection abstraction per tenant from the start).
- REQ-PLT-003 (MUST): Quotas and rate-limits per tenant, per user, and per API key
  (token bucket): requests/min, tokens/day, € /month, tool calls/day.
- REQ-PLT-004 (MUST): Each tenant has its versioned configuration (assistants, corpus, authorized tools, guard thresholds, authorized model-class).
- REQ-PLT-009 (MUST): **Tenant lifecycle** specified and tooling provided:
  *onboarding* (SSO provisioning — OIDC/SAML —, account synchronization via SCIM,
  initial configuration, corpus); *offboarding* (freeze → export → complete purge with
  proof, see REQ-PLT-008); *export/portability* (REQ-FUN-012) in a documented open
  format (JSONL + source files), triggerable autonomously.
- REQ-PLT-010 (MUST): **Administration console** per tenant: management of users
  and roles, quotas and budgets, corpus and connectors, consultation of usage and
  costs, audit log, triggering exports. Admin actions go through the same
  versioned API (no SQL backdoor) and are logged in `audit_log`.
- REQ-PLT-011 (SHOULD): Internal recharge (chargeback): data from
  `usage_events` is exportable by cost center.

## 3. Data Model (Postgres)

```
tenants(id, name, plan, config_version, data_residency)
users(id, tenant_id, external_id, role)
conversations(id, tenant_id, user_id, title, created_at, archived_at)
messages(id, conversation_id, role, content_blocks jsonb, created_at)
message_meta(message_id, model_id, model_version, prompt_version, policy_version,
             input_tokens, output_tokens, cached_tokens, cost_eur, latency_ms, trace_id)
tool_calls(id, message_id, tool_name, args jsonb, result_ref, status, duration_ms, cost_eur)
citations(message_id, chunk_id, doc_id, span, verified bool)
documents(id, tenant_id, source_uri, checksum, acl jsonb, version, indexed_at)
chunks(id, doc_id, tenant_id, ordinal, text, embedding vector, metadata jsonb)   -- RLS
memories(id, tenant_id, user_id, fact, source_message_id, visible bool, created_at)
audit_log(id, tenant_id, actor, action, subject, payload_hash, at)   -- append-only
usage_events(...)  -- feeds the FinOps dashboard
```

- REQ-PLT-005 (MUST): `message_meta` captures **all** versions (model, prompt,
  policy, corpus). Without this, no past response is explainable, and REQ-NFR-010
  is impossible to satisfy. This is a decision to be made from the first migration:
  adding it later does not reconstruct history.
- REQ-PLT-006 (MUST): `audit_log` as append-only (restricted SQL rights, not just
  a convention).
- REQ-PLT-007 (MUST): Encryption at rest, encryption in transit, secrets in a
  vault (Vault / KMS), automatic rotation.
- REQ-PLT-008 (MUST): Retention configurable per tenant; effective cascading purge
  (messages → embeddings → caches → memory summaries → backups according to policy).
  Write the purge job **at the same time** as the schema, not a year later.

## 4. Data Security Tests (Blocking in CI)

- T-DATA-1: RLS active — a test attempts a query without `tenant_id` and **must** fail.
- T-DATA-2: Cross-tenant vector search → 0 results.
- T-DATA-3: Purge → no residual trace in any store (exhaustive scripted verification
  on all backends, including cache).
- T-DATA-4: Document ACLs are respected during retrieval (REQ-RAG-007).

## 5. UI (Minimum Quality Surface)

- REQ-UI-001 (MUST): Fluid streaming, generation stop, regeneration, editing a
  message and re-branching the conversation.
- REQ-UI-002 (MUST): Clickable citations opening the highlighted source passage. This is the
  first lever of **trust** in enterprise; without it, adoption collapses.
- REQ-UI-003 (MUST): Transparent display of agentic steps (tools called,
  documents consulted), collapsible.
- REQ-UI-004 (MUST): Feedback 👍/👎 + comment → feeds the eval dataset and the DPO (`05`).
- REQ-UI-005 (MUST): Clear mention of the AI nature of the assistant and the fallibility
  of responses (AI Act, REQ-CMP-005).
- REQ-UI-006 (SHOULD): Management of attachments, projects/spaces, searchable history.

## 6. Acceptance Criteria

- AC-PLT-1: OpenAPI merged before code; clients generated, not handwritten.
- AC-PLT-2: The 4 T-DATA tests pass, blocking.
- AC-PLT-3: `GET /v1/usage` reconciles to ±2 % with the provider's invoice.
- AC-PLT-4: Every response in prod is reconstructible (exact prompt, versions, docs) from
  the database — verified by a random audit test.
