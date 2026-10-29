-- REQ-PROJ scheduled tasks: explicit repeatable operator migration.
CREATE TABLE IF NOT EXISTS atlas_projects.scheduled_tasks (
 org_id uuid NOT NULL, project_id uuid NOT NULL, id uuid NOT NULL,
 owner_member_id uuid NOT NULL, data jsonb NOT NULL,
 enabled boolean NOT NULL DEFAULT true, next_run timestamptz NOT NULL,
 run_id uuid, lease_until timestamptz, run_data jsonb NOT NULL DEFAULT '{}',
 PRIMARY KEY(org_id,project_id,id),
 FOREIGN KEY(org_id,project_id) REFERENCES atlas_projects.projects(org_id,id),
 FOREIGN KEY(org_id,owner_member_id) REFERENCES atlas_projects.members(org_id,id)
);
CREATE INDEX IF NOT EXISTS scheduled_due ON atlas_projects.scheduled_tasks(next_run) WHERE enabled;
ALTER TABLE atlas_projects.scheduled_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE atlas_projects.scheduled_tasks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS scheduled_read ON atlas_projects.scheduled_tasks;
CREATE POLICY scheduled_read ON atlas_projects.scheduled_tasks FOR SELECT USING
(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor','viewer']));
DROP POLICY IF EXISTS scheduled_write ON atlas_projects.scheduled_tasks;
CREATE POLICY scheduled_write ON atlas_projects.scheduled_tasks FOR ALL USING
(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor']))
WITH CHECK (org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor']));
GRANT SELECT,INSERT,UPDATE,DELETE ON atlas_projects.scheduled_tasks TO atlas_project_app;
-- Discovery returns routing metadata only. It never exposes task instructions or
-- grants a project identity; normal scoped transactions revalidate each claim.
CREATE OR REPLACE FUNCTION atlas_projects.scheduled_candidates()
RETURNS TABLE(org_id uuid,project_id uuid,id uuid,subject text,member_id uuid,membership_revision bigint,policy_revision bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 UPDATE atlas_projects.scheduled_tasks t SET enabled=false,
 data=jsonb_set(jsonb_set(t.data,'{enabled}','false'),'{pause_reason}','"access_lost"')
 WHERE t.enabled AND NOT EXISTS (SELECT 1 FROM atlas_projects.members m
 JOIN atlas_projects.grants g ON g.org_id=m.org_id AND g.member_id=m.id
 JOIN atlas_projects.projects p ON p.org_id=g.org_id AND p.id=g.project_id
 WHERE m.org_id=t.org_id AND m.id=t.owner_member_id AND m.status='active'
 AND g.project_id=t.project_id AND g.role IN ('owner','editor') AND p.state='active');
 -- Revoked creators cannot enter a scoped transaction to release an abandoned
 -- claim. Clear it only after both leases are safely past their hard deadline.
 UPDATE atlas_projects.scheduled_tasks t SET run_id=NULL,lease_until=NULL,run_data='{}',
 data=jsonb_set(t.data,'{last_result,status}','"failed"')
 WHERE NOT t.enabled AND t.data->>'pause_reason'='access_lost'
 AND t.run_id IS NOT NULL AND t.lease_until<=now()
 AND NOT EXISTS (SELECT 1 FROM atlas_projects.work_leases w
 WHERE w.org_id=t.org_id AND w.project_id=t.project_id
 AND w.session_id=(t.run_data->>'session_id')::uuid
 AND NOT COALESCE((w.data->>'released')::boolean,false)
 AND (w.data->>'deadline')::timestamptz>now());
 RETURN QUERY SELECT t.org_id,t.project_id,t.id,m.subject,m.id,m.revision,o.revision
 FROM atlas_projects.scheduled_tasks t
 JOIN atlas_projects.members m ON m.org_id=t.org_id AND m.id=t.owner_member_id
 JOIN atlas_projects.organizations o ON o.org_id=t.org_id
 WHERE (t.enabled AND t.next_run<=now()) OR (t.run_id IS NOT NULL AND t.lease_until<=now())
 ORDER BY t.next_run LIMIT 100;
END $$;
REVOKE ALL ON FUNCTION atlas_projects.scheduled_candidates() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION atlas_projects.scheduled_candidates() TO atlas_project_app;
