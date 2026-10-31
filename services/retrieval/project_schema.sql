-- Explicit operator migration; never executed by application startup.
CREATE SCHEMA atlas_projects;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='atlas_project_app') THEN
 CREATE ROLE atlas_project_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
 END IF;
END $$;
CREATE TABLE atlas_projects.installation (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    initial_org uuid NOT NULL, initial_admin uuid NOT NULL,
    ceilings jsonb NOT NULL
);
CREATE TABLE atlas_projects.organizations (
    org_id uuid PRIMARY KEY, data jsonb NOT NULL, policy jsonb NOT NULL,
    revision bigint NOT NULL DEFAULT 0
);
CREATE TABLE atlas_projects.members (
    org_id uuid NOT NULL REFERENCES atlas_projects.organizations,
    id uuid NOT NULL, subject text NOT NULL, role text NOT NULL CHECK (role IN ('admin','member')),
    status text NOT NULL CHECK (status IN ('invited','active','revoked')),
    revision bigint NOT NULL DEFAULT 0, data jsonb NOT NULL,
    PRIMARY KEY(org_id,id), UNIQUE(org_id,subject)
);
CREATE TABLE atlas_projects.projects (
    org_id uuid NOT NULL REFERENCES atlas_projects.organizations, id uuid NOT NULL,
    owner_member_id uuid NOT NULL, state text NOT NULL CHECK (state IN ('active','archived','deleting')),
    revision bigint NOT NULL DEFAULT 0, data jsonb NOT NULL, instructions text NOT NULL,
    PRIMARY KEY(org_id,id),
    FOREIGN KEY(org_id,owner_member_id) REFERENCES atlas_projects.members(org_id,id)
);
CREATE TABLE atlas_projects.grants (
    org_id uuid NOT NULL, project_id uuid NOT NULL, member_id uuid NOT NULL,
    role text NOT NULL CHECK(role IN ('owner','editor','viewer')),
    revision bigint NOT NULL, data jsonb NOT NULL,
    PRIMARY KEY(org_id,project_id,member_id),
    FOREIGN KEY(org_id,project_id) REFERENCES atlas_projects.projects(org_id,id),
    FOREIGN KEY(org_id,member_id) REFERENCES atlas_projects.members(org_id,id)
);
CREATE TABLE atlas_projects.legacy_rows (
    org_id uuid NOT NULL, project_id uuid NOT NULL, kind text NOT NULL,
    id text NOT NULL, ordinal bigint NOT NULL, data jsonb NOT NULL,
    PRIMARY KEY(org_id,project_id,kind,id),
    FOREIGN KEY(org_id,project_id) REFERENCES atlas_projects.projects(org_id,id)
);
CREATE TABLE atlas_projects.reservations (
    org_id uuid NOT NULL, project_id uuid NOT NULL, id uuid NOT NULL,
    resource text NOT NULL, amount numeric NOT NULL CHECK(amount >= 0),
    state text NOT NULL CHECK(state IN ('reserved','used','unknown','released')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(org_id,id),
    FOREIGN KEY(org_id,project_id) REFERENCES atlas_projects.projects(org_id,id)
);
CREATE TABLE atlas_projects.requests (
    org_id uuid NOT NULL, member_id uuid NOT NULL, key text NOT NULL,
    project_id uuid,
    method text NOT NULL, path text NOT NULL, digest text NOT NULL,
    result jsonb NOT NULL, status integer NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(org_id,member_id,method,path,key),
    FOREIGN KEY(org_id,project_id) REFERENCES atlas_projects.projects(org_id,id),
    FOREIGN KEY(org_id,member_id) REFERENCES atlas_projects.members(org_id,id)
);
CREATE TABLE atlas_projects.audit (
    org_id uuid NOT NULL REFERENCES atlas_projects.organizations,
    id uuid PRIMARY KEY, member_id uuid NOT NULL, project_id uuid,
    kind text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(org_id,member_id) REFERENCES atlas_projects.members(org_id,id),
    FOREIGN KEY(org_id,project_id) REFERENCES atlas_projects.projects(org_id,id)
);
-- REQ-PROJ-06: the ledger and its publication queue are written together.
-- Event payloads are immutable JSON shaped by the frozen public contract.
CREATE TABLE atlas_projects.ledger_events (
    org_id uuid NOT NULL REFERENCES atlas_projects.organizations,
    project_id uuid NOT NULL,
    seq bigint GENERATED ALWAYS AS IDENTITY,
    event_id uuid NOT NULL,
    data jsonb NOT NULL,
    PRIMARY KEY(org_id,project_id,seq),
    UNIQUE(org_id,project_id,event_id),
    FOREIGN KEY(org_id,project_id) REFERENCES atlas_projects.projects(org_id,id)
);
CREATE TABLE atlas_projects.outbox (
    org_id uuid NOT NULL,
    project_id uuid NOT NULL,
    event_id uuid NOT NULL,
    state text NOT NULL CHECK(state IN ('pending','publishing','published')),
    attempts integer NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    PRIMARY KEY(org_id,project_id,event_id),
    FOREIGN KEY(org_id,project_id,event_id) REFERENCES atlas_projects.ledger_events(org_id,project_id,event_id)
);
CREATE FUNCTION atlas_projects.org() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('atlas.org',true),'')::uuid $$;
CREATE FUNCTION atlas_projects.member() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('atlas.member',true),'')::uuid $$;
CREATE FUNCTION atlas_projects.active() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM atlas_projects.members WHERE org_id=atlas_projects.org()
 AND id=atlas_projects.member() AND subject=current_setting('atlas.subject',true) AND status='active')
$$;
CREATE FUNCTION atlas_projects.admin() RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM atlas_projects.members WHERE org_id=atlas_projects.org()
 AND id=atlas_projects.member() AND subject=current_setting('atlas.subject',true) AND status='active' AND role='admin')
$$;
CREATE FUNCTION atlas_projects.access(p uuid, roles text[]) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT atlas_projects.active() AND
 (nullif(current_setting('atlas.project',true),'') IS NULL OR p::text=current_setting('atlas.project',true))
 AND EXISTS(SELECT 1 FROM atlas_projects.grants g JOIN atlas_projects.projects p2
 ON p2.org_id=g.org_id AND p2.id=g.project_id
 WHERE g.org_id=atlas_projects.org() AND g.project_id=p AND g.member_id=atlas_projects.member()
 AND g.role=ANY(roles) AND p2.state <> 'deleting')
$$;
CREATE FUNCTION atlas_projects.founder(p uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT atlas_projects.active() AND EXISTS(SELECT 1 FROM atlas_projects.projects WHERE
 org_id=atlas_projects.org() AND id=p AND owner_member_id=atlas_projects.member()
 AND NOT EXISTS(SELECT 1 FROM atlas_projects.grants WHERE org_id=atlas_projects.org() AND project_id=p))
$$;
GRANT USAGE ON SCHEMA atlas_projects TO atlas_project_app;
GRANT SELECT ON atlas_projects.installation TO atlas_project_app;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA atlas_projects TO atlas_project_app;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA atlas_projects TO atlas_project_app;
REVOKE INSERT,UPDATE,DELETE ON atlas_projects.installation FROM atlas_project_app;
REVOKE UPDATE,DELETE ON atlas_projects.audit FROM atlas_project_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA atlas_projects FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA atlas_projects TO atlas_project_app;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['organizations','members','projects','grants','legacy_rows','reservations','requests','audit','ledger_events','outbox'] LOOP
 EXECUTE format('ALTER TABLE atlas_projects.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE atlas_projects.%I FORCE ROW LEVEL SECURITY',t);
 END LOOP;
END $$;
CREATE POLICY org_read ON atlas_projects.organizations FOR SELECT USING(org_id=atlas_projects.org() AND atlas_projects.active());
CREATE POLICY org_edit ON atlas_projects.organizations FOR UPDATE USING(org_id=atlas_projects.org() AND atlas_projects.admin());
CREATE POLICY member_read ON atlas_projects.members FOR SELECT USING(org_id=atlas_projects.org() AND atlas_projects.active());
CREATE POLICY member_insert ON atlas_projects.members FOR INSERT WITH CHECK(org_id=atlas_projects.org() AND atlas_projects.admin());
CREATE POLICY member_edit ON atlas_projects.members FOR UPDATE USING(org_id=atlas_projects.org() AND atlas_projects.admin());
CREATE POLICY project_read ON atlas_projects.projects FOR SELECT USING(org_id=atlas_projects.org() AND (atlas_projects.access(id,ARRAY['owner','editor','viewer']) OR (current_setting('atlas.deleting',true)='true' AND owner_member_id=atlas_projects.member())));
CREATE POLICY project_insert ON atlas_projects.projects FOR INSERT WITH CHECK(org_id=atlas_projects.org() AND atlas_projects.active() AND owner_member_id=atlas_projects.member() AND nullif(current_setting('atlas.project',true),'') IS NULL);
CREATE POLICY project_edit ON atlas_projects.projects FOR UPDATE USING(org_id=atlas_projects.org() AND atlas_projects.access(id,ARRAY['owner']));
CREATE POLICY project_delete ON atlas_projects.projects FOR DELETE USING(org_id=atlas_projects.org() AND owner_member_id=atlas_projects.member() AND atlas_projects.active());
CREATE POLICY grant_read ON atlas_projects.grants FOR SELECT USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor','viewer']));
CREATE POLICY grant_insert ON atlas_projects.grants FOR INSERT WITH CHECK(org_id=atlas_projects.org() AND
 (atlas_projects.access(project_id,ARRAY['owner']) OR (atlas_projects.founder(project_id) AND member_id=atlas_projects.member() AND role='owner')));
CREATE POLICY grant_edit ON atlas_projects.grants FOR UPDATE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner']));
CREATE POLICY grant_delete ON atlas_projects.grants FOR DELETE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner']));
CREATE POLICY legacy_read ON atlas_projects.legacy_rows FOR SELECT USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor','viewer']));
CREATE POLICY reservation_read ON atlas_projects.reservations FOR SELECT USING(org_id=atlas_projects.org() AND atlas_projects.active());
CREATE POLICY reservation_insert ON atlas_projects.reservations FOR INSERT WITH CHECK(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor','viewer']));
CREATE POLICY reservation_edit ON atlas_projects.reservations FOR UPDATE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor','viewer']));
CREATE POLICY request_read ON atlas_projects.requests FOR SELECT USING(org_id=atlas_projects.org() AND member_id=atlas_projects.member() AND atlas_projects.active() AND (project_id IS NULL OR atlas_projects.access(project_id,ARRAY['owner','editor','viewer'])));
-- Persist the terminal response/audit even when this transaction revoked its
-- own caller. Reads and every subsequent request still require active grants.
CREATE POLICY request_insert ON atlas_projects.requests FOR INSERT WITH CHECK(org_id=atlas_projects.org() AND member_id=atlas_projects.member() AND (project_id IS NULL OR project_id::text=current_setting('atlas.project',true) OR atlas_projects.access(project_id,ARRAY['owner','editor','viewer'])));
CREATE POLICY audit_read ON atlas_projects.audit FOR SELECT USING(org_id=atlas_projects.org() AND
 (atlas_projects.admin() OR atlas_projects.access(project_id,ARRAY['owner','editor','viewer'])));
CREATE POLICY audit_insert ON atlas_projects.audit FOR INSERT WITH CHECK(org_id=atlas_projects.org() AND member_id=atlas_projects.member());
CREATE POLICY ledger_read ON atlas_projects.ledger_events FOR SELECT USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor','viewer']));
CREATE POLICY ledger_insert ON atlas_projects.ledger_events FOR INSERT WITH CHECK(org_id=atlas_projects.org() AND
 (atlas_projects.access(project_id,ARRAY['owner','editor']) OR
  (atlas_projects.access(project_id,ARRAY['viewer']) AND data->>'kind' IN ('file_read','deliverable_downloaded') AND data->'actor'->>'member_id'=atlas_projects.member()::text)));
CREATE POLICY outbox_read ON atlas_projects.outbox FOR SELECT USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor','viewer']));
CREATE POLICY outbox_insert ON atlas_projects.outbox FOR INSERT WITH CHECK(org_id=atlas_projects.org() AND
 (atlas_projects.access(project_id,ARRAY['owner','editor']) OR
  (atlas_projects.access(project_id,ARRAY['viewer']) AND state='pending' AND EXISTS(
   SELECT 1 FROM atlas_projects.ledger_events e WHERE e.org_id=outbox.org_id AND e.project_id=outbox.project_id AND e.event_id=outbox.event_id
   AND e.data->>'kind' IN ('file_read','deliverable_downloaded') AND e.data->'actor'->>'member_id'=atlas_projects.member()::text))));
CREATE POLICY outbox_edit ON atlas_projects.outbox FOR UPDATE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY['owner','editor']));
-- Counts are organization metadata; they must not depend on the caller's grants.
CREATE FUNCTION atlas_projects.project_count() RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT count(*) FROM atlas_projects.projects WHERE org_id=atlas_projects.org() AND state = 'active' AND atlas_projects.active()
$$;
CREATE FUNCTION atlas_projects.stranded(m uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM atlas_projects.grants g WHERE g.org_id=atlas_projects.org() AND g.member_id=m AND g.role='owner'
 AND NOT EXISTS(SELECT 1 FROM atlas_projects.grants other JOIN atlas_projects.members mem ON mem.org_id=other.org_id AND mem.id=other.member_id
 WHERE other.org_id=g.org_id AND other.project_id=g.project_id AND other.role='owner' AND other.member_id<>m AND mem.status='active'))
$$;
REVOKE ALL ON FUNCTION atlas_projects.project_count(),atlas_projects.stranded(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION atlas_projects.project_count(),atlas_projects.stranded(uuid) TO atlas_project_app;

-- Task C storage. Apply explicitly as part of installation, never at startup.
-- A common scoped key allows source references and later broker publications to
-- share the same isolation boundary without exposing private storage metadata.
CREATE TABLE atlas_projects.resources (
    org_id uuid NOT NULL, project_id uuid NOT NULL, id uuid NOT NULL,
    kind text NOT NULL CHECK(kind IN ('folder','file','version','instructions','memory','derived','job','deliverable','outbox')),
    parent_id uuid, data jsonb NOT NULL, private jsonb NOT NULL DEFAULT '{}',
    retention_class text NOT NULL CHECK(retention_class IN ('C','E','T','A')),
    expires_at timestamptz,
    PRIMARY KEY(org_id,project_id,id),
    FOREIGN KEY(org_id,project_id) REFERENCES atlas_projects.projects(org_id,id),
    FOREIGN KEY(org_id,project_id,parent_id) REFERENCES atlas_projects.resources(org_id,project_id,id)
);
CREATE UNIQUE INDEX resource_path ON atlas_projects.resources
(org_id,project_id,parent_id,(data->>'relative_path'))
WHERE kind='file' AND data->>'deleted_at' IS NULL;
CREATE UNIQUE INDEX folder_name ON atlas_projects.resources
(org_id,project_id,parent_id,(data->>'name')) NULLS NOT DISTINCT
WHERE kind='folder' AND data->>'deleted_at' IS NULL;
CREATE TABLE atlas_projects.resource_sources (
    org_id uuid NOT NULL, project_id uuid NOT NULL, resource_id uuid NOT NULL, version_id uuid NOT NULL,
    PRIMARY KEY(org_id,project_id,resource_id,version_id),
    FOREIGN KEY(org_id,project_id,resource_id) REFERENCES atlas_projects.resources(org_id,project_id,id),
    FOREIGN KEY(org_id,project_id,version_id) REFERENCES atlas_projects.resources(org_id,project_id,id)
);
-- Only the broker/operator can issue approvals. The application may consume an
-- existing decision, never manufacture permission from a caller-supplied UUID.
CREATE TABLE atlas_projects.file_approvals (
    org_id uuid NOT NULL, project_id uuid NOT NULL, id uuid NOT NULL,
    data jsonb NOT NULL, consumed_at timestamptz,
    PRIMARY KEY(org_id,project_id,id),
    FOREIGN KEY(org_id,project_id) REFERENCES atlas_projects.projects(org_id,id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['resources','resource_sources','file_approvals'] LOOP
 EXECUTE format('ALTER TABLE atlas_projects.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE atlas_projects.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY scoped_read ON atlas_projects.%I FOR SELECT USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'',''viewer'']))',t);
 EXECUTE format('CREATE POLICY scoped_insert ON atlas_projects.%I FOR INSERT WITH CHECK(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'']))',t);
 EXECUTE format('CREATE POLICY scoped_edit ON atlas_projects.%I FOR UPDATE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'',''editor'']))',t);
 END LOOP;
END $$;
GRANT SELECT,INSERT,UPDATE ON atlas_projects.resources,atlas_projects.resource_sources TO atlas_project_app;
GRANT SELECT,UPDATE(consumed_at) ON atlas_projects.file_approvals TO atlas_project_app;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['legacy_rows','reservations','requests','audit','ledger_events','outbox','resources','resource_sources','file_approvals'] LOOP
 EXECUTE format('CREATE POLICY scoped_delete ON atlas_projects.%I FOR DELETE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'']))',t);
 END LOOP;
END $$;
GRANT DELETE ON atlas_projects.legacy_rows,atlas_projects.reservations,atlas_projects.requests,
 atlas_projects.audit,atlas_projects.ledger_events,atlas_projects.outbox,atlas_projects.resources,
 atlas_projects.resource_sources,atlas_projects.file_approvals TO atlas_project_app;
CREATE FUNCTION atlas_projects.advance_memory() RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE p uuid := nullif(current_setting('atlas.project',true),'')::uuid; result bigint;
BEGIN
 IF NOT atlas_projects.access(p,ARRAY['owner','editor']) THEN RAISE EXCEPTION 'project access denied' USING ERRCODE='42501'; END IF;
 UPDATE atlas_projects.projects SET data=jsonb_set(data,'{memory_revision}',to_jsonb((data->>'memory_revision')::bigint+1))
 WHERE org_id=atlas_projects.org() AND id=p RETURNING (data->>'memory_revision')::bigint INTO result;
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION atlas_projects.advance_memory() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION atlas_projects.advance_memory() TO atlas_project_app;
CREATE FUNCTION atlas_projects.immutable_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.kind='version' AND
 (NEW.data - ARRAY['revision','updated_at','deleted_at','purge_at','availability','index_status']) IS DISTINCT FROM
 (OLD.data - ARRAY['revision','updated_at','deleted_at','purge_at','availability','index_status']) THEN
 RAISE EXCEPTION 'immutable version' USING ERRCODE='23514';
 END IF;
 IF NEW.kind<>OLD.kind OR NEW.org_id<>OLD.org_id OR NEW.project_id<>OLD.project_id OR NEW.id<>OLD.id THEN
 RAISE EXCEPTION 'immutable resource scope' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER immutable_version BEFORE UPDATE ON atlas_projects.resources
FOR EACH ROW EXECUTE FUNCTION atlas_projects.immutable_version();
REVOKE ALL ON FUNCTION atlas_projects.immutable_version() FROM PUBLIC;

-- Task F: the authenticated identity broker resolves only its asserted subject.
-- This bootstrap lookup precedes RLS scope; project grants are checked by scope().
CREATE FUNCTION atlas_projects.identity_memberships(asserted_subject text)
RETURNS TABLE(org_id uuid, member_id uuid, role text, membership_revision bigint, policy_revision bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
 SELECT m.org_id, m.id, m.role, m.revision, o.revision
 FROM atlas_projects.members m JOIN atlas_projects.organizations o ON o.org_id=m.org_id
 WHERE m.subject=asserted_subject AND m.status='active'
 ORDER BY m.org_id
$$;
REVOKE ALL ON FUNCTION atlas_projects.identity_memberships(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION atlas_projects.identity_memberships(text) TO atlas_project_app;
CREATE FUNCTION atlas_projects.ensure_member(asserted_subject text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE organization uuid; identifier uuid; active_count bigint;
BEGIN
 SELECT org_id, count(*) OVER() INTO organization, active_count
 FROM atlas_projects.organizations WHERE data->>'status'='active' ORDER BY org_id;
 IF active_count <> 1 THEN RETURN NULL; END IF;
 SELECT id INTO identifier FROM atlas_projects.members
 WHERE org_id=organization AND subject=asserted_subject;
 IF identifier IS NOT NULL THEN RETURN identifier; END IF;
 identifier=gen_random_uuid();
 INSERT INTO atlas_projects.members(org_id,id,subject,role,status,revision,data)
 VALUES(organization,identifier,asserted_subject,'member','active',0,
   jsonb_build_object('id',identifier,'display_name',asserted_subject));
 RETURN identifier;
END $$;
REVOKE ALL ON FUNCTION atlas_projects.ensure_member(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION atlas_projects.ensure_member(text) TO atlas_project_app;
