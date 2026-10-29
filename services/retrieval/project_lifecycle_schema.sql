-- REQ-PROJ-08 lifecycle upgrade for installations created before archival/deletion.
-- Explicit operator migration; safe to apply repeatedly.
DROP POLICY IF EXISTS project_delete ON atlas_projects.projects;
CREATE POLICY project_delete ON atlas_projects.projects FOR DELETE
 USING(org_id=atlas_projects.org() AND owner_member_id=atlas_projects.member() AND atlas_projects.active());
DROP POLICY IF EXISTS project_read ON atlas_projects.projects;
CREATE POLICY project_read ON atlas_projects.projects FOR SELECT USING
 (org_id=atlas_projects.org() AND (atlas_projects.access(id,ARRAY['owner','editor','viewer']) OR (current_setting('atlas.deleting',true)='true' AND owner_member_id=atlas_projects.member())));
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['legacy_rows','reservations','requests','audit','ledger_events','outbox','resources','resource_sources','file_approvals'] LOOP
  EXECUTE format('DROP POLICY IF EXISTS scoped_delete ON atlas_projects.%I',t);
  EXECUTE format('CREATE POLICY scoped_delete ON atlas_projects.%I FOR DELETE USING(org_id=atlas_projects.org() AND atlas_projects.access(project_id,ARRAY[''owner'']))',t);
 END LOOP;
END $$;
GRANT DELETE ON atlas_projects.legacy_rows,atlas_projects.reservations,atlas_projects.requests,
 atlas_projects.audit,atlas_projects.ledger_events,atlas_projects.outbox,atlas_projects.resources,
 atlas_projects.resource_sources,atlas_projects.file_approvals TO atlas_project_app;
