"""Persist immutable fetched bytes and source-document provenance."""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def _assert_roles_exist() -> None:
    op.execute(
        """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'study_app') THEN
        RAISE EXCEPTION 'role study_app does not exist; create it before migrating';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'study_worker') THEN
        RAISE EXCEPTION 'role study_worker does not exist; create it before migrating';
    END IF;
END
$$
"""
    )


def upgrade() -> None:
    _assert_roles_exist()
    op.execute(
        """
CREATE TABLE source_fetch_artifacts (
    acquisition_id text PRIMARY KEY,
    tenant_id      text NOT NULL REFERENCES tenants (tenant_id),
    project_id     text NOT NULL,
    content_type   text NOT NULL
        CHECK (content_type IN ('text/html', 'text/markdown', 'text/plain')),
    raw_content    bytea NOT NULL
        CHECK (octet_length(raw_content) BETWEEN 1 AND 1048576),
    content_hash   text NOT NULL
        CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$'),
    parser_version text NOT NULL
        CHECK (parser_version IN ('html-to-markdown/v1', 'web-text/v1')),
    fetched_at     timestamptz NOT NULL,
    UNIQUE (tenant_id, project_id, acquisition_id),
    FOREIGN KEY (tenant_id, project_id, acquisition_id)
        REFERENCES acquisition_jobs (tenant_id, project_id, acquisition_id),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, project_id)
)
"""
    )
    op.execute("ALTER TABLE source_fetch_artifacts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE source_fetch_artifacts FORCE ROW LEVEL SECURITY")
    op.execute(
        """
CREATE POLICY source_fetch_artifacts_isolation ON source_fetch_artifacts
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND project_id = current_setting('app.project_id', true)
    )
    WITH CHECK (
        tenant_id = current_setting('app.tenant_id', true)
        AND project_id = current_setting('app.project_id', true)
    )
"""
    )
    op.execute("REVOKE ALL ON source_fetch_artifacts FROM PUBLIC")
    op.execute("REVOKE ALL ON source_fetch_artifacts FROM study_app")
    op.execute("GRANT SELECT, INSERT ON source_fetch_artifacts TO study_worker")

    op.execute(
        "ALTER TABLE source_documents ADD COLUMN fetch_attempt_id text"
    )
    op.execute(
        "ALTER TABLE source_documents ADD COLUMN source_content_type text"
    )
    op.execute(
        "ALTER TABLE source_documents ADD COLUMN raw_content_hash text"
    )
    op.execute(
        """
ALTER TABLE source_documents
    ADD CONSTRAINT source_documents_fetch_provenance_check
    CHECK (
        (
            acquisition_method = 'web_fetch'
            AND fetch_attempt_id IS NOT NULL
            AND source_content_type IN ('text/html', 'text/markdown', 'text/plain')
            AND raw_content_hash ~ '^sha256:[0-9a-f]{64}$'
            AND parser_version IN ('html-to-markdown/v1', 'web-text/v1')
        )
        OR
        (
            acquisition_method <> 'web_fetch'
            AND fetch_attempt_id IS NULL
            AND source_content_type IS NULL
            AND raw_content_hash IS NULL
        )
    )
"""
    )
    op.execute(
        "ALTER TABLE acquisition_jobs"
        " ADD CONSTRAINT acquisition_jobs_attempt_limit_check"
        " CHECK (attempt_count <= 3)"
    )
    op.execute(
        """
ALTER TABLE source_documents
    ADD CONSTRAINT source_documents_fetch_artifact_fk
    FOREIGN KEY (tenant_id, project_id, fetch_attempt_id)
    REFERENCES source_fetch_artifacts (tenant_id, project_id, acquisition_id)
"""
    )


def downgrade() -> None:
    existing = op.get_bind().exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM source_fetch_artifacts)"
    ).scalar_one()
    if existing:
        raise RuntimeError(
            "cannot downgrade acquisition artifacts after fetched source bytes exist"
        )
    op.execute(
        "ALTER TABLE acquisition_jobs"
        " DROP CONSTRAINT acquisition_jobs_attempt_limit_check"
    )
    op.execute(
        "ALTER TABLE source_documents"
        " DROP CONSTRAINT source_documents_fetch_artifact_fk"
    )
    op.execute(
        "ALTER TABLE source_documents"
        " DROP CONSTRAINT source_documents_fetch_provenance_check"
    )
    op.execute(
        "ALTER TABLE source_documents"
        " DROP COLUMN raw_content_hash, DROP COLUMN source_content_type,"
        " DROP COLUMN fetch_attempt_id"
    )
    op.execute("REVOKE ALL ON source_fetch_artifacts FROM study_worker")
    op.execute("DROP POLICY source_fetch_artifacts_isolation ON source_fetch_artifacts")
    op.execute("DROP TABLE source_fetch_artifacts")
