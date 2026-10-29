# 19 — Shared projects and organization API access

Status: design with owner decisions of 2026-10-27 (section 9); K-A in progress. Nothing here is implemented beyond what §2 lists as existing. Requirement IDs are local to this design (`REQ-SHARE-*`, `REQ-KEY-*`). It extends `docs/17-project-mode.md` (§2 data model, §4 API, §5 Work approvals, §7 organization administration) and `docs/18-remote-runners.md`, and does not change their contracts.

## 1. Purpose and scope

Two capabilities for organizations that deploy Atlas:

- **Shared projects.** Several members of one organization work in the same project: same files, instructions, project memory, conversations and deliverables, with roles.
- **API access.** Members and service accounts call Atlas from scripts and applications with keys that the organization issues, scopes, limits and revokes.

Both rest on one identity: an active member of one organization. A key is another way to authenticate that member, never a separate identity with its own rules.

REQ-SHARE-01: P1 scope is sharing between members of the same organization. Out of scope: cross-organization sharing, public links, guests without a membership, real-time co-editing of files.

## 2. What exists (verified on `main`, 2026-10-27)

| Area | State |
| --- | --- |
| Members and grants | `services/retrieval/project_schema.sql`: `organizations`, `members` (`admin`/`member`, `active` status), `grants` (`owner`/`editor`/`viewer`), forced row-level security through `atlas_projects.access()`. `project_scoped_api.py` serves `GET P/grants`, `PUT/DELETE P/grants/{member}` (owner only). |
| Interface | `services/orchestrator/project_mode.js` has no grant management: a project cannot be shared from the UI. |
| Conversations | Each conversation has `owner_member_id`. Visibility of other members' conversations is not specified. |
| Connectors | `services/orchestrator/mcp_client.py`: one administrator file (`ATLAS_MCP_CONFIG`), credentials taken from installation environment variables, writes confirmed. Organization policy has `connector_allowlist` (`project_policy.py`). No per-member credentials, no per-user or per-project rules. |
| Developer API | `services/orchestrator/devapi.py`, `dev_auth.py`, `adr/0001-developer-api.md`: separate local entry, keys created and revoked by an operator command line, stored as SHA-256 hashes, per-key daily request and EUR quotas with reservations, `GET /v1/usage`. Only the raw `atlas-code` model: no search, retrieval, tools or projects. |
| Open WebUI keys | 0.11.3 native: global switch `ENABLE_API_KEYS` plus group permission `features.api_keys`; one key per user, self-service create/regenerate/delete; permission rechecked on each call (removing it blocks the key at once); no expiry, no per-key scope, keys stored in clear. |
| Live check on the project trial | With project mode on, key creation, `/api/models` and revocation work, but every `/api/chat/completions` fails with 403 `origin_rejected`: `browser_origin()` in `services/orchestrator/project_ui.py` requires a browser `Origin` on `POST /v1/chat/completions`. |

Open WebUI 0.11.3 also shares chat folders with users and groups (read/write access grants, disabled by `sharing.folders`). This is not the path: project mode is Atlas-owned, keys there would bypass project grants, and the owner decided to leave Open WebUI (decision of 2026-10-23).

## 3. Shared projects

REQ-SHARE-02: roles. Existing grant roles keep their meaning; this design fixes what each may do.

| Action | Viewer | Editor | Owner | Org admin |
| --- | --- | --- | --- | --- |
| Read files, instructions, memory, shared conversations | yes | yes | yes | only with a grant |
| Chat in the project | yes | yes | yes | only with a grant |
| Start Work, approve Work actions | no | yes | yes | only with a grant |
| Edit files, instructions, memory | no | yes | yes | only with a grant |
| Manage grants and project connector list | no | no | yes | may revoke any grant and transfer ownership, audited |

An org admin does not read project content by default. The admin can change grants for continuity (a departed owner) and the action is recorded in the audit.

REQ-SHARE-03: conversations. Each conversation is `private` (default: only its creator) or `shared` (all members with a grant). Turning a private conversation into a shared one is an explicit action with a warning, and is never automatic. Every turn records its author (`member_id`), shown in the interface. Several members may write in a shared conversation: turns are processed one at a time in arrival order, a second request waits with a visible "busy" state, and the context of each turn is the conversation as committed when it starts.

REQ-SHARE-04: context isolation. In a project, the model context is built only from the project: instructions, project memory, project files, the conversation and its tool results. Personal (user-level) memory and personal instructions of any member are never injected into a project turn, shared or not. The context manifest (doc 17 `context_manifest`) lists every source used, so this can be tested. A member who wants a personal fact in the project adds it to project memory explicitly.

REQ-SHARE-05: Work in a shared project. Only editors and owners start Work or approve its actions. An approval is bound to the approving member (doc 17 §5) and shown with their name. Work started by one member may be cancelled by its initiator, any owner, or an org admin with a grant.

REQ-SHARE-06: revocation. Removing a grant or deactivating a member takes effect on the next request: the scoped identity is resolved again on every call. A turn or Work action already running for that member is cancelled at its next tool boundary. Pending approvals by that member are void. Their past turns stay in the conversation with their name; their private conversations stay private and are kept under the organization retention policy.

## 4. Connectors (MCP) under sharing

REQ-SHARE-07: three layers, all must allow a connector:

1. **Organization:** `connector_allowlist` (exists).
2. **Member or group:** an admin rule per member or group, `allow` or `deny` per connector. Groups are new (`groups`, `group_members` tables in the project schema, managed by the org admin).
3. **Project:** an owner-managed list of connectors usable in that project. Default: empty.

REQ-SHARE-08: credentials. A connector call always uses the credentials of the member who made the request, never those of another member, the project owner or the installation. Connectors that act on an account (mail, calendar, files, forge) need per-member credentials (OAuth or a token the member enters), stored by a connector broker on the model of the runner broker of doc 18 (§4): never in the model context, logs or project files. Installation-wide credentials, as in today's `ATLAS_MCP_CONFIG`, are allowed only for connectors that do not act on a personal account (for example a public documentation server), and are marked as such.

REQ-SHARE-09: personal connectors in shared conversations (owner decision, 2026-10-27). When a connector marked `personal` is about to run in a shared conversation, the caller sees a confirmation card before the call: the connector, the action, and the statement that the result will be visible to every member of the project. Confirmation is needed for each call; it is not remembered. Refusal leaves the turn without that result, and the model says so. In a private conversation the existing rules apply (writes confirmed, reads allowed). The ledger records each confirmation with member, connector and conversation.

## 5. API keys

REQ-KEY-01: one Atlas key system for the organization, owned by the project service, replacing both the operator-only developer keys and Open WebUI keys. Open WebUI keys stay disabled (`ENABLE_API_KEYS` off).

| Field | Notes |
| --- | --- |
| `id`, `org_id`, `member_id` | The key acts as that member. Service accounts are members of kind `service` (no interactive login), owned by an admin. |
| `name`, `prefix` | Visible prefix (`atl_` plus 8 characters) for recognition in lists and leaks; the rest is shown once at creation. |
| `hash` | Hash of the full key with a server pepper; the key is never stored in clear. |
| `scopes[]` | `assistant` (Atlas with search and tools, outside projects), `model` (raw model, today's developer API), `project:{id}:read`, `project:{id}:chat`, `project:{id}:work`, `connectors` (off by default; personal connectors never usable by key in P1). |
| `expires_at` | Required. Default 90 days, maximum set by the organization. |
| `quota` | Requests per day and EUR per day, with the reservation logic of `dev_auth.py`; also bounded by the member's and organization's quotas. |
| `created_by`, `created_at`, `last_used_at`, `last_used_ip_prefix`, `revoked_at` | Shown in lists and in the audit. |

REQ-KEY-02: issuing. Org admins create, list and revoke keys for any member or service account. Members create and revoke their own keys only if the organization policy allows self-service (`api_keys: off | admin_only | self_service`, default `admin_only`), within limits the admin sets (scopes, quota, expiry). A key can never have more rights than its member has at the time of each call: scopes are intersected with current grants.

REQ-KEY-03: authentication at the Atlas edge. A key arrives as `Authorization: Bearer` on the public API prefix. The edge looks it up by hash, checks expiry, revocation, member status and scope, then forwards the same signed, short-lived scoped identity that browser requests get (doc 17 REQ-PROJ-04). Origin checks remain for browser requests, which carry cookies; key requests carry no cookie and no browser `Origin`. This removes the `origin_rejected` failure seen on the trial without weakening the browser path. Keys never go through Open WebUI.

REQ-KEY-04: API surface. OpenAI-compatible `chat/completions`, `responses` and `models` (formats of the developer API), plus project selection by an `atlas_project` field or a header. A key with `project:{id}:chat` gets project Chat with citations; `work` allows Work, whose approvals are returned as pending actions that the member approves in the interface or through an approval endpoint. `GET /v1/usage` per key.

REQ-KEY-05: audit. Key creation, use summary (per day), scope change, revocation and failures (expired, revoked, out of scope, over quota) are ledger events. Content is not logged. A revoked or expired key fails with 401, an out-of-scope call with 403 and the missing scope, an exhausted quota with 429 and the quota values (D5).

REQ-KEY-06: migration. Existing developer keys are imported as service-account keys with scope `model` and their current quotas, hashes kept as they are (SHA-256, flagged for rotation). The operator command line becomes a client of the admin API. The local `8030` entry keeps working until owners rotate their keys.

## 6. Interface

- Project settings: "Members" panel (add from the organization list, role, remove), "Connectors" panel (project list). Conversation header: private or shared, with authors shown per turn.
- Organization admin: members, groups, connector rules, API key policy, all keys with prefix, owner, scopes, last use, revoke.
- Account: "API keys" (if self-service), with the key shown once and a copy button.

## 7. Threats

| Threat | Mitigation |
| --- | --- |
| A member's personal data leaks to other members | No personal memory in projects; personal connectors need a per-call confirmation in shared conversations; caller's credentials only. |
| Prompt injection in a shared file triggers a connector | Project connector list empty by default; writes confirmed; personal connectors confirmed per call. |
| Stolen key | Hash at rest, visible prefix, mandatory expiry, quotas, last-use display, immediate revocation, scopes intersected with current grants. |
| A departed member keeps access | Identity and key resolved on every call; deactivation cancels running work. |
| Escalation through a key | A key never exceeds its member; service accounts need an admin owner; `work` scope only with an explicit project grant. |

## 8. Phasing and acceptance

| Phase | Scope | Acceptance (live, generic accounts) |
| --- | --- | --- |
| S-A | Grant UI, conversation visibility, author per turn, context isolation test (manifest shows no personal source) | Two test members: owner shares a project with an editor and a viewer; viewer chats but cannot start Work; editor approves a Work action; revoking the editor cancels their running Work. |
| K-A | Key table, admin and self-service API, edge authentication, `assistant` and `model` scopes, quotas, audit, devapi migration | A key calls `chat/completions` with search on the project-mode trial (no `origin_rejected`); revocation fails the next call with 401; quota exhaustion returns 429 with values; key list shows prefix and last use. |
| K-B | Project scopes (`read`, `chat`, `work`) and approvals through the API | A key with `project:X:chat` gets cited answers from project X and 403 on project Y. |
| S-B | Groups, member and project connector rules, connector broker with per-member credentials, confirmation card for personal connectors | In a shared conversation, a test member's mail connector asks for confirmation each time and shows the visibility warning; another member's credentials are never used. |

## 9. Owner decisions (2026-10-27)

1. API keys are issued by organization administrators only by default (`admin_only`); self-service is an organization option.
2. In a shared project, a conversation is private by default and is shared by an explicit action.
3. An organization administrator manages grants but does not read project content without a grant.
4. Key lifetime: 90 days by default, one year at most.
5. A personal connector used in a shared conversation needs an explicit confirmation by the caller on every call.
6. Until K-A ships, the guest instance uses Open WebUI keys for a tester group behind an operator switch (PR #69): model list and chat completions only, per-user budgets apply.
