# Remote runners — Linux implementation contract

Status: approved by the supervisor (direction and owner answers, 2026-10-25).
Source: design 18, REQ-RUN-01 through REQ-RUN-08; project mode sections 5–7.
This is additive to `project-mode.openapi.yaml`; Linux is the first transport.
Owner-authorized real targets (2026-10-26) add macOS POSIX execution and
read-only Android commands through the existing SSH transport. Windows remains deferred.

## Authority and storage

Organization admins register targets, generate per-target ed25519 credentials,
pin SHA256 host-key fingerprints, probe, set policy and grant projects access.
Private keys exist only in the runner container's private volume. Public keys
are returned once by generation; later reads return opaque references and
fingerprints. No API accepts or returns private keys. The model projection
excludes host, port, user, credential references and host keys.

The retrieval service owns tenant tables `runner_targets`, `runner_grants`,
`runner_sessions`, `runner_commands`, using forced RLS and organization/project
filters. Existing scoped identities, execution secrets, membership checks and
Work fences govern every operation. The runner never accesses retrieval's DB:
it verifies a short-lived retrieval-signed permission before SSH effects and
rechecks the grant, command policy and approval binding. Durable broker
checkpoints are execution state, not a second authorization database.

Targets include revision/timestamps, Linux or macOS OS, architecture, shell, address,
work root, credential reference, pinned host-key fingerprints, optional jump
target, permission mode, allow/deny/sensitive rules, session/disk/minutes quotas,
probe status and same-locality designation. Grants include revision, project,
grantor, egress decision and project rules. Sessions bind organization, project,
Work session, target, directory, synced digest and last activity. Commands bind
member, session, target, exact command, cwd, timeout, code digest, decision,
state, duration, transferred bytes and project file references.

## Execution and approvals

`remote_run` freezes the scratch snapshot before approval. The first egress and
an ask-mode command share one card, bound to that exact snapshot and command.
Approval expires after 120 seconds and is consumed once. Revocation, target or
policy revision, membership or grant changes invalidate it. Denials cannot be
approved away. The card can add a confirmed project command-prefix rule.
`ask` still asks for commands except those explicitly allowed by that project
rule; `rules` allows matching commands; `auto` permits all supported commands.
Deny and sensitive rules apply in every mode. Local-only code requires an
admin-designated target in the same locality.

Rules operate on parsed argv, every pipeline and conditional-chain component,
not a raw startswith. A final `*` matches remaining arguments. Deny chains may
match a whole pipeline. Unsupported shell expansion, redirection, background
syntax or malformed commands fail closed with an explicit error. No new shell
parser dependency. Literal quoted arguments, pipelines, `&&`, `||` and `;` are
supported; execution uses the same validated command. Capabilities grant no
permissions. Rules allowing interpreters authorize their argument programs.

SSH uses a cleared environment, explicit identity, pinned known_hosts,
StrictHostKeyChecking, BatchMode, no forwarding and bounded connection time.
Key scanning is only used to retrieve public material matching the admin's
already-pinned fingerprint; never trust-on-first-use. Only registered endpoints
are reachable from the broker's SSH transport; deployment firewall limits
network egress to these pairs and the internal authorization endpoint.

Each Work session/target has a persistent directory. Initial Git bundle clone
and subsequent incremental bundles transfer exact scratch snapshots; untracked
build caches survive. Commands run in separate process groups with a remote
watchdog (600 second default, 1800 maximum). Cancel and timeout kill the group.
A lost connection produces `unknown`; reconciliation checks the remote command
record before any new work and never reruns a command. Session close/24-hour
idle cleanup removes directories; failed cleanup stays visible and retryable.
Process identity uses Linux procfs start time or macOS `ps` start time; a missing
or changed identity never authorizes signalling a reused PID. Probing verifies
the registered OS against the remote OS. macOS requires Python 3 and Git.
Concurrency, reserved minutes and session disk limits apply before execution.

## Results and audit

Tools: `list_targets`, `remote_run`, `remote_status` (including cancel),
`remote_fetch`. At most four remote group tools enter context. Commands may
outlive a tool wait and remain attached to their Work session. Progress is
streamed as existing Work events. Full redacted logs and explicitly fetched
files become project files, with hashes, byte counts, untrusted provenance and
`reopen_result` references. Summaries are at most 8192 UTF-8 bytes, with exit,
duration, tail, JUnit/TAP counts and first failures where available. Fetch is
bounded in bytes/count, rejects symlinks and traversal and never syncs implicitly.

Ledger events: runner_code_egress_approved/revoked, runner_session_opened/closed,
runner_command_requested/approved/denied/started/completed/failed/cancelled/unknown,
runner_file_fetched. Audit holds command text and digest, decision, code digest,
identity, destination, outcome and byte counts; never SSH keys or log bodies.

## Verification

Contract examples; tenant/role/revision/approval matrices; real disposable sshd
container tests for pin mismatch, sync/cache preservation, process groups,
timeout/cancel, reconnect, cleanup, safe fetch, logs and permission parsing.
Work tool and browser integration, full credential-free suite and journeys.
Live acceptance is limited to the atlas-project trial and a dedicated
unprivileged sshd container, at most two attempts after the harness is green,
EUR 0.50. No milestone or CI completion is claimed by local checks.
