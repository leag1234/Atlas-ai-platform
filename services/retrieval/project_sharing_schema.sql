-- REQ-SHARE-02/03/06: repeatable operator migration after transcript schema.
ALTER TABLE atlas_projects.audit ADD COLUMN IF NOT EXISTS details jsonb NOT NULL DEFAULT '{}';
CREATE OR REPLACE FUNCTION atlas_projects.conversation_visible(p uuid, c text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT c IS NULL OR EXISTS (
 SELECT 1 FROM atlas_projects.resources r WHERE r.org_id=atlas_projects.org()
 AND r.project_id=p AND r.id::text=c AND r.kind='conversation'
 AND (r.data->>'owner_member_id'=atlas_projects.member()::text OR r.data->>'visibility'='shared'))
 OR (NOT EXISTS (SELECT 1 FROM atlas_projects.resources r WHERE r.org_id=atlas_projects.org()
 AND r.project_id=p AND r.id::text=c AND r.kind='conversation') AND EXISTS
 (SELECT 1 FROM atlas_projects.legacy_rows r WHERE r.org_id=atlas_projects.org()
 AND r.project_id=p AND r.kind='conversations' AND r.id=c))
$$;
CREATE OR REPLACE FUNCTION atlas_projects.session_visible(p uuid, s text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT s IS NULL OR EXISTS (SELECT 1 FROM atlas_projects.resources r
 WHERE r.org_id=atlas_projects.org() AND r.project_id=p AND r.id::text=s
 AND r.kind='work_session' AND atlas_projects.conversation_visible(p,r.data->>'conversation_id'))
$$;
REVOKE ALL ON FUNCTION atlas_projects.conversation_visible(uuid,text),atlas_projects.session_visible(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION atlas_projects.conversation_visible(uuid,text),atlas_projects.session_visible(uuid,text) TO atlas_project_app;
-- Appending needs the global chain head, never another member's event content.
CREATE OR REPLACE FUNCTION atlas_projects.ledger_head(p uuid)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT data->>'digest' FROM atlas_projects.ledger_events
 WHERE org_id=atlas_projects.org() AND project_id=p
 AND atlas_projects.access(p,ARRAY['owner','editor','viewer']) ORDER BY seq DESC LIMIT 1
$$;
REVOKE ALL ON FUNCTION atlas_projects.ledger_head(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION atlas_projects.ledger_head(uuid) TO atlas_project_app;
DROP POLICY IF EXISTS conversation_privacy ON atlas_projects.outbox;
CREATE POLICY conversation_privacy ON atlas_projects.outbox AS RESTRICTIVE USING
(EXISTS (SELECT 1 FROM atlas_projects.ledger_events e WHERE e.org_id=outbox.org_id
 AND e.project_id=outbox.project_id AND e.event_id=outbox.event_id));
DROP POLICY IF EXISTS conversation_privacy ON atlas_projects.resources;
CREATE POLICY conversation_privacy ON atlas_projects.resources AS RESTRICTIVE USING
(CASE WHEN kind='conversation' THEN data->>'owner_member_id'=atlas_projects.member()::text OR data->>'visibility'='shared'
 WHEN kind IN ('turn','work_session') THEN atlas_projects.conversation_visible(project_id,data->>'conversation_id') ELSE true END);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['ledger_events','file_approvals','work_progress'] LOOP
 EXECUTE format('DROP POLICY IF EXISTS conversation_privacy ON atlas_projects.%I',t);
 EXECUTE format('CREATE POLICY conversation_privacy ON atlas_projects.%I AS RESTRICTIVE USING
 (atlas_projects.conversation_visible(project_id,data->>''conversation_id'') AND atlas_projects.session_visible(project_id,data->>''session_id''))',t);
 END LOOP;
END $$;
DROP POLICY IF EXISTS conversation_privacy ON atlas_projects.work_leases;
CREATE POLICY conversation_privacy ON atlas_projects.work_leases AS RESTRICTIVE USING
(atlas_projects.session_visible(project_id,session_id::text));
DROP POLICY IF EXISTS conversation_privacy ON atlas_projects.transcript_progress;
CREATE POLICY conversation_privacy ON atlas_projects.transcript_progress AS RESTRICTIVE USING
(EXISTS (SELECT 1 FROM atlas_projects.resources r WHERE r.org_id=transcript_progress.org_id
 AND r.project_id=transcript_progress.project_id AND r.id=turn_id));
-- Revocation must not need permission to read the departed member's private data.
CREATE OR REPLACE FUNCTION atlas_projects.invalidate_member_work(o uuid, p uuid, m uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 UPDATE atlas_projects.resources SET data=jsonb_set(data,'{state}','"cancelled"')
 WHERE org_id=o AND (p IS NULL OR project_id=p) AND kind='turn'
 AND data->>'created_by_member_id'=m::text AND data->>'state'='running';
 UPDATE atlas_projects.resources r SET data=jsonb_set(r.data,'{state}','"cancelling"')
 WHERE r.org_id=o AND (p IS NULL OR r.project_id=p) AND r.kind='work_session'
 AND EXISTS (SELECT 1 FROM atlas_projects.work_leases l WHERE l.org_id=o
 AND l.project_id=r.project_id AND l.session_id=r.id AND l.holder_member_id=m
 AND NOT coalesce((l.data->>'released')::boolean,false));
 UPDATE atlas_projects.file_approvals SET data=jsonb_set(data,'{decision}','"invalidated"')
 WHERE org_id=o AND (p IS NULL OR project_id=p) AND consumed_at IS NULL
 AND data->>'decision' IN ('pending','allowed') AND
 (data->>'approver_member_id'=m::text OR data->>'requested_by_member_id'=m::text);
 IF atlas_projects.member() IS NOT NULL THEN
 INSERT INTO atlas_projects.audit(org_id,id,member_id,project_id,kind,details)
 VALUES(o,gen_random_uuid(),atlas_projects.member(),p,'member_work_invalidated',jsonb_build_object('member_id',m));
 END IF;
END $$;
CREATE OR REPLACE FUNCTION atlas_projects.sharing_revoked() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF current_setting('atlas.deleting',true)='true' THEN
  RETURN NULL;
 END IF;
 IF TG_TABLE_NAME='grants' THEN
  IF TG_OP='DELETE' OR NEW.role IS DISTINCT FROM OLD.role THEN
   PERFORM atlas_projects.invalidate_member_work(OLD.org_id,OLD.project_id,OLD.member_id);
  END IF;
 ELSIF NEW.status IS DISTINCT FROM OLD.status OR NEW.role IS DISTINCT FROM OLD.role THEN
  PERFORM atlas_projects.invalidate_member_work(OLD.org_id,NULL,OLD.id);
 END IF;
 RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS sharing_revoked ON atlas_projects.grants;
CREATE TRIGGER sharing_revoked AFTER DELETE OR UPDATE ON atlas_projects.grants
FOR EACH ROW EXECUTE FUNCTION atlas_projects.sharing_revoked();
DROP TRIGGER IF EXISTS sharing_revoked ON atlas_projects.members;
CREATE TRIGGER sharing_revoked AFTER UPDATE ON atlas_projects.members
FOR EACH ROW EXECUTE FUNCTION atlas_projects.sharing_revoked();
REVOKE ALL ON FUNCTION atlas_projects.invalidate_member_work(uuid,uuid,uuid),atlas_projects.sharing_revoked() FROM PUBLIC,atlas_project_app;

-- Narrow administrative metadata mutation: never returns project content.
CREATE OR REPLACE FUNCTION atlas_projects.manage_project_grant(p uuid, m uuid, transfer boolean, expected bigint)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE prior uuid; rev bigint; target jsonb; g jsonb; stamp text := to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
BEGIN
 IF NOT atlas_projects.admin() THEN RETURN 'permission_denied'; END IF;
 SELECT owner_member_id,revision INTO prior,rev FROM atlas_projects.projects
 WHERE org_id=atlas_projects.org() AND id=p AND state<>'deleting' FOR UPDATE;
 IF NOT FOUND THEN RETURN 'not_found'; END IF;
 IF transfer THEN
  IF rev<>expected THEN RETURN 'revision_conflict'; END IF;
  SELECT data INTO target FROM atlas_projects.members WHERE org_id=atlas_projects.org() AND id=m AND status='active';
  IF NOT FOUND THEN RETURN 'not_found'; END IF;
  g := jsonb_build_object('id',gen_random_uuid(),'org_id',atlas_projects.org(),'project_id',p,'member_id',m,
   'created_by_member_id',atlas_projects.member(),'created_at',stamp,'updated_at',stamp,'deleted_at',NULL,'purge_at',NULL,'revision',1,'role','owner');
  INSERT INTO atlas_projects.grants VALUES(atlas_projects.org(),p,m,'owner',1,g)
  ON CONFLICT(org_id,project_id,member_id) DO UPDATE SET role='owner',revision=atlas_projects.grants.revision+1,
   data=atlas_projects.grants.data || jsonb_build_object('role','owner','revision',atlas_projects.grants.revision+1,'updated_at',stamp);
  UPDATE atlas_projects.projects SET owner_member_id=m,revision=revision+1 WHERE org_id=atlas_projects.org() AND id=p;
  UPDATE atlas_projects.grants SET role='editor',revision=revision+1,
   data=data || jsonb_build_object('role','editor','revision',revision+1,'updated_at',stamp)
   WHERE org_id=atlas_projects.org() AND project_id=p AND member_id=prior AND member_id<>m;
 ELSE
  SELECT revision INTO rev FROM atlas_projects.grants WHERE org_id=atlas_projects.org() AND project_id=p AND member_id=m;
  IF NOT FOUND THEN RETURN 'not_found'; END IF;
  IF rev<>expected THEN RETURN 'revision_conflict'; END IF;
  IF EXISTS(SELECT 1 FROM atlas_projects.grants WHERE org_id=atlas_projects.org() AND project_id=p AND member_id=m AND role='owner') THEN
   SELECT g2.member_id INTO prior FROM atlas_projects.grants g2 JOIN atlas_projects.members mem ON mem.org_id=g2.org_id AND mem.id=g2.member_id
    WHERE g2.org_id=atlas_projects.org() AND g2.project_id=p AND g2.member_id<>m AND g2.role='owner' AND mem.status='active' ORDER BY g2.member_id LIMIT 1;
   IF NOT FOUND THEN RETURN 'last_owner'; END IF;
   UPDATE atlas_projects.projects SET owner_member_id=prior,revision=revision+1 WHERE org_id=atlas_projects.org() AND id=p AND owner_member_id=m;
  END IF;
  DELETE FROM atlas_projects.grants WHERE org_id=atlas_projects.org() AND project_id=p AND member_id=m;
 END IF;
 INSERT INTO atlas_projects.audit(org_id,id,member_id,project_id,kind,details)
 VALUES(atlas_projects.org(),gen_random_uuid(),atlas_projects.member(),p,CASE WHEN transfer THEN 'ownership_transferred' ELSE 'admin_grant_revoked' END,
 jsonb_build_object('member_id',m,'previous_owner_member_id',prior));
 RETURN 'ok';
END $$;
REVOKE ALL ON FUNCTION atlas_projects.manage_project_grant(uuid,uuid,boolean,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION atlas_projects.manage_project_grant(uuid,uuid,boolean,bigint) TO atlas_project_app;

-- Owners may stop execution without acquiring access to private content.
CREATE OR REPLACE FUNCTION atlas_projects.cancel_private_work(p uuid, s uuid, request uuid)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE active_request uuid;
BEGIN
 IF NOT (atlas_projects.access(p,ARRAY['owner']) OR
 (atlas_projects.admin() AND atlas_projects.access(p,ARRAY['viewer','editor','owner']))) THEN
 RETURN 'not_found'; END IF;
 IF NOT EXISTS(SELECT 1 FROM atlas_projects.resources WHERE org_id=atlas_projects.org()
 AND project_id=p AND id=s AND kind='work_session') THEN RETURN 'not_found'; END IF;
 SELECT request_id INTO active_request FROM atlas_projects.work_leases
 WHERE org_id=atlas_projects.org() AND project_id=p AND session_id=s AND NOT coalesce((data->>'released')::boolean,false);
 IF active_request IS NOT NULL AND active_request<>request THEN RETURN 'session_busy'; END IF;
 UPDATE atlas_projects.resources SET data=jsonb_set(data,'{state}',to_jsonb(
 CASE WHEN active_request IS NULL THEN 'cancelled'::text ELSE 'cancelling'::text END))
 WHERE org_id=atlas_projects.org() AND project_id=p AND id=s;
 UPDATE atlas_projects.file_approvals SET data=jsonb_set(data,'{decision}','"invalidated"')
 WHERE org_id=atlas_projects.org() AND project_id=p AND data->>'session_id'=s::text
 AND consumed_at IS NULL AND data->>'decision' IN ('pending','allowed');
 INSERT INTO atlas_projects.audit(org_id,id,member_id,project_id,kind,details)
 VALUES(atlas_projects.org(),gen_random_uuid(),atlas_projects.member(),p,'work_cancelled',jsonb_build_object('session_id',s));
 RETURN 'ok';
END $$;
REVOKE ALL ON FUNCTION atlas_projects.cancel_private_work(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION atlas_projects.cancel_private_work(uuid,uuid,uuid) TO atlas_project_app;
