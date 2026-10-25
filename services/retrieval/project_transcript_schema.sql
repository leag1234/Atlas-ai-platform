-- Approved private transcript interface, 2026-10-24. Operator migration only.
ALTER TABLE atlas_projects.resources DROP CONSTRAINT IF EXISTS resources_kind_check;
ALTER TABLE atlas_projects.resources ADD CONSTRAINT resources_kind_check CHECK
(kind IN ('folder','file','version','instructions','memory','derived','job','deliverable','outbox','work_session','repository','conversation','turn'));
DROP POLICY IF EXISTS transcript_insert ON atlas_projects.resources;
CREATE POLICY transcript_insert ON atlas_projects.resources FOR INSERT WITH CHECK
(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor','viewer'])
 AND kind IN ('conversation','turn') AND data->>'created_by_member_id'=atlas_projects.member()::text
 AND (kind='conversation' OR data->>'action'='chat'));
DROP POLICY IF EXISTS transcript_edit ON atlas_projects.resources;
CREATE POLICY transcript_edit ON atlas_projects.resources FOR UPDATE USING
(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor','viewer'])
 AND (kind='conversation' OR (kind='turn' AND data->>'action'='chat' AND data->>'created_by_member_id'=atlas_projects.member()::text)))
WITH CHECK (org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor','viewer'])
 AND (kind='conversation' OR (kind='turn' AND data->>'action'='chat' AND data->>'created_by_member_id'=atlas_projects.member()::text)));
CREATE TABLE IF NOT EXISTS atlas_projects.transcript_progress (
 org_id uuid NOT NULL, project_id uuid NOT NULL, turn_id uuid NOT NULL,
 seq bigint NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(org_id,project_id,seq),
 FOREIGN KEY(org_id,project_id,turn_id) REFERENCES atlas_projects.resources(org_id,project_id,id)
);
ALTER TABLE atlas_projects.transcript_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE atlas_projects.transcript_progress FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS scoped_read ON atlas_projects.transcript_progress;
CREATE POLICY scoped_read ON atlas_projects.transcript_progress FOR SELECT USING
(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor','viewer']));
DROP POLICY IF EXISTS scoped_insert ON atlas_projects.transcript_progress;
CREATE POLICY scoped_insert ON atlas_projects.transcript_progress FOR INSERT WITH CHECK
(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor','viewer']) AND EXISTS
 (SELECT 1 FROM atlas_projects.resources r WHERE r.org_id=transcript_progress.org_id
 AND r.project_id=transcript_progress.project_id AND r.id=turn_id AND r.kind='turn'
 AND r.data->>'created_by_member_id'=atlas_projects.member()::text
 AND (r.data->>'action'='chat' OR atlas_projects.access(project_id,ARRAY['owner','editor']))));
GRANT SELECT,INSERT ON atlas_projects.transcript_progress TO atlas_project_app;
DROP POLICY IF EXISTS transcript_ledger_insert ON atlas_projects.ledger_events;
CREATE POLICY transcript_ledger_insert ON atlas_projects.ledger_events FOR INSERT WITH CHECK
(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['viewer'])
 AND data->>'kind' IN ('context_selected','location_used','model_dispatch_intent','model_sent','model_acknowledged','model_failed','model_unknown')
 AND data->'actor'->>'member_id'=atlas_projects.member()::text AND data->>'session_id' IS NULL AND EXISTS
 (SELECT 1 FROM atlas_projects.resources r WHERE r.org_id=ledger_events.org_id AND r.project_id=ledger_events.project_id
 AND r.kind='turn' AND r.data->>'request_id'=ledger_events.data->>'request_id'
 AND r.data->>'conversation_id'=ledger_events.data->>'conversation_id'
 AND r.data->>'created_by_member_id'=atlas_projects.member()::text AND r.data->>'action'='chat'));
DROP POLICY IF EXISTS transcript_outbox_insert ON atlas_projects.outbox;
CREATE POLICY transcript_outbox_insert ON atlas_projects.outbox FOR INSERT WITH CHECK
(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['viewer']) AND state='pending' AND EXISTS
 (SELECT 1 FROM atlas_projects.ledger_events e WHERE e.org_id=outbox.org_id AND e.project_id=outbox.project_id AND e.event_id=outbox.event_id
 AND e.data->>'kind' IN ('context_selected','location_used','model_dispatch_intent','model_sent','model_acknowledged','model_failed','model_unknown')
 AND e.data->'actor'->>'member_id'=atlas_projects.member()::text AND e.data->>'session_id' IS NULL));
