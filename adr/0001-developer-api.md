# ADR-0001 — M15 developer API boundary

Status: proposed; adopted by merging the contract under green CI, in accordance
with the human autonomy mandate and conditional squash. Requirements REQ-DEV-001 to005.

Context: M7 chat uses shared private history/RAG/MCP, while M15 introduces
individual keys and quotas and tools executed in clients. Current Codex uses
Responses; Claude Code uses Messages. Both must reach the same code gateway without
accessing private chat data.

Options: add identities to the complete chat pipeline; publish the provider
directly; create an isolated developer entry sharing the gateway.
The first requires a multi-user UI migration outside scope; the second exposes
credentials/costs and does not provide the Messages protocol.

Decision: local8030 entry without conversational state, local SQLite keys/quotas,
and translation of the three formats to the gateway. Tools remain client-side.
Chat8020 retains its private scope. No cloud credential is in clients.
One provider code attempt below0.05EUR preserves the large-context budget; M8 chat
fallback remains unchanged. Budget refusals are visible.

Consequences: two documented local entry points; no public hosting or production
hardening promise. Experimental Messages translation for a non-Claude model, without
Anthropic provider support. Protocol limits are explicit and tested; unknown formats
are refused. SQLite retains unknown reservations after a crash, at the cost of
conservative throughput. Changes to this decision require a new ADR, not editing this one.
