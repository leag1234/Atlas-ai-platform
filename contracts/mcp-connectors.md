# Version 0.8 connector contract — REQ-SHARE-07/08/09

Public authenticated API: POST /api/atlas/mcp/{org_id}/{operation}
(existing front relay prefix); /api/mcp/{org_id}/{operation} is an adapter alias. Optional project_id
selects a currently granted project. The browser supplies no member identity.
Operations: register (admin), list, member-rule (admin), project-list (owner),
connect-token (self), disconnect (self), oauth-start (self), oauth-finish (self),
discover, call, approve. OAuth redirects to the configured exact callback;
the authenticated member submits its state and code to oauth-finish.
Internal counterpart: /internal/atlas/connectors/{operation}, signed project
identity only, current membership/policy/grant revalidated before every action.

Registry entries: UUID id, name, kind shared/personal, transport HTTP URL or
allowlisted stdio command identifier, auth none/token/oauth. Admin registration
contains no credential. A token always belongs to (organization, member, connector).
Stdio command and exact arguments are deployment allowlisted, never model supplied.
HTTP endpoints require HTTPS, except explicitly allowed local test endpoints.
OAuth uses protected-resource and issuer metadata, preregistered client IDs, S256
PKCE, single-use state bound to member/connector/redirect, and resource binding.
No automatic retry of calls. Disconnect deletes credentials and pending OAuth.

All three policy layers must allow: organization UUID allowlist; member rule
(default deny, explicit deny overrides group allows when groups are introduced);
project list (default empty). Ordinary chat omits the project layer. Explicit visibility=private permits private reads (creator only); shared and legacy
project-readable conversations require every personal call to be approved. Pending calls bind member, project, conversation, connector,
arguments, discovery schema, current credential fingerprint and exact command configuration; expire in five minutes;
are single use; authority and credentials are checked again on approval.
Writes and every personal project call return state=approval_required, approval_id,
connector, action and visibility_warning. Approval is a browser API operation,
never a model tool. Denial returns state=denied without invoking the connector.

Tool selection: one group per connector; tools/list schemas and descriptions
populate only authorized groups. A switch tool selects group/focus; at most eight
declarations are offered. Execution rejects calls outside the selected group.
Transport limits: 15 seconds, 256 KiB response, 16 KiB call arguments,
128 discovered tools, bounded pagination/notifications; JSON and SSE accepted.
Credentials and PKCE state are AEAD encrypted under a server-supplied key; broker
storage is separate from project files. Logs contain identifiers/outcomes only.

Deployment: enable ATLAS_MCP_CONNECTORS=1 on the adapter; configure the project
identity broker as for project mode. ATLAS_MCP_BROKER_DB is a dedicated private
SQLite path outside project storage; ATLAS_MCP_BROKER_KEY is a base64 32-byte
server key. Back up the encrypted broker and key separately. ATLAS_MCP_COMMANDS
maps command IDs to exact command/args/token_env; no inherited environment.
ATLAS_MCP_ALLOW_LOCAL=1 permits loopback HTTP for local integration only.
The installation policy ceiling and organization connector_allowlist both contain
registered connector UUIDs. Members default deny. OAuth clients are preregistered
public clients; expired tokens require reconnect, with no automatic call retry.
Legacy ATLAS_MCP_CONFIG remains available only on the unscoped legacy path;
authenticated connector turns suppress that installation credential path.

Dependencies: cryptography (Apache-2.0/BSD, maintained PyCA, ~4.5 MB wheel)
provides authenticated AES-GCM; custom encryption is not an acceptable alternative.
jsonschema (MIT, maintained, ~90 KB plus referencing dependencies) validates arbitrary
discovered schemas; hand-written subsets would accept invalid arguments. Its type
stubs are development-only. Schema references cannot fetch external resources.

Ordinary chat may select connector_org_id explicitly (required for multi-org
members); a single membership is otherwise resolved automatically. This selector
does not select project context. Legacy credential tools are disabled whenever
ATLAS_MCP_CONNECTORS=1. Run the existing project installer --upgrade-work to apply
the additive viewer connector ledger policies; rollback drops the two policies
from connector_schema.sql, without deleting content.

The registry is capped at 128 connectors per organization. Each broker operation
has a 35-second total deadline in addition to the per-exchange 15-second limit;
this also bounds discovery across several connectors. Newly created project turns
bind connector actions to the conversation ID returned by the transcript broker.

Chat JSON and the final SSE chunk expose `atlas.connector_approvals`; streaming
also emits `phase=connector_approval` with the approval object as soon as a call
requires consent. The model cannot approve. The browser submits `approve` with
its authenticated session. An approval result is returned to the browser for the
next turn; there is no automatic model continuation or UI implementation here.

Network boundaries reject private, link-local, metadata and transition addresses,
including DNS results actually used by the socket. Loopback is enabled only by
the explicit local integration flag. Redirects and compressed responses are
rejected; no bearer token is forwarded to a redirect destination. OAuth clients
use preregistration (automatic dynamic registration is not implemented).

Protocol references: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
and https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization.
