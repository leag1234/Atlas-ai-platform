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
 EXECUTE format('DROP POLICY IF EXISTS scoped_delete ON atlas_projects.%I',t);
 EXECUTE format('CREATE POLICY scoped_read ON atlas_projects.%I FOR SELECT USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'',''viewer'']))',t);
 EXECUTE format('CREATE POLICY scoped_insert ON atlas_projects.%I FOR INSERT WITH CHECK(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'']))',t);
 EXECUTE format('CREATE POLICY scoped_edit ON atlas_projects.%I FOR UPDATE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'']))',t);
 END LOOP;
END $$;
GRANT SELECT,INSERT,UPDATE ON atlas_projects.work_leases TO atlas_project_app;
GRANT SELECT,INSERT ON atlas_projects.work_progress TO atlas_project_app;
CREATE POLICY scoped_delete ON atlas_projects.work_leases FOR DELETE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner']));
CREATE POLICY scoped_delete ON atlas_projects.work_progress FOR DELETE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner']));
GRANT DELETE ON atlas_projects.work_leases,atlas_projects.work_progress TO atlas_project_app;

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
DROP POLICY IF EXISTS scoped_delete ON atlas_projects.forge_operations;
CREATE POLICY scoped_read ON atlas_projects.forge_operations FOR SELECT
 USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor','viewer']));
CREATE POLICY scoped_insert ON atlas_projects.forge_operations FOR INSERT
 WITH CHECK(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor']));
CREATE POLICY scoped_edit ON atlas_projects.forge_operations FOR UPDATE
 USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor']));
GRANT SELECT,INSERT,UPDATE ON atlas_projects.forge_operations TO atlas_project_app;
CREATE POLICY scoped_delete ON atlas_projects.forge_operations FOR DELETE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner']));
GRANT DELETE ON atlas_projects.forge_operations TO atlas_project_app;

CREATE INDEX IF NOT EXISTS work_lease_expiry ON atlas_projects.work_leases(org_id,expires_at)
 WHERE NOT coalesce((data->>'released')::boolean,false);

-- UUID v5 matches Scope.reserve's durable reservation IDs, including old leases.
-- uuid-ossp ships with the supported PostgreSQL image; operator installation only.
CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public;
CREATE OR REPLACE FUNCTION atlas_projects.release_work_reservations(o uuid, p uuid, reservation uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE affected integer;
BEGIN
 -- Terminal reports settle known model usage first. Missing reports never imply
 -- zero spend; preserve both previously settled usage and already unknown usage.
 UPDATE atlas_projects.reservations SET
 amount=CASE WHEN resource IN ('concurrent_jobs','stored_bytes','temporary_bytes') THEN 0 ELSE amount END,
 state=CASE WHEN resource IN ('concurrent_jobs','stored_bytes','temporary_bytes') THEN 'released'
            WHEN state='reserved' THEN 'unknown' ELSE state END
 WHERE org_id=o AND project_id=p
 AND resource IN ('concurrent_jobs','stored_bytes','temporary_bytes','daily_model_eur','monthly_model_eur')
 AND id=public.uuid_generate_v5(reservation,resource);
 GET DIAGNOSTICS affected = ROW_COUNT;
 IF affected<>5 THEN
  RAISE EXCEPTION 'incomplete Work reservation set' USING ERRCODE='23514';
 END IF;
END $$;
REVOKE ALL ON FUNCTION atlas_projects.release_work_reservations(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION atlas_projects.release_work_reservations(uuid,uuid,uuid) TO atlas_project_app;

-- Like scheduled_candidates, this maintenance boundary returns no private content.
-- A selected organization must be the authenticated caller's organization; NULL
-- is used only by the private scheduler transport to sweep all organizations.
CREATE OR REPLACE FUNCTION atlas_projects.recover_expired_work_leases(target_org uuid)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE o uuid; w record; recovered integer := 0; usage jsonb; model record;
 stamp text; patch jsonb; message text := 'Work interrupted: execution lease expired before a final report arrived.';
BEGIN
 IF target_org IS NOT NULL AND
 (target_org IS DISTINCT FROM atlas_projects.org() OR NOT atlas_projects.active()) THEN
  RAISE EXCEPTION 'unauthorized recovery' USING ERRCODE='42501';
 END IF;
 FOR o IN SELECT DISTINCT org_id FROM atlas_projects.work_leases
 WHERE (target_org IS NULL OR org_id=target_org)
 AND NOT coalesce((data->>'released')::boolean,false)
 AND expires_at < clock_timestamp()-interval '15 seconds' ORDER BY org_id LOOP
  -- Use the same lock order as Scope, before taking any lease row lock. The
  -- subsequent query sees renewals committed while we waited for this lock.
  PERFORM pg_advisory_xact_lock(hashtextextended(o::text,0));
  FOR w IN SELECT * FROM atlas_projects.work_leases
  WHERE org_id=o AND NOT coalesce((data->>'released')::boolean,false)
  AND expires_at < clock_timestamp()-interval '15 seconds'
  ORDER BY project_id,session_id FOR UPDATE LOOP
   PERFORM atlas_projects.release_work_reservations(o,w.project_id,(w.data->>'reservation_id')::uuid);
   SELECT amount,state INTO model FROM atlas_projects.reservations
   WHERE org_id=o AND project_id=w.project_id
   AND id=public.uuid_generate_v5((w.data->>'reservation_id')::uuid,'daily_model_eur');
   SELECT data->'usage' INTO usage FROM atlas_projects.resources
   WHERE org_id=o AND project_id=w.project_id AND id=w.session_id;
   usage := usage || jsonb_build_object('reserved_eur',0,
    'unknown_eur',CASE WHEN model.state='unknown' THEN model.amount ELSE 0 END,
    'cost_eur',CASE WHEN model.state='used' THEN model.amount ELSE coalesce((usage->>'cost_eur')::numeric,0) END);
   stamp := to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
   patch := jsonb_build_object('state','failed','usage',usage,'updated_at',stamp);
   UPDATE atlas_projects.resources SET data=data || patch ||
    jsonb_build_object('revision',(data->>'revision')::bigint+1)
   WHERE org_id=o AND project_id=w.project_id AND id=w.session_id;
   UPDATE atlas_projects.resources SET data=data || patch || jsonb_build_object(
    'revision',(data->>'revision')::bigint+1,
    'content_refs',data->'content_refs' || jsonb_build_array(jsonb_build_object(
      'kind','answer','text',message,'file_version_id',NULL)))
   WHERE org_id=o AND project_id=w.project_id AND kind='turn'
   AND data->>'action'='work' AND data->>'state'='running'
   AND data->>'request_id'=w.data->>'reservation_id';
   UPDATE atlas_projects.file_approvals SET data=data || jsonb_build_object('decision','invalidated')
   WHERE org_id=o AND project_id=w.project_id AND data->>'session_id'=w.session_id::text
   AND consumed_at IS NULL AND data->>'decision' IN ('pending','allowed');
   UPDATE atlas_projects.work_leases SET data=data || jsonb_build_object('released',true,'recovered_at',stamp)
   WHERE org_id=o AND project_id=w.project_id AND session_id=w.session_id;
   INSERT INTO atlas_projects.audit(org_id,id,member_id,project_id,kind)
   VALUES(o,gen_random_uuid(),w.holder_member_id,w.project_id,'work_lease_expired');
   recovered := recovered+1;
  END LOOP;
 END LOOP;
 RETURN recovered;
END $$;
REVOKE ALL ON FUNCTION atlas_projects.recover_expired_work_leases(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION atlas_projects.recover_expired_work_leases(uuid) TO atlas_project_app;
