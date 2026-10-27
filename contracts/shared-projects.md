# Shared projects S-A (REQ-SHARE-02 through REQ-SHARE-06)

All existing identity, idempotency and optimistic revision rules apply. P denotes
`/api/atlas/orgs/{org}/projects/{project}`. All names below are fictional in tests.

- Conversations are private by default, visible only to their creator with a
  current project grant. `visibility` is `private` or `shared` in responses.
- `POST P/conversations/{conversation}/share`, body `{"confirm":true}`, requires
  the creator's current grant, If-Match and Idempotency-Key. It explicitly warns
  through the confirmation contract that all project grantees can read the full
  conversation, including previous turns. Returns the updated conversation.
  Sharing is audited and irreversible through this API. An owner/admin cannot
  share another member's private conversation.
- Turns return `member_id`, the author; historical rows use created_by_member_id.
  A conversation admits one running turn, Chat or Work, across service processes.
  A second begin returns 409 `session_busy` with a retry message. Idempotent
  replay remains available. Context is assembled after acquiring this boundary.
- `GET P/members` is owner-only and returns `{items:[{id,display_name}],next_cursor}`
  for active organization members. Identity subjects are never exposed here.
- `GET P/connectors` returns `{connector_ids:[uuid]}`; `PUT P/connectors` replaces
  the list, owner-only, with If-Match against the project revision. IDs must be
  allowed by organization policy; the initial list is empty. Connector credential
  brokering and the remaining S-B connector policy layers are a separate task.
- `DELETE /api/atlas/orgs/{org}/project-grants/{project}/{member}` is org-admin-only
  and revokes a grant with If-Match. Last-owner protection requires transfer first.
- `POST /api/atlas/orgs/{org}/project-ownership/{project}` is org-admin-only, body
  `{member_id:uuid}`, with If-Match against project revision. Atomically grants
  ownership to the active target and demotes the prior primary owner to editor.
  These metadata operations are audited and return no project content. Admins
  still need a project grant for all content, Chat and Work operations.
- Viewers read and Chat; editors also edit and start/approve Work; owners also
  manage grants/connectors. Work cancellation is permitted to the initiator,
  an owner, or an org admin with a project grant.
- Grant revocation/demotion and member deactivation invalidate pending approvals
  and flag running Work for cancellation. The existing worker fence re-resolves
  authority before each tool; it stops execution when authority is lost. Running
  transcripts are cancelled and private conversations remain private. Spend is
  retained until existing lease recovery reconciles cleanup.

Migration: project_sharing_schema.sql is applied by install/upgrade_work after
transcript schema. Existing conversations default to private. Existing author
metadata is preserved. No model/provider calls are needed for these semantics.

Privacy also filters transcript/Work progress, approvals, ledger and outbox rows.
The project ledger hash chain remains global: an authorized append reads only the
latest digest through a metadata-only function, without access to private events.
Clients reading a filtered ledger may observe sequence gaps.
