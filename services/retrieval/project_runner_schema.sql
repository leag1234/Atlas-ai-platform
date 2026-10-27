-- REQ-RUN-02. Operator migration; keys never enter these tenant tables.
CREATE TABLE IF NOT EXISTS atlas_projects.runner_targets (
 org_id uuid NOT NULL, id uuid NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(org_id,id), FOREIGN KEY(org_id) REFERENCES atlas_projects.organizations(org_id)
);
ALTER TABLE atlas_projects.runner_targets ENABLE ROW LEVEL SECURITY;
ALTER TABLE atlas_projects.runner_targets FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS scoped_read ON atlas_projects.runner_targets;
DROP POLICY IF EXISTS admin_write ON atlas_projects.runner_targets;
CREATE POLICY scoped_read ON atlas_projects.runner_targets FOR SELECT
 USING(org_id=atlas_projects.org() AND atlas_projects.active());
CREATE POLICY admin_write ON atlas_projects.runner_targets FOR ALL
 USING(org_id=atlas_projects.org() AND atlas_projects.admin())
 WITH CHECK(org_id=atlas_projects.org() AND atlas_projects.admin());
GRANT SELECT,INSERT,UPDATE ON atlas_projects.runner_targets TO atlas_project_app;

CREATE TABLE IF NOT EXISTS atlas_projects.runner_grants (
 org_id uuid NOT NULL, project_id uuid NOT NULL, id uuid NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(org_id,project_id,id),
 FOREIGN KEY(org_id,id) REFERENCES atlas_projects.runner_targets(org_id,id),
 FOREIGN KEY(org_id,project_id) REFERENCES atlas_projects.projects(org_id,id)
);
CREATE TABLE IF NOT EXISTS atlas_projects.runner_sessions (
 org_id uuid NOT NULL, project_id uuid NOT NULL, id uuid NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(org_id,project_id,id),
 FOREIGN KEY(org_id,project_id) REFERENCES atlas_projects.projects(org_id,id)
);
CREATE TABLE IF NOT EXISTS atlas_projects.runner_commands (
 org_id uuid NOT NULL, project_id uuid NOT NULL, id uuid NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(org_id,project_id,id),
 FOREIGN KEY(org_id,project_id) REFERENCES atlas_projects.projects(org_id,id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['runner_grants','runner_sessions','runner_commands'] LOOP
 EXECUTE format('ALTER TABLE atlas_projects.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE atlas_projects.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('DROP POLICY IF EXISTS scoped_read ON atlas_projects.%I',t);
 EXECUTE format('DROP POLICY IF EXISTS scoped_insert ON atlas_projects.%I',t);
 EXECUTE format('DROP POLICY IF EXISTS scoped_edit ON atlas_projects.%I',t);
 EXECUTE format('CREATE POLICY scoped_read ON atlas_projects.%I FOR SELECT USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'',''viewer'']))',t);
 EXECUTE format('CREATE POLICY scoped_insert ON atlas_projects.%I FOR INSERT WITH CHECK(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'']))',t);
 EXECUTE format('CREATE POLICY scoped_edit ON atlas_projects.%I FOR UPDATE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'']))',t);
 EXECUTE format('GRANT SELECT,INSERT,UPDATE ON atlas_projects.%I TO atlas_project_app',t);
 END LOOP;
END $$;

-- Aggregate quota checks span projects without disclosing their rows or content.
CREATE OR REPLACE FUNCTION atlas_projects.runner_usage(target uuid)
RETURNS TABLE(active_commands bigint,target_commands bigint,open_sessions bigint,target_sessions bigint,reserved_seconds bigint,org_reserved_seconds bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT
 (SELECT count(*) FROM atlas_projects.runner_commands WHERE org_id=atlas_projects.org() AND data->>'state' IN ('running','unknown')),
 (SELECT count(*) FROM atlas_projects.runner_commands WHERE org_id=atlas_projects.org() AND data->>'target_id'=target::text AND data->>'state' IN ('running','unknown')),
 (SELECT count(*) FROM atlas_projects.runner_sessions WHERE org_id=atlas_projects.org() AND data->>'state'='open' AND (data->>'last_activity')::timestamptz > now()-interval '24 hours'),
 (SELECT count(*) FROM atlas_projects.runner_sessions WHERE org_id=atlas_projects.org() AND data->>'target_id'=target::text AND data->>'state'='open' AND (data->>'last_activity')::timestamptz > now()-interval '24 hours'),
 (SELECT coalesce(sum(CASE WHEN data->>'state' IN ('running','unknown') THEN (data->>'timeout_s')::bigint ELSE ceil(coalesce((data->>'duration_s')::numeric,0))::bigint END),0)::bigint FROM atlas_projects.runner_commands WHERE org_id=atlas_projects.org() AND data->>'target_id'=target::text AND data->>'state' IN ('running','unknown','succeeded','failed','timed_out','cancelled') AND (data->>'created_at')::timestamptz>=date_trunc('month',now())),
 (SELECT coalesce(sum(CASE WHEN data->>'state' IN ('running','unknown') THEN (data->>'timeout_s')::bigint ELSE ceil(coalesce((data->>'duration_s')::numeric,0))::bigint END),0)::bigint FROM atlas_projects.runner_commands WHERE org_id=atlas_projects.org() AND data->>'state' IN ('running','unknown','succeeded','failed','timed_out','cancelled') AND (data->>'created_at')::timestamptz>=date_trunc('month',now()))
 WHERE atlas_projects.active()
$$;
REVOKE ALL ON FUNCTION atlas_projects.runner_usage(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION atlas_projects.runner_usage(uuid) TO atlas_project_app;
