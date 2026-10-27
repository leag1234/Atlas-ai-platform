-- REQ-PROJ-05. Explicit idempotent operator migration, never application startup.
ALTER TABLE atlas_projects.resources DROP CONSTRAINT IF EXISTS resources_kind_check;
ALTER TABLE atlas_projects.resources ADD CONSTRAINT resources_kind_check CHECK
(kind IN ('folder','file','version','instructions','memory','derived','job','deliverable','outbox','work_session','repository','conversation','turn'));
CREATE TABLE IF NOT EXISTS atlas_projects.work_leases (
 org_id uuid NOT NULL, project_id uuid NOT NULL, session_id uuid NOT NULL,
 generation bigint NOT NULL CHECK(generation>0), request_id uuid NOT NULL,
 holder_member_id uuid NOT NULL, expires_at timestamptz NOT NULL,
 data jsonb NOT NULL,
 PRIMARY KEY(org_id,project_id,session_id),
 FOREIGN KEY(org_id,project_id,session_id) REFERENCES atlas_projects.resources(org_id,project_id,id),
 FOREIGN KEY(org_id,holder_member_id) REFERENCES atlas_projects.members(org_id,id)
);
CREATE TABLE IF NOT EXISTS atlas_projects.work_progress (
 org_id uuid NOT NULL, project_id uuid NOT NULL, session_id uuid NOT NULL,
 seq bigint NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(org_id,project_id,seq),
 FOREIGN KEY(org_id,project_id,session_id) REFERENCES atlas_projects.resources(org_id,project_id,id)
);
ALTER TABLE atlas_projects.file_approvals ADD COLUMN IF NOT EXISTS execution jsonb;
GRANT INSERT,UPDATE(data,execution) ON atlas_projects.file_approvals TO atlas_project_app;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['work_leases','work_progress'] LOOP
 EXECUTE format('ALTER TABLE atlas_projects.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE atlas_projects.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('DROP POLICY IF EXISTS scoped_read ON atlas_projects.%I',t);
 EXECUTE format('DROP POLICY IF EXISTS scoped_insert ON atlas_projects.%I',t);
 EXECUTE format('DROP POLICY IF EXISTS scoped_edit ON atlas_projects.%I',t);
 EXECUTE format('CREATE POLICY scoped_read ON atlas_projects.%I FOR SELECT USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'',''viewer'']))',t);
 EXECUTE format('CREATE POLICY scoped_insert ON atlas_projects.%I FOR INSERT WITH CHECK(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'']))',t);
 EXECUTE format('CREATE POLICY scoped_edit ON atlas_projects.%I FOR UPDATE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'']))',t);
 END LOOP;
END $$;
GRANT SELECT,INSERT,UPDATE ON atlas_projects.work_leases TO atlas_project_app;
GRANT SELECT,INSERT ON atlas_projects.work_progress TO atlas_project_app;

CREATE TABLE IF NOT EXISTS atlas_projects.forge_operations (
 org_id uuid NOT NULL, project_id uuid NOT NULL, id uuid NOT NULL,
 approval_id uuid NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(org_id,project_id,id), UNIQUE(org_id,project_id,approval_id),
 FOREIGN KEY(org_id,project_id,approval_id) REFERENCES atlas_projects.file_approvals(org_id,project_id,id)
);
ALTER TABLE atlas_projects.forge_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE atlas_projects.forge_operations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS scoped_read ON atlas_projects.forge_operations;
DROP POLICY IF EXISTS scoped_insert ON atlas_projects.forge_operations;
DROP POLICY IF EXISTS scoped_edit ON atlas_projects.forge_operations;
CREATE POLICY scoped_read ON atlas_projects.forge_operations FOR SELECT
 USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor','viewer']));
CREATE POLICY scoped_insert ON atlas_projects.forge_operations FOR INSERT
 WITH CHECK(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor']));
CREATE POLICY scoped_edit ON atlas_projects.forge_operations FOR UPDATE
 USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor']));
GRANT SELECT,INSERT,UPDATE ON atlas_projects.forge_operations TO atlas_project_app;
