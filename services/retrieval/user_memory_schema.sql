-- P1d, REQ-SHARE-04: no project grant can expose personal memory.
CREATE TABLE IF NOT EXISTS atlas_projects.user_memory (
    org text NOT NULL,
    subject text NOT NULL,
    data jsonb NOT NULL,
    PRIMARY KEY (org, subject)
);
ALTER TABLE atlas_projects.user_memory ENABLE ROW LEVEL SECURITY;
ALTER TABLE atlas_projects.user_memory FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS personal_scope ON atlas_projects.user_memory;
CREATE POLICY personal_scope ON atlas_projects.user_memory
    USING (org = current_setting('atlas.memory_org', true)
       AND subject = current_setting('atlas.memory_subject', true))
    WITH CHECK (org = current_setting('atlas.memory_org', true)
       AND subject = current_setting('atlas.memory_subject', true));
GRANT SELECT, INSERT, UPDATE, DELETE ON atlas_projects.user_memory TO atlas_project_app;
