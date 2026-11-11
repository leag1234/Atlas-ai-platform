-- REQ-RUN-02. Operator migration; keys never enter these tenant tables.
CREATE TABLE IF NOT EXISTS atlas_projects.runner_targets (
 org_id uuid NOT NULL, id uuid NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(org_id,id), FOREIGN KEY(org_id) REFERENCES atlas_projects.organizations(org_id)
);
-- Personal machines (owner decision 2026-11-11): a row with owner_member_id
-- belongs to that member alone. NULL keeps the organization target of an admin.
ALTER TABLE atlas_projects.runner_targets ADD COLUMN IF NOT EXISTS owner_member_id uuid;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='runner_targets_owner_fkey'
  AND conrelid='atlas_projects.runner_targets'::regclass) THEN
  ALTER TABLE atlas_projects.runner_targets ADD CONSTRAINT runner_targets_owner_fkey
   FOREIGN KEY(org_id,owner_member_id) REFERENCES atlas_projects.members(org_id,id);
 END IF;
END $$;
CREATE INDEX IF NOT EXISTS runner_targets_owner ON atlas_projects.runner_targets(org_id,owner_member_id)
 WHERE owner_member_id IS NOT NULL;
ALTER TABLE atlas_projects.runner_targets ENABLE ROW LEVEL SECURITY;
ALTER TABLE atlas_projects.runner_targets FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS scoped_read ON atlas_projects.runner_targets;
DROP POLICY IF EXISTS admin_write ON atlas_projects.runner_targets;
DROP POLICY IF EXISTS owner_write ON atlas_projects.runner_targets;
-- No member, administrators included, reads another member's machine.
CREATE POLICY scoped_read ON atlas_projects.runner_targets FOR SELECT
 USING(org_id=atlas_projects.org() AND atlas_projects.active()
  AND (owner_member_id IS NULL OR owner_member_id=atlas_projects.member()));
CREATE POLICY admin_write ON atlas_projects.runner_targets FOR ALL
 USING(org_id=atlas_projects.org() AND atlas_projects.admin() AND owner_member_id IS NULL)
 WITH CHECK(org_id=atlas_projects.org() AND atlas_projects.admin() AND owner_member_id IS NULL);
CREATE POLICY owner_write ON atlas_projects.runner_targets FOR ALL
 USING(org_id=atlas_projects.org() AND atlas_projects.active() AND owner_member_id=atlas_projects.member())
 WITH CHECK(org_id=atlas_projects.org() AND atlas_projects.active() AND owner_member_id=atlas_projects.member());
GRANT SELECT,INSERT,UPDATE,DELETE ON atlas_projects.runner_targets TO atlas_project_app;

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
 EXECUTE format('DROP POLICY IF EXISTS scoped_delete ON atlas_projects.%I',t);
 EXECUTE format('CREATE POLICY scoped_read ON atlas_projects.%I FOR SELECT USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'',''viewer'']))',t);
 EXECUTE format('CREATE POLICY scoped_insert ON atlas_projects.%I FOR INSERT WITH CHECK(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'']))',t);
 EXECUTE format('CREATE POLICY scoped_edit ON atlas_projects.%I FOR UPDATE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'']))',t);
 EXECUTE format('CREATE POLICY scoped_delete ON atlas_projects.%I FOR DELETE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'']))',t);
 EXECUTE format('GRANT DELETE ON atlas_projects.%I TO atlas_project_app',t);
 EXECUTE format('GRANT SELECT,INSERT,UPDATE ON atlas_projects.%I TO atlas_project_app',t);
 END LOOP;
END $$;

-- The owner of a personal machine removes its per-project use records with
-- it, including in a project he has left since.
DROP POLICY IF EXISTS personal_read ON atlas_projects.runner_grants;
DROP POLICY IF EXISTS personal_delete ON atlas_projects.runner_grants;
CREATE POLICY personal_read ON atlas_projects.runner_grants FOR SELECT
 USING(org_id=atlas_projects.org() AND EXISTS(SELECT 1 FROM atlas_projects.runner_targets t
  WHERE t.org_id=runner_grants.org_id AND t.id=runner_grants.id AND t.owner_member_id=atlas_projects.member()));
CREATE POLICY personal_delete ON atlas_projects.runner_grants FOR DELETE
 USING(org_id=atlas_projects.org() AND EXISTS(SELECT 1 FROM atlas_projects.runner_targets t
  WHERE t.org_id=runner_grants.org_id AND t.id=runner_grants.id AND t.owner_member_id=atlas_projects.member()));

-- Aggregate quota checks span projects without disclosing their rows or content.
-- A session counts against the parallel-session limits only while it is in use
-- (activity in the last 5 minutes). An idle session keeps its working
-- directory for later turns but takes no slot: with 24 hours, two chat
-- conversations filled a personal machine (limit 2) for a whole day, and with
-- 30 minutes a user working in three conversations was refused (staging
-- 2026-11-11). Running commands are limited separately.
CREATE OR REPLACE FUNCTION atlas_projects.runner_usage(target uuid)
RETURNS TABLE(active_commands bigint,target_commands bigint,open_sessions bigint,target_sessions bigint,reserved_seconds bigint,org_reserved_seconds bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT
 (SELECT count(*) FROM atlas_projects.runner_commands WHERE org_id=atlas_projects.org() AND data->>'state' IN ('running','unknown')),
 (SELECT count(*) FROM atlas_projects.runner_commands WHERE org_id=atlas_projects.org() AND data->>'target_id'=target::text AND data->>'state' IN ('running','unknown')),
 (SELECT count(*) FROM atlas_projects.runner_sessions WHERE org_id=atlas_projects.org() AND data->>'state'='open' AND (data->>'last_activity')::timestamptz > now()-interval '5 minutes'),
 (SELECT count(*) FROM atlas_projects.runner_sessions WHERE org_id=atlas_projects.org() AND data->>'target_id'=target::text AND data->>'state'='open' AND (data->>'last_activity')::timestamptz > now()-interval '5 minutes'),
 (SELECT coalesce(sum(CASE WHEN data->>'state' IN ('running','unknown') THEN (data->>'timeout_s')::bigint ELSE ceil(coalesce((data->>'duration_s')::numeric,0))::bigint END),0)::bigint FROM atlas_projects.runner_commands WHERE org_id=atlas_projects.org() AND data->>'target_id'=target::text AND data->>'state' IN ('running','unknown','succeeded','failed','timed_out','cancelled') AND (data->>'created_at')::timestamptz>=date_trunc('month',now())),
 (SELECT coalesce(sum(CASE WHEN data->>'state' IN ('running','unknown') THEN (data->>'timeout_s')::bigint ELSE ceil(coalesce((data->>'duration_s')::numeric,0))::bigint END),0)::bigint FROM atlas_projects.runner_commands WHERE org_id=atlas_projects.org() AND data->>'state' IN ('running','unknown','succeeded','failed','timed_out','cancelled') AND (data->>'created_at')::timestamptz>=date_trunc('month',now()))
 WHERE atlas_projects.active()
$$;
REVOKE ALL ON FUNCTION atlas_projects.runner_usage(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION atlas_projects.runner_usage(uuid) TO atlas_project_app;
