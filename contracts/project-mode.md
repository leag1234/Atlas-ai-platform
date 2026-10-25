# Project mode P0 contract

Status: normative interface for task A, 2026-10-21. This freezes the target for
implementation tasks B through N; it does not claim runtime availability,
isolation, quality acceptance or a completed milestone. Source:
[design](../docs/17-project-mode.md), including section 12 owner answers.
The worktree base is the unmerged project-mode-design branch.

The accompanying [OpenAPI 3.1 document](project-mode.openapi.yaml) defines field
names, JSON types, required fields, bounds, closed objects, paths and status
codes. This document defines ownership, transitions and cross-record checks.
Both are normative: schema validation alone cannot authorize an operation.
Examples under `examples/project-mode/` are synthetic contract fixtures, never
runtime defaults. No organization, repository host or model provider is implied.

## Scope and requirements

| Requirement | Frozen interface |
| --- | --- |
| REQ-PROJ-01 | Durable projects; explicit Chat/Work; original files; document work; ledger; organization administration; migration compatibility. Skills, scheduling and device execution disabled. |
| REQ-PROJ-02 | Entities, scope, revisions, retention and locality below. |
| REQ-PROJ-03 | Retrieval owns project metadata and blob publication; no orchestrator database access. PostgreSQL with forced RLS replaces legacy SQLite through explicit migration. |
| REQ-PROJ-04 | Scoped API, authentication, pagination, typed errors, concurrency and chat adapter below. |
| REQ-PROJ-05 | Bounded Work, leases, approvals and checked publication below. |
| REQ-PROJ-06 | Boundary-generated ledger, byte accounting and local-only restrictions below. |
| REQ-PROJ-07 | Membership, grants, quotas, administrative access and policy intersection below. |
| REQ-PROJ-08 | Owned project composer and transcript consume these routes; shell injection grants no authority. |
| REQ-PROJ-09 | Two evaluation levels, mandatory safety and evidence below. |
| REQ-PROJ-10 | Contract-only task A; no implementation of B through N. |

## Entities, ownership and scope

All identifiers are UUID strings except opaque cursors, imported chunk/chat
aliases, content hashes, Git commit hashes and configured identity subjects.
Timestamps are RFC 3339 UTC with `Z`. Revisions are nonnegative integers starting
at zero; imported stale turn context alone may retain `-1`. A project response
uses `id` for its project ID; every child carries `org_id` and `project_id`.
Organization `org_id` equals its `id`. Never trust body identity or scope fields:
creation requests omit them; the server derives them from authenticated scope.

Common entity fields are `id: uuid`, `created_at/updated_at: timestamp`,
`created_by_member_id: uuid`, `revision: integer`,
`deleted_at/purge_at: timestamp|null`. System-created records reference the
installation's provisioned service member. Append-only ledger/progress/audit
payloads use their explicitly defined event fields. Composite keys and foreign
keys enforce `(org_id, project_id, id)` relationships. Organization content
survives a creator's departure. An object from an inaccessible scope returns
404, including when another scope contains an equal-looking name or path.

The tables below identify the authoritative schema and owner; every specific
field and its type is enumerated in that OpenAPI schema, including nested types.

| Schema | Specific fields and meaning | Owner; class |
| --- | --- | --- |
| Organization | name, status, deployment_id, storage_namespace, retention_policy, quota_policy, policy_revision, image_safety_floor, connector_allowlist, skill_allowlist | Deployment admin provisions out of band; organization owns content; minimal A tombstone after purge |
| Member | identity_subject, admin/member role, invited/active/revoked status, joined_at | Organization admin; unique subject within organization; A pseudonym after revocation |
| Project | name, owner_member_id, active/archived/deleting state, default_action, instructions_file_id, memory_revision, deliverables_folder_id, retention_override, policy_revision | Organization; explicit grants; C |
| ProjectGrant | member_id, owner/editor/viewer role | Project owner; ends with membership/project |
| Folder | parent_id, name, server/synced/local_only locality, device_id, opaque device_folder_handle, deny/ask/allow model_use, sync_revision, status, retention_override | Project content; member owns device binding; C server content, local metadata only |
| File | folder_id, relative_path, mime, size_bytes, current version_id, sha256, origin, availability, source_locator, index_status, classification, inherited locality | Project; mutable pointer, C |
| FileVersion | file_id, parent_version_id, mime, size_bytes, sha256, origin, availability, source_locator, index_status | Project; immutable bytes, C; derived extracts/index/previews E |
| Instructions | file_id, version, text, active_version_id, edited_by, effective_revision | Project; editor edits UTF-8 PROJECT.md through the same version store; C |
| MemoryEntry | text, kind, source_conversation_id, source_turn_id, source_file_version_ids, proposed/active/deleted status, confirmed_by, memory_revision | Project; C bounded by source restrictions; never system policy |
| Conversation | title, owner_member_id, last_action, legacy_chat_id | Project; C; conversation creator grants no extra project authority |
| Turn | conversation_id, action, request_id, content_refs, file_version_ids, context_revision, state, usage, persistability | Project; C, metadata only for restricted ephemeral content |
| WorkSession | conversation_id, initiator_member_id, workspace_id, base_file_versions, repository_id, state, plan, effective_limits, usage, lease, last_event_seq, legacy_session_id, pending_approvals, environment | Scoped execution service; C metadata and T scratch |
| Deliverable | file_version_id, session_id, turn_id, source_version_ids, strategy, validation, published_at, visibility, download_route, mime, size_bytes, sha256 | Project reserved deliverables folder; C canonical bytes, E preview |
| LedgerEvent | seq, event_id, timestamp, actor, optional conversation/session, request_id, kind, resource_refs, source/destination, byte_counts, outcome, approval_id, policy_revision, previous_digest, digest, model_dispatch, audit_record | Project boundary outbox; append-only A metadata |
| Approval | session_id, requested_by_member_id, approver_member_id, actor, action_kind/digest, paths, version_refs, destination, maximum_bytes, expires_at, decision, consumed_at, policy_revision | Broker; A, exact scoped permission |
| AuditRecord | approval_id, request_id, actor, phase, action_digest, timestamp, outcome, operation_id, ledger_event_id | Broker; A metadata; never credentials or document bodies |
| Repository | folder_id, approved remote_ref, base_commit, configured forge, connector_id | Project metadata C; credentials remain private broker references |
| Job | kind, status, progress, session_id, purge_stores, backup_expiry_at, legal_hold | Owning service; T payload, A lifecycle outcome |
| SkillReference | catalog_entry_id, digest, version, user/project scope, member_id, enabled=false, approval_state | Organization catalog, lifetime of reference; no P0 loading or execution |

`blob_key` and repository `credential_ref` are internal strings, deliberately
absent from public schemas. Blob keys use IDs, not user path concatenation:
`orgs/{org}/projects/{project}/blobs/{file}/{version}`. Derived data includes the
source version and processor version. Workers mount only the resolved session
scratch directory; they never access canonical blobs or the parent tenant tree.
No cross-organization deduplication is permitted. File paths are relative,
traversal-free and unique within their folder; moves reject collisions and
cross-project or locality changes. MIME and byte hashes are verified from bytes.
Legacy `not_retained` originals have null hashes when unknown, never invented
bytes. Authorized content reads return 404 with an explanatory message when the
original was not retained; extraction and scoped legacy citations still work.

## Retention and locality (REQ-PROJ-02, REQ-PROJ-06)

Owner-accepted defaults: C has no automatic expiry; deletion grace is 30 days;
T expires 24 hours after terminal state; A expires after 90 days; backups expire
within 30 days. E expires with its source and is invalidated immediately on
source deletion/restriction. Superseded originals follow C by default;
`superseded_versions_days=null` means no additional automatic version expiry.
Organization administrators may tighten defaults and configure them within
installation ceilings. Null project/folder override means inherited policy.
Effective expiry is the earliest applicable expiry; a child cannot extend a
parent ceiling. Legal holds are off by default and explicitly visible when set.

Deletion immediately revokes reads, retrieval, context and downloads. Grace is
recoverable storage, never eligibility for use. The deletion job exposes status
for metadata, blobs, derived data, workspace, exports and backups, with an expiry
date and any legal hold. Restore must apply tombstones and expiry before serving
content. After purge, A retains only policy-permitted opaque identifiers and
minimal outcome metadata; labels and content references are removed. Revocation
stops new access without claiming recall from an inference destination.

Files inherit folder locality, never a request-body override. Server and synced
folders retain versioned server bytes; synced does not imply a connected desktop.
P0 local-only folders contain metadata and display `device_unavailable`.
Local inventories report unknown counts with `known=false`, null bytes/files and
null or last-known `as_of`; an absent device is never reported as an empty folder.

Creation uploads (`POST .../files`) require a UUID `folder_id` query parameter.
The authenticated organization/project plus this query selector are the authoritative
destination. `folder_id` is not a FileUpload multipart field; body selectors are
rejected. The destination is authorized before the request body is consumed,
regardless of multipart field order. Version uploads resolve the folder from the
scoped file selector before consuming the body.

P0 MUST reject local-only upload/version bytes (`409 local_only_upload_forbidden`)
before buffering/persisting them, and model/context egress
(`409 local_only_egress_forbidden`) before dispatch. Model-use allow/ask does not
unlock device access in P0. `POST local-reads` returns
`409 local_device_unavailable`; sync-manifests returns
`409 feature_not_available`. Neither can promote local-only into synced storage.
A locality change requires a broker-approved transition plan binding versions,
bytes, destination and policy. No plan endpoint is enabled in P0: an attempted
class change returns `409 locality_transition_requires_plan`, even if an approval
ID is supplied; a same-class rename is ordinary revisioned metadata editing.

The future local-only boundary also covers derived answers, memory and artifacts:
keep them on the device unless the user explicitly promotes selected content.
No persistent server originals, extracts, embeddings, traces, queues, transcript
copies or crash dumps. Future authorized egress requires a memory-only bounded
relay and explicit policy checks. P0 implements neither device reads nor this
relay. Web upload means server storage and must be labelled accordingly.

## Authentication, permissions and policy (REQ-PROJ-04, REQ-PROJ-07)

The public prefix is `/api/atlas/orgs/{org}/projects`; project children use
`.../{project}`. UUID selectors are not authority. Two edge mechanisms map to
one verified identity: the existing host HttpOnly `token` session cookie,
verified by the host session authority; or the existing restricted ingress pair
`X-Atlas-User` plus `X-Atlas-Proxy-Secret`. The latter is a private proxy hop,
not browser header authentication. Strip browser-supplied trusted headers,
verify the proxy secret at the restricted relay, and strip it before forwarding.
The existing `X-Atlas-User-Role: user` controls the host shell only, never an
organization role. No implicit organization from email domain, no self-signup,
and no automatic admin from the first browser login.

Resolve active membership and grants server-side. Forward a newly signed,
short-lived internal identity binding subject/member, org/project, audience,
expiry, membership and policy revisions. Internal credentials are not a public
auth alternative. Deployment signing and key rotation remain operator settings.
Cookie mutations require CSRF protection and same-origin checks; validate Origin
and Fetch Metadata, with a verified session-bound CSRF token where the deployment
permits cross-site origins. Never accept an arbitrary caller JWT or use an
unverified cookie decode. Reauthorize every download, cursor page, SSE replay,
source resolution, job and approval. Logout clears identity-scoped browser IDs.

| Principal | Allowed scope |
| --- | --- |
| Deployment admin | Provision organizations and installation ceilings; map legacy ownership explicitly; no implicit project content access |
| Organization admin | Invite/revoke members, manage role/policy/quotas/connectors/catalogs and audit; content access only through an explicit audited grant |
| Project owner | Manage grants and project state/delete; editor capabilities |
| Project editor | Files, folders, instructions, memory, Chat, Work, repositories and approval requests |
| Project viewer | Read/download, inspect ledger and Chat; may create a conversation and append its Chat turns; cannot Work or mutate project memory/files |
| Bound approver | Decide only the approval addressed to that active member, within their current grant/policy |

Viewer Chat may persist its transcript and usage but cannot auto-activate new
memory. Work cancellation is allowed to the authorized initiator, editor/owner,
or explicitly audited administrative access. Organization admin never implies a
cross-organization bypass. Administrative reads generate `administrative_access`
ledger events. Project grants cannot reference revoked or foreign members. Owner
transfer must retain at least one active owner; self-revocation cannot strand a
project. Archive is read/Chat-only; mutation and Work require active state.

Effective permissions are intersections; quota and cost ceilings are minima;
image safety is the strictest system/organization/project/user choice (`strict`
is more restrictive than `standard`). No setting can weaken the system image
floor, deny-list, isolation or audit requirements. Cache/index keys, vector
search, history aliases, SSE, mounts, approvals and exports carry both scopes.
Use query filters plus FORCE ROW LEVEL SECURITY with a non-owner application
role and transaction-local scope reset for pooled connections. Membership/policy
changes invalidate leases, pending approvals and live subscriptions immediately.

ASSUMPTION: the design does not give numeric organization storage/member/spend
quotas. Installation must supply QuotaPolicy explicitly; no unlimited or vendor
preset is implied. Policy GET reports effective quotas and usage. Reserve actual
storage and expected model cost/concurrency atomically across sessions and
reconcile crashes/deletion. Unknown/failed calls retain cost reservations.

## HTTP, revisions, replay and errors

OpenAPI enumerates all P0 design routes, plus immutable version listing and
reserved refusal routes. Organization provisioning is an operator action, not
an invented public signup endpoint. Lists use `{items,next_cursor}`; default
limit 50, maximum 100. Cursors are opaque, integrity-protected, bound to identity,
scope, filters and a stable snapshot, ordered by `(created_at,id)` or ascending
ledger sequence. Never use a cursor to bypass current authorization. An expired
snapshot returns `410 cursor_expired`; no duplicate/omitted rows within a valid
snapshot. Extraction uses its own bounded `segments,coverage,next_cursor` page.

Every mutation carries `Idempotency-Key`, scoped to member, organization, method
and canonical path, including version/filter selectors. Retain request hash and
original result for at least 24 hours (no content-bearing local-only requests).
Same key and payload replays the original status/body without a second write,
charge or event; different payload returns `409 idempotency_conflict`. Concurrent
first uses serialize. Recheck current authorization before replay. In-progress
operations return their original operation/job reference. Replaying a chat key
attaches to stored events/result; it never restarts a worker. Repository operation
IDs remain durable beyond the key window to reconcile ambiguous remote outcomes.

PUT/PATCH/DELETE and replacement uploads require `If-Match: "N"` and return ETag
where defined. Missing precondition is 428; mismatch is 409 with current_revision
when authorized. Atomically check referenced base_version/base_commit and parent
revision with the mutation. Grant creation uses revision zero for absence;
existing grants begin at one. Increment affected revisions once per successful
mutation. File rename/move uses base_version plus file revision; upload creation
checks expected_parent_revision; instructions use both effective revision and
base_version. Memory erase-all uses memory_revision. Memory edits invalidate
stale context/turn eligibility. Publication from an outdated snapshot conflicts;
no last-write-wins, including two concurrent Work sessions.

Approvals are an intentional exception to generic success replay: any repeated,
expired, revoked or consumed decision returns 410, even with the same key. The
execution broker persists/reconciles operation IDs so an ambiguous side effect
is not retried blindly. Scope authorization always precedes revision/error detail.

| HTTP | Codes and meaning |
| --- | --- |
| 401 | unauthenticated |
| 403 | permission_denied, origin_rejected, policy_denied, approval_required |
| 404 | not_found, including inaccessible identifiers; no tenant existence oracle |
| 409 | revision_conflict, session_busy, idempotency_conflict, selector_conflict, path_conflict, locality_transition_requires_plan, local_device_unavailable, local_only_upload_forbidden, local_only_egress_forbidden, feature_not_available |
| 410 | approval_expired_or_replayed, cursor_expired |
| 413 | size_limit, with measured values |
| 416 | range_not_satisfiable |
| 422 | invalid_request, including unknown fields and malformed paths |
| 428 | precondition_required |
| 429 | quota_exceeded, budget_exceeded, rate_limit_exceeded, with measured values |
| 502 | provider_failure: actual upstream failure, never a policy/budget refusal |
| 503 | dependency_unavailable: unavailable dependency, never exhausted quota |
| 504 | provider_timeout: actual upstream timeout only |

Errors use `{error,status,message,request_id,measurements,provider_status,
provider_ref,provider_outcome,retryable,current_revision,operation_id}`. Every
measurement contains nonnegative `measured,limit,unit`. Size/budget/quota stops
must name the failed resource and measured value/threshold in plain text too.
D5 forbids disguising a quota or budget refusal as provider failure/timeout.
Propagate actual upstream status (including nonstandard quota statuses) and
configured provider reference without credentials. Unknown upstream measurements
are explicitly unknown in message; report the measured local reservation and
ceiling, never fabricate remaining provider credits. Unknown transmission retains
reservations and uses provider_outcome `unknown`; no blind automatic retry.

Once SSE headers are sent, emit the same typed Error within an atlas.project
error/limit event and persist the checked terminal recap; HTTP cannot be changed
retroactively. Return requested usable content when available and accurately
label missing/failed artifacts. No success link precedes durable validated bytes.
Downloads reauthorize, use `Cache-Control: private, no-store`, and support one
bounded byte range (206, Content-Range); invalid/multiple ranges return 416.
Storage keys and temporary unsigned URLs never grant project authority.

## Chat, Work and events (REQ-PROJ-04, REQ-PROJ-05, REQ-PROJ-08)

`POST /v1/chat/completions` retains the existing transport. The project profile
requires model (a configured public profile), org_id, project_id,
conversation_id, the new user message, optional file_version_ids (empty by default), and stream=true. Omitted project_action
means `chat`, regardless of project default_action; the UI uses the default to
preselect an explicit toggle. Never infer Work from code words. `work` requires
an explicitly created/resumed scoped work_session_id. Chat rejects a Work
session selector. Legacy code_mode/code_session_id are accepted only by a
separate migration adapter resolving authenticated aliases; conflicting selectors
fail with selector_conflict, never take precedence. The closed project request
schema deliberately excludes raw legacy selectors.

Resolve membership, conversation, file ownership and effective policy before
attachments, vision, follow-ups or any dispatch. The owned composer uploads
through scoped files, supplies selected immutable file_version_ids and sends the new user turn; canonical history is rebuilt
from authorized server turns. Existing host attachment shapes may enter only
through that same authenticated normalization boundary, never raw documents or
caller-supplied historical assistant turns. Instructions and memory revisions
are snapshotted with retrieved passages and a context manifest of exact source
versions, locators, exclusions and coverage. Stale context_revision=-1 turns stay
inspectable but cannot supply context. Changing action preserves conversation
identity; title generation remains auxiliary and cannot create Work sessions.

Chat uses scoped retrieval; Work uses the existing Code engine with the same
context. Code is a Work capability, not a third action. Start a database lease,
resolve snapshot, reserve budget/ledger intent, plan/read/act/validate, publish
canonical output and outbox atomically, persist checked recap, release lease and
destroy worker. Session states: ready -> running -> awaiting_approval -> running
-> completed/failed; cancel from active states -> cancelling -> cancelled.
Only one active turn per session; a conflict returns session_busy. Leases bind
request/member/expiry/generation; expired leases cannot publish. An explicit new
user turn can resume a terminal session only after versions/policy are rechecked.
Disconnect cancels by default. Replay/reload never implies resumption.

Keep the eight existing tools (list_dir, read_file, search, edit_file, write_file,
run_command, update_plan, request_approval). Structured document interfaces use
file/version/selectors and SourceLocator/Coverage/Segment: PDF pages, DOCX
paragraphs/tables and XLSX sheets/ranges. Formula text and cached values remain
distinct. OCR is a separate authorized model dispatch. Conversion, comparison,
organization and publication must report incomplete coverage or unsupported
fidelity; no fabricated exhaustive comparison or lossless editable-PDF promise.
Disable document macros, external links, automatic formula execution and network.
Binary tools do not loosen text-tool limits. New builder dependencies require
separate review. Document contents remain untrusted data, never policy.

Work ceilings: 40 tools, 41 model calls, 1200 seconds, EUR 3/turn and EUR 0.10
per gateway call. Worker ceilings: 200 MiB expanded bytes, 20000 archive entries,
256 KiB text-tool input, 32 KiB command output, 120 seconds and 512 MiB per command.
Attachment ingress remains 16 MiB/file, extracted text at most 4 Mi characters.
Keep networkless workers, no credentials/host mounts/capabilities, read-only root,
bounded tmpfs/PIDs/CPU/memory and explicit container removal on cancel.
Ordinary Chat retains 120 seconds, file requests 300 seconds and their existing
profile-specific cost rules (file-request ceiling EUR 0.30); Work ceilings must
be shown before send. Any lower organization/project limit wins.

SSE retains ordinary content deltas and adds `event: atlas.project`, `id: seq`,
and JSON ProjectEvent data. Ledger and progress records share one durable,
monotonic per-project sequence allocation (ledger sequences may have gaps).
Replay after/Last-Event-ID returns strictly greater sequences; if both are
supplied they must agree. Deduplicate by project+sequence/event_id, not text.
Retained events are reauthorized; purged replay history returns cursor_expired.
Translate old atlas.code version 1 turn-local sequences into newly persisted
project sequences; never reuse them as project cursors. Preserve tool status,
plan items, approval, diff, limit, honesty and recap semantics. ProgressEvent.output
is a closed ProgressOutput: old output.items maps to items; approval maps to the
full scoped approval; diff maps to diff; limit maps to measurement/detail; recap
maps to text/steps/cost_eur; exported files map to file_version_ids; workspace maps
to environment. Completed tool output is preserved as bounded, redacted JSON text
in output.tool_result; do not infer success from narration. Adapter conversion
happens once before durable sequence allocation; content-bearing
progress is C/T scoped data, not permanently retained A ledger metadata.

## Approvals and publication

The broker creates Approval records and returns them in WorkSession pending
approvals. Bind organization/project, addressed member, optional session, exact
action kind/digest, canonical paths/versions, destination, maximum bytes, policy
revision and expiry (120 seconds initially). action_digest is SHA-256 of canonical
UTF-8 JSON: sorted keys, no insignificant whitespace, no non-finite numbers;
include all effect-bearing fields and scope, never credentials. Different bytes,
base versions, destination or policy require a new approval. Decide once; expire,
revoke or deny without effects. Consumed_at records execution consumption, not
an unexecuted positive decision. ApprovalResult accepted means decision recorded,
not execution success. Pending decisions cannot approve away a policy denial.

Require approval for project/file/memory deletion, overwriting existing content,
locality promotion, external export/write, repository import/branch/commit/PR,
and future local-byte use under ask policy. A new requested deliverable or
scratch edit needs no repeated approval. Instructions replacement is an overwrite
and requires approval_id; activation of user-proposed memory is an explicit edit,
with source restrictions still enforced. A memory status change to deleted also
requires approval_id; PATCH cannot bypass the deletion gate. Direct overwrite/version routes also
require the approval. No optional approval field means implicit permission.

Audit request, decision, execution and outcome, including denial, expiry and
unknown remote result. AuditRecord is linked to the ledger and scoped approval.
Repository actions use one generic configured forge interface: initial adapter
support follows the owner ordering, with no forge default in products/examples.
remote_ref must be an approved credential-free HTTPS reference; connector policy
checks host/path, SSRF and destination. Worker shell has no outgoing credentials.
Expected base_commit and change_digest bind branch/commit/PR parameters. Missing
approval creates a pending Operation carrying the full broker approval; approved execution is
asynchronous. Reconcile operation_id at the remote broker before a retry after
an unknown result. Scratch baseline commits never grant forge authority.

Publish only after artifact validation checks package/readability, requested
content and source coverage. Validation.status must be passed; its hash equals
the canonical version hash. Persist bytes, version pointer and outbox atomically;
a partial failure stays a pending job, never a downloadable success claim.
Strategy created/regenerated/edited_in_place is explicit. Original versions
remain recoverable. Canonical downloads outlive replaceable delivery caches.

## Data ledger (REQ-PROJ-06)

Emit kinds from actual boundaries: folder_registered, locality_changed,
file_uploaded/versioned/read/moved/deleted/purged, extraction_completed,
context_selected, model_dispatch_intent/sent/acknowledged/failed/unknown,
approval_requested/decided, tool_started/completed/failed,
deliverable_created/downloaded, sync_conflict, retention_applied, plus explicit
administrative_access and repository_action. Slash notation expands to the
individual underscore enum names in OpenAPI. Ledger contains no bodies,
credentials or raw tool output. Prefer opaque local handles; hide sensitive
labels. Readers need current project access; audit exports require audited admin
authorization and broker approval before leaving the project.

Persist dispatch intent before I/O. ModelDispatch records call_id and attempt,
provider_ref, purpose (generation/embedding/rerank/ocr/vision), source version
IDs/locators, instructions/memory revisions, source_bytes, selected_utf8_bytes,
image_bytes, serialized_payload_bytes, response_bytes, tokens and estimate flag.
Every retry is a new attempt; repeat history bytes count each time. `source_bytes`
counts original referenced ranges; selected bytes count extracts actually used;
serialized payload counts the complete application request before TLS, including
encoding/history. Network overhead is excluded. These are distinct quantities.
Model lifecycle events require model_dispatch; its byte_counts equal the event's.
Unknown crash/timeout becomes `possibly_sent`, not zero; an acknowledgement is
not evidence of a provider's retention/deletion policy.

Summary payload_bytes sums each dispatch attempt once, not once per lifecycle
event. unique_source_bytes deduplicates selected original ranges by version;
model calls include possibly-sent attempts with their separately visible count.
Server/produced totals deduplicate retained versions; session filters restrict
transmissions without changing project-wide identity semantics. as_of_seq gives
a consistent snapshot for the Local/On server/Sent to model/Produced panels.

Hash-chain digest = SHA-256 of canonical event JSON with digest omitted and
previous_digest included; the first event has previous_digest=null. Maintain a
trusted external checkpoint to detect changes. This is not proof against a
privileged operator. Retention removes sensitive fields via an audited purge and
checkpoint transition, not silent chain rewriting. Transactional outbox retries
reuse event_id; clients and consumers never double-count publication.

## Evaluation (REQ-PROJ-09)

Owner answers override the draft's single gate. Report both levels on every
measurement, including when neither passes:

| Level | Required result |
| --- | --- |
| Internal preview | At least 15/30 first-attempt solves; all mandatory gates below; zero false success claims |
| General availability target | At least 27/30 first-attempt solves and at least 5/6 in each EN/FR/DE/ES/IT group; all mandatory gates below |

Mandatory gates for both: D25 through D30 all pass, zero cross-organization or
local-only leaks, no false success claims, every run within declared budgets,
and independent integration evidence for RLS, migration round-trip, quotas,
image-policy monotonicity and forge approval/replay. Missing/timeout cases are
failures. Preview does not require the GA per-language minimum, but every
language score and the GA gap must still be reported. The owner reviews the gap
before wider rollout; a preview pass is never described as GA acceptance.

EvaluationTask freezes input_manifest (relative paths, bytes, hashes),
expected_artifacts (path, MIME, content checks), allowed_effects, language,
effective limits, verifier timeout/reference and deterministic base-archive hash.
Expected answers and known-good outputs stay withheld until execution finishes;
the public task manifest is not an answer oracle.

EvaluationReport contains exactly D01 through D30 once, six cases per language,
first-attempt outcomes, artifacts/hashes, verifier exits, bytes, latency, tool/model
calls, cost, ledger reconciliation, both thresholds and both computed results.
Safety_passed is computed from D25-D30 and independent checks, not a caller's
unsupported success flag. preview_passed and ga_target_met are derived from the
rules above. Offline recordings prove determinism, not live model quality; a
live report needs separately authorized live execution. No measurement is
performed by task A and no green CI claim is made here.

Fixtures/verifiers and withheld known-good outputs require owner review before
measurement. Inspect artifact contents/render bounds, not names or routing.
Retain the design's thirty task definitions, language balance and variant coverage;
do not tune prompts, corpus, assertions or thresholds through this contract task.
Future acceptance needs its owner-approved CI gate and green run link.

## Changes from today

CONTRADICTION: intentional changes below follow the owner-approved P0 design;
they are requirements for later implementation, not current capabilities.

| Existing source/behavior | Contract change |
| --- | --- |
| retrieval/projects.py: SQLite projects without organization membership; project-scoped facts/history | Explicit org/member/grant scope, forced RLS and migration; retain UUIDs, memory revisions and stale -1 turn semantics |
| retrieval/project_api.py: unpaginated lists, `{id}`/`{ok}` responses, name-based conversation create, origin checks only | Authenticated scoped prefix, title field, typed entities/pages/errors, idempotency, ETags and approval enforcement; legacy aliases require authorization |
| project instructions string and extracted-text document store | Versioned PROJECT.md and immutable original bytes; migrated legacy_extracted availability is not_retained, never fabricated originals |
| existing facts corrected/erased with revision increment | Editable scoped MemoryEntry lifecycle; retain stale-context invalidation, source restrictions and no policy injection |
| project_chat.py: server history and memory correction | Preserve reconstruction, add scope/action/context manifest before every attachment/vision/follow-up path |
| code_mode.py / orchestrator README: owner-only Code selector, process-local active set, turn-local atlas.code sequence | Explicit Work in the same conversation, persistent scoped lease, durable project SSE sequence and replay without automatic resume |
| code_api.py: opaque turn-local approval and status/audit paths | Scoped digest-bound broker approval, 120-second expiry, version/policy invalidation and durable audit/operation reconciliation |
| code_ui.py: owner relay strips Authorization/Cookie | Authenticated project BFF verifies session/private ingress and issues scoped internal identity; old relay cannot authorize project work |
| Code workers and limits | Preserve containment and ceilings; add bounded binary document tools, no implicit network or credential access |
| terminal delivery URLs and temporary exports | Canonical project versions and reauthorized downloads including migrated aliases; no unsigned URL authorization bypass |
| original PoC scope excludes long-term memory and hardened tenancy | Explicit P0 extension under owner mandate; prior completed gates are unchanged |

Migration must preserve ordered turns, facts, exact instructions, chunk IDs/text,
embeddings, code workspaces/baselines/audit and available browser history with
client_import provenance. Legacy ownership is selected by the installing system
administrator, not a product preset. Quiesce writers, snapshot/checksum, use a
resumable manifest, rehearse restore, verify counts/IDs/bytes/citations/downloads,
then switch aliases. Unknown ownership or missing state is quarantined and
reported. Never discard P0 writes on rollback; legacy originals remain read-only
until verified acceptance and approved expiry. No re-embedding is required.

## Approved identity-resolution amendment (task F)

The orchestrator calls retrieval's private `POST /internal/atlas/identity` after
verifying the host session or trusted front-door identity. The closed request
contains `subject`, `org` and `project`; the latter two are nullable UUID selectors.
A project requires an organization. No caller may supply a member, role, revision,
expiry or audience. Retrieval returns current active `memberships` in the existing
Membership shape. Without an organization, `identity` and `expires` are null.
With an organization, retrieval revalidates membership, current organization policy
and any selected project's grant, then returns `identity` and its Unix `expires`.
The signed claims are exactly ScopedIdentity: subject, member, org, project,
membership_revision, policy_revision, expires, audience. Lifetime is 60 seconds
(with the existing verifier's maximum 120 seconds); audience is
`atlas-retrieval-projects`. Existing scoped routes recheck current revisions.
Unknown/revoked subjects return an empty membership list for discovery; an
inaccessible selection returns `404 not_found` without disclosing its existence.

Only the orchestrator holds the environment-provided
`ATLAS_PROJECT_IDENTITY_SECRET` (at least 32 bytes), also configured in retrieval.
The endpoint requires `X-Atlas-Identity-Secret`, constant-time comparison and no
browser Origin/Fetch Metadata. It is not exposed by the public relay. It never
accepts session cookies, front-door credentials or existing internal identities
as authentication. Retrieval alone holds `ATLAS_PROJECT_SIGNING_KEY` and owns
membership lookup; the orchestrator never opens its database. A narrow SQL
SECURITY DEFINER lookup exposes only the asserted subject's active memberships
and revisions to the retrieval application role; all selected project access
still uses the existing forced RLS scope. Install/update this SQL explicitly,
never through application startup. No credential or subject is logged.

`Idempotency-Key` is required for request correlation. Resolution has no mutation
or cost reservation to replay; retries resolve current state and mint a fresh
credential. Responses use `Cache-Control: private, no-store` and the existing
Error schemas (401 authentication, 403 browser origin, 404 inaccessible selection,
422 malformed input, 503 missing configuration/dependency). Scoped resource
credentials cannot discover the broker and receive 404; missing credentials
receive 401 even when the broker is unconfigured. The BFF never returns
the credential or expiry to the browser; `/api/atlas/me` retains the Me schema.

Set `ATLAS_PROJECT_MODE=1` on the orchestrator/front relay to enable project gates;
a configured `ATLAS_PROJECT_DSN` also enables them for migration compatibility.
`ATLAS_TRUSTED_EMAIL_HEADER` names the front door's verified subject header
(default `X-Atlas-User`); it is trusted only with `ATLAS_PROXY_SECRET` on the private
hop. The host `token` cookie is verified via the pinned host's `/api/v1/auths/`
endpoint at the configured internal `ATLAS_WEBUI_URL` (or the local host-service
port). No unverified token decoding or caller Authorization passthrough occurs.
State-changing public routes require an exact configured `ATLAS_PROJECT_ORIGIN`
(or direct request origin) match and same-origin Fetch Metadata when present.
Cross-site mutations are unsupported and fail `403 origin_rejected`.

In project mode, legacy `/projects`, `/code`, `/atlas-code`, terminal browse/serve,
host-file APIs (including extracted content) and generated image/SVG routes fail
closed with contract errors. Use scoped file-content and deliverable routes, which reauthorize each request and
retain the retrieval service's policy, ETag, range and download headers. Later
Chat/Work tasks own scoped execution; the legacy owner relay cannot bypass it.

## Approved execution-broker amendment (2026-10-22)

REQ-PROJ-05: retrieval remains the only authority for reservations, leases,
approvals and durable project sequences. The orchestrator never opens its database.
The five private POST routes under `/internal/atlas/execution/` are `acquire`,
`turn`, `approvals`, `decisions` and `events`. Their closed schemas are normative.
No public permission or existing public response shape changes.

Every call requires `X-Atlas-Execution-Secret`, configured separately as
`ATLAS_PROJECT_EXECUTION_SECRET` (at least 32 bytes) in both services, compared in
constant time, and `X-Atlas-Execution-Audience: atlas-retrieval-execution`.
It also requires a fresh retrieval-issued `X-Atlas-Project-Identity` with audience
`atlas-retrieval-projects` and a non-null project. Resolve that identity afresh
through the approved identity broker. Neither credential alone, a browser cookie,
front-door header nor a caller-selected role grants execution authority.
Reject Origin/Fetch Metadata and never expose these paths through the public BFF.
Authenticate and authorize scope before reading a bounded 160000-byte JSON body.
Every call carries Idempotency-Key; authorization and fencing precede replay.

`acquire` checks expected session revision, current editor/owner grant, active
project, selected folders and exact current base versions. Atomically reserve one
concurrent job, the effective turn cost against daily/monthly quotas and requested
storage bytes, then issue a 30-second lease with an increasing generation. The
lease never extends the turn's effective wall deadline. Competing or expired,
unreconciled leases return `409 session_busy`. A request ID cannot acquire twice.
Return ExecutionState with the unchanged WorkSession and effective limits.

`turn` binds session, request and generation. `check` reauthorizes current grants,
policy, folders and versions; `renew` also extends the lease, bounded by the wall
deadline. Changed authority fails closed. `recover` is available to a current
editor/owner after expiry, cancellation or invalidated authority: compare the old
generation, advance it and transfer cleanup ownership to the new request/member.
Return `cleanup_required=true`, state cancelling; this authority permits cleanup
and release only, never execution or publication. It does not resume work.
The original request retains its reservations until cleanup is acknowledged.
A second recover must wait for the cleanup lease to expire.

`release` requires `cleanup_confirmed=true`, a terminal state and the exact current
fence. Cleanup removes generation-labelled containers and the scoped scratch
workspace before releasing concurrency/storage reservations. Terminal usage
reconciles actual cost; null usage retains the entire model reservation as unknown.
An unknown portion remains charged, never silently released on error, disconnect
or restart. Release permits cleanup after base/policy invalidation, but still needs
current scope access; after membership revocation an authorized editor performs
recovery. Stale holders cannot release, emit events or consume session approvals.
Cancellation marks the session cancelling and invalidates execution immediately;
the worker observes this on its next check/heartbeat and cleans up. It never means
that a still-running worker has already stopped. Cleanup failure retains the fence
and reservation for explicit recovery.

`approvals` checks the execution fence and addressed active editor/owner. Derive
paths and versions from the canonical effects, validate current versions/revisions,
destination and maximum bytes, and hash with the existing canonical file-mutation
rule (scope, executing member, policy revision, action kind and exact effects).
Store a pending Approval in retrieval's canonical file approval store, expiring
120 seconds after issuance. The issuer cannot submit a decision or digest.
The closed effect variants cover existing overwrite, file deletion and memory
deletion transactions. Other action kinds remain policy/feature denied until
their separately contracted executor exists; this amendment grants no connector,
local-byte or repository capability. Destination must be the selected server
project and the byte ceiling must cover the exact effect. No approval can weaken
locality, model-use, worker or quota policy.

`decisions` and the existing public approval decision route perform the same
transaction: require the currently addressed member, exact digest, live execution
fence, unchanged versions/policy and unexpired pending approval. Record the decision
once; `consumed_at` stays null until actual canonical execution. Every repeated,
expired, revoked, invalidated or consumed decision returns
`410 approval_expired_or_replayed`, including repeated Idempotency-Key. A different
digest returns `403 approval_required` with no decision. Public canonical mutations
also check the associated live lease before consuming the approval.

`events` accepts bounded existing ProgressEvent/AuditRecord payloads under a live
execution fence. Scope, member, request, session and approval links must agree with
server authority. Retrieval replaces caller sequence/timestamps/link identifiers
with authoritative values, allocates sequences from the existing ledger allocator,
and persists progress plus audit ledger/outbox in the same transaction. Event IDs
and Idempotency-Key prevent duplicate publication. Progress remains scoped C/T
content; only bounded metadata enters the audit ledger. Broker issuance, decision,
expiry/denial and execution outcomes are audited at their actual boundaries.

Errors retain Error and D5 semantics: missing service authentication is 401;
scoped identity without the service credential is 404; inaccessible scope is 404;
viewer/policy denial is 403; stale generation, expiry or competing execution is
409 session_busy; revision mismatch is 409 revision_conflict; repeated approval
is 410; malformed/unknown fields are 422; oversized body is 413; quota/budget
refusal is 429 with measured values; unavailable storage is 503. No automatic retry
of an ambiguous effect. Installation uses an explicit, idempotent operator migration
with forced RLS and non-owner application privileges, never startup schema changes.

## Approved atomic organization amendment (2026-10-22)

REQ-PROJ-03/05: `POST /internal/atlas/execution/organize` is a private,
retrieval-owned execution operation. It reuses the execution broker's service
credential, scoped identity, audience, ingress restrictions, 160000-byte body
limit and Idempotency-Key. It is never exposed through the public BFF and changes
no public schema. Authorization and the session_id/request_id/generation lease
fence precede replay and execution.

ExecutionOrganize has mode `preview` or `apply` and 1–100 OrganizationOperation
items. Each binds file_id, immutable base_version, expected_revision, destination
folder_id and relative path. Selectors and destinations are normalized before
checking uniqueness. Duplicate file selectors or final (folder_id,path) pairs
are invalid. Reject occupied destinations, including other selected files: no
swaps in P0. An unchanged destination for the same file is permitted. Sources
must belong to the session's selected base versions; source and destination
folders must be selected by that session and satisfy current Work policy.
Reserved PROJECT.md cannot be moved. Traversal and locality changes are refused.

Preview checks current scope, role, policy, lease, all versions and revisions,
paths, locality and collisions, returning normalized operations and an empty files
array. It mutates nothing. Apply repeats every check in one authorized retrieval
transaction, then commits all moves, file and folder revisions, context/policy
invalidations, file_moved ledger/outbox events and idempotency result together or
none. Immutable original versions and bytes are preserved. Apply returns the
normalized operations and resulting File entities in operation order. A preview
is not a reservation or permission to omit apply checks.

Replay with the same key and body returns the recorded result without repeating
moves, revisions, invalidations or events, after current authorization and fence
checks. Reusing a key for a different body is a conflict. No automatic retry of an
ambiguous effect. Error uses the existing semantics: 401 missing service auth;
404 inaccessible scope or scoped identity without service auth; 403 role/policy
refusal; 409 stale lease/version/revision, occupied path or locality conflict;
422 malformed fields, duplicate selectors/destinations or invalid paths; 413 body
or operation count exceeded, with measured value, limit and unit; 429 quota;
503 unavailable storage. Counts over 100 use 413 before schema validation.

## Approved repository-action amendment (2026-10-22)

REQ-PROJ-05/06: the supervisor authorizes two additions to CommitAction only:
`target_branch` and `snapshot: {id, sha256}`. The snapshot is an immutable artifact
stored by retrieval, identified by its file-version UUID and SHA-256 of its bytes.
The broker resolves and verifies it outside the worker. Existing public fields
retain their meaning; no credential, tree bytes or private operation state enters
the public action. The approval digest covers the entire action (except its
approval_id), repository identity/configuration, scope, policy, executing member,
base_commit, target_branch, snapshot ID/digest and message. change_digest remains
the caller's digest of the proposed change; it never substitutes for snapshot
verification or approval. Any effect-bearing change requires another approval.

The initial snapshot encoding is canonical UTF-8 JSON (sorted keys, compact
separators): `{"files":[{"content":"<base64>","mode":"100644","path":"a.txt"}]}`.
Entries are sorted by path, unique, relative, traversal-free; modes are 100644 or
100755. Content is strict canonical base64. This is the complete resulting tree,
including deletions by omission; symlinks, submodules and .git paths are rejected.
The artifact and all containing folders must remain available and permitted.
Use the existing file upload/version store; the broker checks its authoritative
size/hash and inherited locality before export. No worker receives forge tokens.

The private adapter interface has read-only base validation and reconciliation,
and one approved-effect execution operation. Retrieval owns durable operation
IDs, approval consumption, states and audit records. It commits intent and the
unique operation ID before remote writes. Only the configured connector may
resolve credentials; approved credential-free remote_ref must exactly match its
configured repository and allowed host/path. No product forge default exists.
GitHub is the first adapter. Tokens exist only in the executing broker process,
never the worker, public object, ledger, logs, exception message or traceback.

Each branch uses its exact approved branch name as remote identity. Commits append
`Atlas-Operation: <operation_id>` as a trailer; PR bodies append
`<!-- Atlas-Operation: <operation_id> -->`. These broker-owned markers are part of
this approved execution convention; caller-supplied marker lines are refused.
A commit preserves the approved message plus that trailer, has exactly the
approved base as its sole parent and the snapshot tree, and advances the explicit
branch only from that base. A PR binds its approved head and target refs and their
resolved commits at issuance. The expected base is the target commit. Existing
or moved targets cannot be silently adopted, overwritten or force-pushed.

Before any retry of an ambiguous send, reconcile by remote reads: exact branch
and base; commit marker, parent/tree and branch ancestry; PR marker, parameters
and bound refs. Persist intermediate commit identity before updating the ref.
Reconciliation can return succeeded, absent, conflict or unknown. Absence must
be proved; failed/incomplete/paginated-incomplete reads remain unknown. An
unprovable commit-creation result remains unknown (unreferenced GitHub commits
cannot reliably be enumerated). Never blindly resend that creation. Deterministic
blob/tree preparation may be repeated; creating an effect may not. Conflicts
fail closed. The operation is durable across broker restart and has one approval;
retrying the public approved action or decision returns 410. A private recovery
runner resumes the stored operation, never reuses the public approval.

The existing repository action POST returns pending Operation with full Approval
when approval_id is absent. Its decision uses the existing approval route, exact
digest, current scope/grants/policy, and 120-second expiry. An approved action
POST atomically consumes that approval and queues the operation, returning 202;
execution is asynchronous. Execution and recovery reauthorize before effects.
Audits record request, decision, execution and outcome including unknown results,
with operation_id. Revocation or changed source/base/configuration denies further
writes. Imported repository registration remains a separately approved capability.

## Approved deliverable publication amendment (2026-10-22)

REQ-PROJ-03/05/06: `POST /internal/atlas/execution/publish` is a private
retrieval-owned operation using the existing ExecutionBroker service credential,
audience, fresh scoped identity, ingress restrictions and Idempotency-Key.
It changes no existing public schema. Authorization of the active project,
editor/owner and server-derived reserved deliverables folder precedes body reads.
Publication requires the current session/request/generation fence, including
current membership, policy, selected folders and exact current base versions.
Cleanup authority cannot publish. Scope, member, canonical destination and blob
keys are always server-derived; caller authority/destination fields are invalid.

The closed multipart ExecutionPublication contains exactly one JSON `metadata`
part (ExecutionPublish), one binary `file` and an optional binary `preview`.
Metadata is bounded to 160000 UTF-8 bytes. Artifact bytes are bounded by the
effective upload and storage ceilings; multipart framing is bounded to 65536
bytes in addition to metadata, artifact and preview limits. Duplicate, unknown,
nested, missing or malformed parts return `422 invalid_request`. File MIME is
derived from bytes and must agree with the requested DOCX/XLSX/PPTX/PDF suffix;
filenames and part Content-Type cannot override it. Empty/unreadable packages,
missing/failed verification or mismatched hashes return `422 invalid_request`.
Byte excess returns `413 size_limit`; storage excess returns `429 quota_exceeded`,
with measured values and limits. No refusal publishes partial content.

Metadata carries session_id, request_id, positive generation, turn_id, relative
path, expected_parent_revision, unique source_version_ids, strategy and Validation.
The execution request identifies the publication turn: turn_id must equal that
request_id and be bound to this live session's conversation. All declared source
versions must be selected session base versions. Retrieval rechecks their current
pointers, availability, locality and model-use policy as well as the folder
revision. Existing destination paths conflict; this operation creates a new
canonical file and cannot overwrite existing content or bypass approval.
The strategy describes its derivation, including editing a source in scratch;
original source versions remain recoverable.

Validation must have status `passed`, verifier_exit `0`, and checks including
`package_readable`, `requested_content`, `source_coverage`. Only the bounded
trusted verifier can construct this result after reopening the actual artifact
with the format library, checking the expected content and declared source
coverage and hashing the exact bytes. Model tool arguments, model prose and
worker-supplied success assertions are not verification provenance. The private
broker sends the verifier result, never forwards caller-supplied Validation.
Retrieval checks hash equality against actual canonical bytes, size, MIME and
package readability again before committing. The authenticated broker is the
trusted provenance boundary; its credentials never enter model or worker input.

The optional preview is a derived UTF-8 plain-text or PDF representation, bounded
to 1048576 bytes and counted against effective storage ceilings. It is produced
by the verifier from the reopened artifact, never caller HTML or an independent
source. Retrieval binds it to the new canonical source version and a server-owned
processor version, retains it as class E, and rechecks its MIME/readability.
`GET /api/atlas/orgs/{org}/projects/{project}/deliverables/{deliverable}/preview`
returns authenticated `text/plain` or `application/pdf` bytes with private/no-store
and nosniff headers. Every read rechecks source and deliverable authorization,
retention, deletion, restrictions and source linkage. A missing/expired preview
returns `404 not_found`; callers may still download the canonical artifact.
Regeneration must repeat source authorization and bounded verification. A preview
never becomes class-C content or extends its source's retention.

Return `201` with the unchanged Deliverable schema only after immutable canonical
bytes, version, file pointer, Deliverable, preview metadata and deliverable_created
ledger/outbox are committed atomically. Database failure removes staged blobs;
uncommitted blobs are never downloadable. Canonical downloads outlive scratch and
export-cache expiry. Same key and package replays the original status/body without
another version, charge or event, after current authorization, live fencing and
source restrictions. Different payload conflicts. Stale generation/lease is
`409 session_busy`, stale version/folder revision is `409 revision_conflict`,
occupied path is `409 path_conflict`; other errors retain existing Error semantics.

## Amendment 2026-10-24 (private transcript interface), approved by the supervisor

REQ-PROJ-03/04/07: three private POST routes; no existing public request or response schema changes.

1. `/internal/atlas/transcript/begin`
   - Closed request `TranscriptBegin`, all fields required:
     `request_id: UUID`, `request: ProjectChatRequest` (existing schema).
   - Closed response `TranscriptState`, all fields required:
     `turn: Turn`, `context: Context`, `replay: boolean`,
     `events: array<ProjectEvent>` (maximum 100), `last_event_seq: integer >= 0`.
   - First use returns 201 and atomically creates a running canonical turn with
     the question, exact selected versions, action and current context revision.
     Resolve scope, conversation, selected files, source restrictions and policy
     before context selection. Return canonical authorized history in Context.
     Work additionally checks the selected session belongs to the conversation.
   - Scope/member/key plus the canonical request hash identify a single request.
     Same key/body returns 200 with the existing turn and replay=true, even if a
     caller generated another request_id; the original server binding wins.
     Different body returns 409 idempotency_conflict. Replays never dispatch.
     Further replay uses existing project events with after/Last-Event-ID.

2. `/internal/atlas/transcript/finish`
   - Closed request `TranscriptFinish`, all fields required:
     `request_id: UUID`, `conversation_id: UUID`,
     `state: completed|failed|cancelled`, `content_refs: array<ContentRef>`,
     `usage: Usage`, `error: Error|null` (all referenced schemas already exist).
   - Response 200 `Turn` (existing schema).
   - Resolve the running turn by authenticated scope, conversation and request;
     derive IDs, action, creator, selected versions, persistability and context
     revision from the stored begin operation. Only the initiating member's
     authenticated service request may finish that turn. Enforce source
     restrictions again and retain unknown cost reservations. Never permit an
     arbitrary caller to replace question content or another member's turn.
   - Persist checked answer, terminal state/usage and one terminal ProgressEvent
     plus its audit/outbox atomically. Duplicate identical finish replays the
     original result; conflicting finish fails with idempotency_conflict.
     Stale context remains inspectable but is excluded from future context.

3. `/internal/atlas/transcript/events`
   - Closed request `TranscriptEvents`, all fields required:
     `conversation_id: UUID`, `request_id: UUID`,
     `events: array<ProjectEvent>` (minimum 1, maximum 40).
   - Closed response `TranscriptEventsResult`, required
     `events: array<ProjectEvent>` (minimum 1, maximum 40).
   - Bind events to the running turn and current authority. Replace caller
     sequence, scope and link fields with server authority; reject inconsistent
     links. Allocate from the existing shared project sequence and persist
     content-bearing progress separately from metadata-only audit/outbox.
     Support actual Chat model-dispatch intent/outcome events, checked errors,
     and recaps; never infer transmissions from narration. Work tool/approval
     events remain on the existing fenced execution broker.

All three routes reuse the existing execution service credential and audience
plus a freshly resolved scoped identity, forbid browser Origin/Fetch Metadata,
require Idempotency-Key and a 160000-byte bounded JSON body, and are excluded
from the public BFF. Reauthorize before replay and before accepting content.
Use existing typed Error semantics, including measured size/quota refusals.
Chat permits current viewer/editor/owner, including archived read/Chat projects;
Work requires editor/owner, active project and the existing execution lease for
execution. No viewer acquires Work authority. Organization/project/member fields
cannot be caller authority. Retention, FORCE RLS and source invalidation apply.
Chat reserves its existing effective per-request cost against organization quotas
at begin; Work keeps its existing lease reservations without double charging.
An ambiguous/crashed dispatch retains its reservation and never auto-resumes.

## Amendment 2026-10-24 (slide locators), approved by the supervisor

PPTX extraction uses SourceLocator {kind: slide, slide: N}, with N the
one-based slide position in presentation order. Coverage.unit is slides;
selected_units counts distinct indexed slides and total_units is the source
slide count, or null when it cannot be determined safely. Speaker notes are
associated with their slide. Coverage.complete is false for omitted or
truncated content; warnings identify affected slides and reasons. The same
slide locator is preserved in extraction segments, indexed sources, Chat
citations and ledger source references. Retrieval retains ownership of
canonical storage and the index; this amendment grants no database access,
network access, OCR dispatch authority or worker storage access.

## Amendment 2026-10-25 (organization names), approved by the supervisor

REQ-PROJ-04/08: organization and membership responses expose the stored organization
`name` as read-only display metadata. It grants no authority and never replaces
UUID scope selectors. Existing organization storage already contains this field.

## Scoped model approval adapter

REQ-PROJ-05: the model-facing approval tool accepts a flat `effect_kind` selector
(overwrite, delete_file, delete_memory) and the exact fields for that effect.
The orchestrator deterministically maps it to the unchanged strict ScopedApproval
and execution broker effects. Unknown, irrelevant, missing or mistyped fields
are rejected before broker calls; authority and exact-effect validation remain
server-owned. Existing strict internal arguments remain compatible.
A Work loop stop or unresolved tool validation failure produces a failed session,
turn and recap with the reason; returning text alone never implies completion.

## L7 owner ruling: model document input and terminal readback

REQ-PROJ-05/08: `publish_document` offers Markdown `content` for DOCX/PDF/XLSX
(headings, paragraphs, lists, simple pipe tables), or `title` and `slides`
(title/bullets, optional notes/table) for PPTX. The server maps these inputs
into the existing strict Content model; existing structured objects and JSON
strings encoding that model remain accepted. Invalid, ambiguous and oversized
inputs fail before publication. Format, source coverage, permissions, package
verification and publication schemas are unchanged.

Cancellation uses the current Work lease request_id. Recovery retains the
original reservation_id as the turn's usage attribution. Failed/cancelled
terminal transcripts retain their reason in answer content and authoritative
usage, including unknown reservations, for reload.

Final transcript persistence is shielded from browser disconnect. On reload,
the original creator may reconcile a still-running Work turn through the
existing authorized finish boundary only when its session is cancelled and
the released lease retains the matching reservation_id. This never resumes
inference or reconciles an unrelated execution.

## Amendment 2026-10-25 (context telemetry / tool-result references), approved by the supervisor

REQ-PROJ-04/05/06/08/09: additive P1a context measurement. Usage and ModelDispatch
may include `context` (ContextUsage). Public Chat response metadata exposes
`context` and `context_samples`, including failed dispatch attempts. Each sample
reports the active gateway-declared model window, estimated input tokens split
into system, project_instructions, pinned_memory, tool_declarations, session_state,
recent_turns, tool_results and document_passages. stable_prefix_tokens is the sum
of the first four layers; total_tokens is the sum of all layers. The estimation
method is `utf8_bytes_div_4_ceil_per_component`; provider_input_tokens is separate
and nullable, never an attributed provider breakdown. No thresholds, routing,
content order or budgets change in measurement. Context-only estimates exclude
image bytes and are not a billing or exact provider-token claim. The right panel
shows Context used, the split, window and estimation method for the latest call.
Work model events use the same private transcript event authorization and additionally
require the active Work session/request lease. Existing clients may omit the new
optional fields. D31-D33 are separate recorded-only long-session tasks; D01-D30
and their gates remain unchanged. Synthetic verifier controls are not recordings.

The additive `/internal/atlas/transcript/work-events` route accepts closed
WorkTranscriptEvents (conversation_id, request_id, generation, events), uses the
existing transcript broker authentication and response, and requires the current
Work fence. The original TranscriptEvents envelope is unchanged.

Two additional private routes, `result-put` and `result-open`, use the same
authentication, scope, origin denial and Idempotency-Key. All fields in their
closed schemas are required; generation is null for Chat and the current lease
generation for Work. A result ID is `origin_turn_uuid:result_uuid`. Bodies are
canonical turn-private content, retained and revoked with that turn and its
source versions; they never enter metadata-only ledger events. Storage charges
actual UTF-8 bytes to the existing stored_bytes quota. Put accepts at most
1048576 UTF-8 content bytes (8388608-byte JSON ingress cap for escaped content);
open retains the 160000-byte ingress cap and returns at most 8192 Unicode
characters, using zero-based half-open ranges. No filesystem path is accepted.
Reopen rechecks the requesting turn, original turn's sources, conversation,
project, membership and identical Work session (or Chat conversation); it never
replays cached content before authorization. Store replay is idempotent after
authorization and the current Work fence. Unknown/inaccessible references are
404; invalid ranges 422; stale leases 409; quota 429 and sizes 413 with values.

Every executed tool result is stored before entering context. Continuously keep
the last ATLAS_CONTEXT_KEEP_RESULTS complete eligible bodies (default 4, 0-100)
in the active loop. Older bodies become a two-line deterministic description
and reopen_result reference. Original content is unchanged in storage. Approval,
plan and group-selection results, user messages, assistant decisions and stable
prefix are excluded from clearing. Each clearing emits context_selected with
optional ContextClearing metadata (ids, estimated tokens_removed/tokens_added,
method), atomically with storage, and is displayed in the right panel.
No structured state summarization or history compaction is added in P1a.

Work offers reopen_result as a core tool alongside the existing three core tools,
with four group slots. The additive optional switch_tool_group.focus field selects
a group's tool into those slots when needed; every existing group capability
remains reachable and the gateway's eight-tool capacity is unchanged.

## Amendment 2026-10-25 (session state), approved by the supervisor

REQ-PROJ-04/05/06/08/09: retrieval owns a typed SessionState for each conversation
and, separately, each Work session. Decisions, established facts, open questions,
files read/produced and current plan retain source and originating turn. State is
derived content, never project instructions or activated memory. It inherits all
source restrictions and revision invalidation of the represented turns.

Private transcript `state-read` and `state-write` reuse transcript authentication,
origin denial, bounded ingress, current scope/source checks and Idempotency-Key.
They bind to the initiating member's completed turn. State-write compares the
previous state revision and the exact covered turn IDs; stale concurrent updates
fail 409. Retrieval validates the closed schema and provenance before atomically
persisting state in the turn's private content, with storage quota accounting.
No new database authority or startup migration is required. Context optionally
returns session_state and represented_turn_ids; ContextRequest optionally selects
work_session_id, checked against its conversation. Updates run after terminal
persistence in a background task, one bounded gateway call with the fast public
profile, outside the answer path. Failure logs metadata only and preserves state.

Merge is keyed by entry ID within each category. Missing entries are retained;
explicit replacements preserve their new source/turn. Open-question resolution
and plan replacement are explicit fields; facts and decisions cannot disappear
through omission. Invalid schema/provenance or an oversized merged state rejects
the whole update. Constraints are excluded from generated state authority:
project instructions and every active constraint are reread verbatim each turn.

Assembly is system, project instructions, pinned memory and tool declarations
(stable, never compacted), state, recent verbatim turns, tool results and document
passages. ATLAS_CONTEXT_RECENT_TURNS defaults to 8; ATLAS_CONTEXT_RECENT_TOKENS to
20000. Only turns covered by persisted state may be removed. At 60% of the active
model window remove represented older turns; retain the recent configured window.
At 80% display a warning; at 95% refuse with measured tokens/window and propose a
new session starting from the state. Apply thresholds to the complete dispatch,
including tools. Preserve the P1a raw estimate and add a calibrated count (initial
4/3 correction; update from provider observations by provider family). Exact
provider counts take precedence for their observed payload; changed payloads use
the calibrated estimate. Each removal is a context_selected ledger event with
ContextCompaction metadata, visible under Context used. No transcript is deleted.
The right panel displays state read-only; Edit is a disabled step-3 placeholder.

State update dispatches use additive `state-events` (TranscriptEvents envelope),
restricted to the initiating member's completed turn and its state reservation.
They retain the original source checks but grant no Work execution lease authority.
Reserve at most EUR 0.01 from the turn's remaining ceiling before inference;
reconcile acknowledged usage on state-write and retain unknown reservations on
failure. A Work snapshot inherits the authorized conversation history and is
also eligible as its conversation's snapshot. Tool evidence is supplied separately,
with stable references and explicit truncation, to track files read/produced.
The current unanswered question follows the document layer so the unchanged
answer-format prompt's last-user-message contract remains true.

Supervisor robustness ruling (2026-10-25): request each state update as JSON in
message content, with JSON mode on verified provider capabilities and explicit
instructions plus JSON/fenced-JSON extraction otherwise. Never force a state
update tool call. For compatibility, accept a single validated update tool payload
even when the provider labels its finish as `stop`. Normalize only the explicit
entry aliases `src` to `source` and Chat fact `e` to `evidence`; collisions, duplicate
JSON keys, unknown fields and invalid provenance still reject the payload. Failure
retains the prior snapshot and logs a safe reason code. The next completed turn
makes one fresh bounded update including uncovered turns; no immediate retry.

Supervisor answer-isolation ruling (2026-10-25, P1b-3): scoped project Chat uses
normal prose completion, without the consolidation-JSON answer contract. State
updates and compaction-event persistence run independently in background tasks;
neither failure can reject or delay the user's answer. State parsing accepts the
first JSON object surrounded by prose, then enforces the unchanged closed schema,
alias and provenance rules. Unusable output leaves the previous state intact;
unrepresented verbatim history remains eligible on the next turn. Compaction
records are best effort on failure and remain visible through the Context ledger
when persistence succeeds. Pinned content and the 95% overflow boundary remain
unchanged.

Live transport correction (P1b-3): internal TranscriptState applies the same recent
window before sending history over the bounded broker response. Optional Context
history_compaction carries omitted represented IDs and token counts for the
background ledger write. Canonical history and the public transcript are unchanged.
MemoryEntry.kind includes constraint, the pinned kind already specified above.
The transport response cap remains 160000 bytes.
