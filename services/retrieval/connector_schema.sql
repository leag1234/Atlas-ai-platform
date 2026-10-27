-- REQ-SHARE-09: viewer Chat connector confirmations, scoped to an existing conversation.
-- Repeatable operator migration; rollback drops these two policies.
DROP POLICY IF EXISTS connector_ledger_insert ON atlas_projects.ledger_events;
CREATE POLICY connector_ledger_insert ON atlas_projects.ledger_events FOR INSERT WITH CHECK
(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['viewer'])
 AND data->>'kind' IN ('approval_requested','approval_decided','tool_completed')
 AND data->'source'->>'kind'='connector'
 AND data->'actor'->>'member_id'=atlas_projects.member()::text
 AND data->>'session_id' IS NULL AND EXISTS
 (SELECT 1 FROM atlas_projects.resources r WHERE r.org_id=ledger_events.org_id
 AND r.project_id=ledger_events.project_id AND r.kind='conversation'
 AND r.id::text=ledger_events.data->>'conversation_id'
 AND r.data->>'deleted_at' IS NULL
 AND (r.data->>'visibility' IS DISTINCT FROM 'private'
      OR r.data->>'owner_member_id'=atlas_projects.member()::text)));
DROP POLICY IF EXISTS connector_outbox_insert ON atlas_projects.outbox;
CREATE POLICY connector_outbox_insert ON atlas_projects.outbox FOR INSERT WITH CHECK
(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['viewer'])
 AND state='pending' AND EXISTS
 (SELECT 1 FROM atlas_projects.ledger_events e WHERE e.org_id=outbox.org_id
 AND e.project_id=outbox.project_id AND e.event_id=outbox.event_id
 AND e.data->>'kind' IN ('approval_requested','approval_decided','tool_completed')
 AND e.data->'source'->>'kind'='connector'
 AND e.data->'actor'->>'member_id'=atlas_projects.member()::text
 AND e.data->>'session_id' IS NULL));
