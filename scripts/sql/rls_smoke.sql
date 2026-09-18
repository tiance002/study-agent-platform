-- RLS smoke-test fixture for the local development database.
--
-- Purpose: prove that "application role + row level security + missing
-- tenant context yields ZERO rows" actually holds inside PostgreSQL,
-- instead of only being asserted in design documents.
--
-- Loading this file is idempotent: it drops and recreates the fixture.
-- It must be run as the superuser - scripts/db_check.cmd does that for you.
--
-- Pure ASCII on purpose (see the note in scripts/pg_start.cmd).

DROP TABLE IF EXISTS demo_doc;

CREATE TABLE demo_doc (
    id        text PRIMARY KEY,
    tenant_id text NOT NULL,
    content   text
);

ALTER TABLE demo_doc ENABLE ROW LEVEL SECURITY;

-- FORCE also binds the table owner. Superusers still bypass RLS entirely,
-- which is exactly why the application role must never be a superuser.
ALTER TABLE demo_doc FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON demo_doc;

-- current_setting('app.tenant_id', true): the second argument means
-- "return NULL when the setting is absent" rather than raising an error.
-- Since `tenant_id = NULL` is never true, a MISSING tenant context
-- degrades to zero rows instead of leaking every row. That is invariant #2
-- enforced by the database, not by application code remembering to filter.
CREATE POLICY tenant_isolation ON demo_doc
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT, INSERT, UPDATE, DELETE ON demo_doc TO study_app;

-- The superuser bypasses RLS, so it can seed both tenants.
INSERT INTO demo_doc VALUES
    ('d1', 't1', 'row owned by tenant t1'),
    ('d2', 't2', 'row owned by tenant t2');
