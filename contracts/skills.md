# P1 skills (REQ-PROJ-02/04/05/07, REQ-SHARE-02/04/06)

A package is one folder containing UTF-8 SKILL.md with YAML front matter
`name` (lowercase slug, <=64 characters), `description` (<=1024 characters,
normalized to one line), and nonempty Markdown instructions. Other ecosystem
metadata is accepted as inert data. Optional files are opaque resources.
Limits: ZIP 1 MiB, expanded 2 MiB, 64 files, individual file 256 KiB,
SKILL.md 32 KiB, catalog 32 uploaded packages / 8 MiB expanded total.
Reject unsafe paths, duplicate files, links, special files, encrypted archives,
unsupported compression and malformed YAML. Nothing is extracted on the host.
Identity is SHA-256 over sorted file paths and contents, independent of ZIP metadata.

Existing authenticated `/api/atlas/orgs/{org}` scope is authoritative:
- GET `/skills`: admin catalog list (built-in and uploaded metadata, enabled state).
- POST `/skills`: application/zip package; disabled initially; 201 metadata.
- PATCH `/skills/{digest}`: `{enabled: boolean}`; org admin only.
- GET `/skills/enabled`: active member's enabled metadata only.
- GET `/skills/{digest}/body`: enabled body and resource names.
- GET `/skills/{digest}/resource/{path}`: enabled bounded base64 resource.
Project routes under `/projects/{project}`:
- GET `/skills`: effective enabled metadata and selection revision.
- PUT `/skills`: `{digests: [...]}`; owner only, replace project enable list.
- GET `/skills/{digest}/body` and `/resource/{path}`: only effective selection.
Mutations require Idempotency-Key, updates If-Match; organization/catalog revision
uses the organization revision; project selection uses the project revision.
All reads recheck current membership, grant, enabled state and intersected
installation/organization digest allowlists. Admin does not bypass project grants.
Package enrollment in installation ceilings is operator-only via the skills CLI.
No automatic policy expansion on upload or enable. Existing P0 reference routes
remain reserved; P1 uses the separate skills routes.

The `load_skill` tool lists only enabled names/descriptions in its stable declaration.
It fetches body on demand, optionally one resource. An approved catalog body is
operator instruction: the tool returns only `skill <name> loaded`, while the
orchestrator inserts a separate system message for the rest of the current turn.
The message delimits the body and labels its name and immutable version (package
SHA-256 digest). It is never serialized as a tool result, retained as a result
reference, or persisted in conversation history/pinned memory. Base prompts stay
unchanged. Documents, web pages, resources and all other tool outputs remain data.
Telemetry records digest/name and estimated loaded tokens.
Chat returns text resources only and cannot execute package code. Work stages
resources through the existing worker protocol and runs commands only in its
networkless container; remote execution is unavailable after a skill is loaded.
Memory tools are unavailable after skill loading; no skill API writes memory.
Existing explicit approvals remain mandatory for outbound/publishing effects.

Acceptance: format/caps/adversarial ZIP checks; real scoped API authorization,
policy/revision/idempotency/revocation; deferred loading, resource execution
containment and memory denial; two fictional inputs per built-in with structural
and substantive output checks; live same-input comparison with/without tools.

Standalone chat discovery is enabled by `ATLAS_SKILLS_ENABLED=1` (P1 rollout
switch, preserving P0 recorded prefixes). In project mode, ordinary chat resolves
the authenticated member's sole organization; multiple memberships require a
project selection. Scoped project Chat/Work use their explicit project catalog.
CLI: `python -m services.retrieval.skills_cli builtins|validate|list|upload|enable|disable|select`.
The public Make entry is `make skills ARGS='builtins'`; pass the same CLI arguments
through ARGS for catalog operations.
`enroll DIGEST` requires the operator DSN; organization approval remains a separate
existing policy PATCH, followed by enable and project selection. Use `--revision`
for updates and `--org`/`--project` selectors. Admin auth comes from
`ATLAS_ADMIN_IDENTITY` (private retrieval endpoint) or `ATLAS_ADMIN_COOKIE` and
`ATLAS_ADMIN_ORIGIN` (authenticated edge); never pass credentials on the command line.
All runtime skill loading requires `ATLAS_SKILLS_ENABLED=1`; existing P0 capability
reports remain unchanged for the UI integration branch. Authorization is refreshed
at every later tool boundary, so disabling a loaded skill cancels further actions.
